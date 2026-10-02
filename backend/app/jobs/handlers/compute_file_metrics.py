"""Quality snapshot for a file, refreshed progressively during the pipeline.

Pure aggregation — no LLM. Independent of file status (mux/completion): it
reflects the latest state of whatever data exists; metrics with no source data
yet stay NULL rather than becoming 0. Two complementary distances:

- edit_distance_norm: mean normalized Levenshtein between the AI pipeline's
  final output (original_ai_translated_text, which polish keeps in sync
  with its edits) and what actually shipped (translated_text). Nonzero only
  when a human corrected lines — falling across episodes = the consistency
  layer (glossary, TM, style bible) is working.
- polish_churn_norm: mean normalized Levenshtein over the polish pass's
  before/after pairs (stored in polish_edit QaItem details). High churn =
  the cheap translation model needs better context or a better model.
"""
from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import datetime
from typing import Any

from rapidfuzz.distance import Levenshtein
from sqlalchemy import delete, func, select

from app.core.database import SyncSessionLocal
from app.db.models import File, FileQualityMetric, LlmCall, QaItem, SubtitleChunk, SubtitleEvent
from app.jobs.context import JobContext, JobResult, ProgressFn
from app.jobs.registry import register_job_handler
from app.subs.tag_masking import plain_text

logger = logging.getLogger(__name__)


def metrics_fingerprint_stmt(file_id: int):
    """One cheap SELECT summarising every input the metrics read. Shared by the
    handler (stored on the snapshot) and the orchestrator (staleness check)."""
    ev = (
        select(
            func.count(SubtitleEvent.translated_text),
            func.coalesce(func.sum(func.length(SubtitleEvent.translated_text)), 0),
            func.coalesce(func.sum(func.length(SubtitleEvent.original_ai_translated_text)), 0),
            func.coalesce(func.sum(SubtitleEvent.is_user_edited), 0),
            func.coalesce(func.sum(SubtitleEvent.is_approved), 0),
        )
        .where(SubtitleEvent.file_id == file_id, SubtitleEvent.event_type == "dialogue")
        .subquery()
    )
    qa = (
        select(func.count(QaItem.id), func.coalesce(func.max(QaItem.id), 0))
        .where(QaItem.file_id == file_id).subquery()
    )
    llm = select(func.count(LlmCall.id)).where(LlmCall.file_id == file_id).scalar_subquery()
    return select(*ev.c, *qa.c, llm)


def fingerprint_of(row) -> str:
    return ":".join(str(v) for v in tuple(row))


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


@register_job_handler("compute_file_metrics")
def compute_file_metrics(
    payload: dict[str, Any],
    ctx: JobContext,
    progress: ProgressFn,
) -> JobResult:
    file_id: int = payload["file_id"]
    now = datetime.utcnow().isoformat()

    progress(0.1, "Loading events")

    with SyncSessionLocal() as session:
        file = session.get(File, file_id)
        if file is None:
            return JobResult(status="failed", result=None,
                             error_code="FILE_NOT_FOUND",
                             error_message=f"File id={file_id} not found")
        project_id = file.project_id
        translation_attempt = file.translation_requested_at
        # Taken before the data reads: a concurrent change then only causes an
        # extra refresh, never a missed one.
        fingerprint = fingerprint_of(session.execute(metrics_fingerprint_stmt(file_id)).one())

        events = list(session.scalars(
            select(SubtitleEvent)
            .where(SubtitleEvent.file_id == file_id)
            .where(SubtitleEvent.event_type == "dialogue")
            .where(SubtitleEvent.translated_text.isnot(None))
        ).all())

        distances: list[float] = []
        confidences: list[float] = []
        confidences_edited: list[float] = []
        user_edited = approved = 0
        for e in events:
            if e.is_user_edited:
                user_edited += 1
            if e.is_approved:
                approved += 1
            if e.original_ai_translated_text is not None:
                ai_text = plain_text(e.original_ai_translated_text)
                final_text = plain_text(e.translated_text or "")
                if ai_text or final_text:
                    distances.append(Levenshtein.normalized_distance(ai_text, final_text))
            if e.translation_confidence is not None:
                confidences.append(e.translation_confidence)
                if e.is_user_edited:
                    confidences_edited.append(e.translation_confidence)

        qa_counts = Counter(
            row.severity
            for row in session.scalars(
                select(QaItem).where(QaItem.file_id == file_id)
            ).all()
        )

        polish_rows = list(session.scalars(
            select(QaItem.details_json)
            .where(QaItem.file_id == file_id, QaItem.qa_type == "polish_edit")
        ).all())
        polish_edit_count = len(polish_rows)
        polish_churn: list[float] = []
        for details_json in polish_rows:
            try:
                details = json.loads(details_json or "{}")
            except ValueError:
                continue
            before = plain_text(details.get("before") or "")
            after = plain_text(details.get("after") or "")
            if before or after:
                polish_churn.append(Levenshtein.normalized_distance(before, after))

        cost_row = session.execute(
            select(
                func.sum(LlmCall.cost_usd),
                func.sum(LlmCall.prompt_tokens),
                func.sum(LlmCall.completion_tokens),
            ).where(LlmCall.file_id == file_id)
        ).one()

        prompt_versions = [
            v for v in session.scalars(
                select(SubtitleChunk.prompt_version)
                .where(SubtitleChunk.file_id == file_id)
                .where(SubtitleChunk.prompt_version.isnot(None))
            ).all()
        ]
        prompt_version = Counter(prompt_versions).most_common(1)[0][0] if prompt_versions else None

    progress(0.7, "Writing metrics")

    with SyncSessionLocal() as session:
        current_file = session.get(File, file_id)
        if (current_file is None
                or current_file.translation_requested_at != translation_attempt):
            return JobResult(
                status="succeeded",
                result={"skipped": "stale translation attempt"},
                error_code=None,
                error_message=None,
            )
        session.execute(delete(FileQualityMetric).where(FileQualityMetric.file_id == file_id))
        session.add(FileQualityMetric(
            file_id=file_id,
            project_id=project_id,
            prompt_version=prompt_version,
            events_total=len(events),
            events_user_edited=user_edited,
            events_approved=approved,
            edit_distance_norm=_mean(distances),
            polish_churn_norm=_mean(polish_churn),
            polish_edit_count=polish_edit_count,
            qa_blockers=qa_counts.get("blocker", 0),
            qa_warnings=qa_counts.get("warning", 0),
            qa_info=qa_counts.get("info", 0),
            mean_confidence=_mean(confidences),
            mean_confidence_edited=_mean(confidences_edited),
            llm_cost_usd=float(cost_row[0]) if cost_row[0] is not None else None,
            prompt_tokens=int(cost_row[1] or 0),
            completion_tokens=int(cost_row[2] or 0),
            source_fingerprint=fingerprint,
            created_at=now,
        ))
        session.commit()

    progress(1.0, "Done")
    return JobResult(
        status="succeeded",
        result={
            "events_total": len(events),
            "edit_distance_norm": _mean(distances),
            "polish_churn_norm": _mean(polish_churn),
            "user_edited": user_edited,
        },
        error_code=None,
        error_message=None,
    )
