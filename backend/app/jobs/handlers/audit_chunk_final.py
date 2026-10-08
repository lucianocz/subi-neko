"""Read-only LLM audit of the final, committed dialogue translation.

This stage deliberately runs after ``review_chunk_final`` (including its
optional targeted Polish loop).  It records human-review findings but never
changes subtitle text, timings, confidence, locks, approvals, or Polish state.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Any

from sqlalchemy import delete, select

from app.core.database import SyncSessionLocal
from app.db.models import File, QaItem, SubtitleChunk, SubtitleEvent
from app.jobs.context import JobContext, JobResult, ProgressFn
from app.jobs.handlers.prompt_context import (
    build_character_block,
    build_glossary_block,
    build_scene_block,
    build_speaker_identity_map,
    build_style_block,
    build_unmapped_speaker_block,
    load_analysis_context,
    load_episode_context,
    load_following_context_events,
    load_glossary_terms,
    load_preceding_context_events,
    load_prompt_characters,
    load_style_context,
    load_unmapped_gendered_speakers,
)
from app.jobs.registry import register_job_handler
from app.llm import client as llm_client
from app.llm.schemas import (
    FINAL_AUDIT_CATEGORIES,
    FINAL_AUDIT_SEVERITIES,
    FinalAuditResponse,
)
from app.subs.tag_masking import plain_text

logger = logging.getLogger(__name__)

FINAL_AUDIT_QA_TYPES = {f"final_audit_{category}" for category in FINAL_AUDIT_CATEGORIES}
_MIN_EXPLANATION_LENGTH = 4


def _identity_suffix(identity: tuple[str | None, str | None]) -> str:
    name, gender = identity
    values = [value for value in (name, gender) if value]
    return f" ({', '.join(values)})" if values else ""


def _context_line(prefix: str, event: SubtitleEvent, include_translation: bool) -> str:
    speaker = f" ({event.name})" if event.name else ""
    line = (f"[{prefix}] {event.line_index}{speaker}: "
            f"SOURCE: {plain_text(event.source_text or '')}")
    if include_translation:
        line += f" | CZ: {plain_text(event.translated_text or '')}"
    return line


def _validate_issues(
    response: FinalAuditResponse,
    target_by_index: dict[int, dict[str, Any]],
) -> tuple[list[dict[str, Any]] | None, str | None]:
    """Validate and de-duplicate model findings before any DB mutation."""
    rows: list[dict[str, Any]] = []
    seen: set[tuple[int, str, str]] = set()
    for position, issue in enumerate(response.issues):
        if issue.i not in target_by_index:
            return None, f"Issue {position} references out-of-chunk line {issue.i}"
        category = issue.category.strip().lower()
        if category not in FINAL_AUDIT_CATEGORIES:
            return None, f"Issue {position} has unsupported category {issue.category!r}"
        severity = issue.severity.strip().lower()
        if severity not in FINAL_AUDIT_SEVERITIES:
            return None, f"Issue {position} has unsupported severity {issue.severity!r}"
        explanation = issue.explanation.strip()
        if len(explanation) < _MIN_EXPLANATION_LENGTH or not re.search(r"\w", explanation):
            return None, f"Issue {position} has an empty or meaningless explanation"
        suggestion = issue.suggestion.strip() if issue.suggestion else None
        key = (issue.i, category, re.sub(r"\s+", " ", explanation).casefold())
        if key in seen:
            continue
        seen.add(key)
        rows.append({
            "line_index": issue.i,
            "category": category,
            "severity": severity,
            "explanation": explanation,
            "suggestion": suggestion or None,
        })
    return rows, None


@register_job_handler("audit_chunk_final")
def audit_chunk_final(
    payload: dict[str, Any],
    ctx: JobContext,
    progress: ProgressFn,
) -> JobResult:
    file_id: int = payload["file_id"]
    chunk_index: int = payload["chunk_index"]
    model: str = payload.get("model") or ctx.options.openai_model_better or ctx.options.openai_model_cheap
    now = datetime.utcnow().isoformat()

    progress(0.05, "Loading final reviewed dialogue")
    with SyncSessionLocal() as session:
        chunk = session.scalar(
            select(SubtitleChunk)
            .where(SubtitleChunk.file_id == file_id)
            .where(SubtitleChunk.chunk_index == chunk_index)
        )
        if chunk is None:
            return JobResult(status="failed", result=None, error_code="CHUNK_NOT_FOUND",
                             error_message=f"Chunk {chunk_index} for file {file_id} not found")
        # ``audited`` is accepted so an explicit rerun can atomically replace
        # this stage's unresolved findings without touching any other QA.
        if chunk.status not in ("final_reviewed", "audited"):
            return JobResult(
                status="failed", result=None, error_code="CHUNK_NOT_FINAL_REVIEWED",
                error_message=(f"Chunk {chunk_index} has status '{chunk.status}'; "
                               "audit_chunk_final requires 'final_reviewed' or 'audited'"),
            )
        if (chunk.content_type or "dialogue") != "dialogue":
            return JobResult(
                status="failed", result=None, error_code="UNSUPPORTED_CONTENT_TYPE",
                error_message="Final QA Audit is only valid for dialogue chunks",
            )

        file = session.get(File, file_id)
        if file is None:
            return JobResult(status="failed", result=None, error_code="FILE_NOT_FOUND",
                             error_message=f"File {file_id} not found")

        targets = list(session.scalars(
            select(SubtitleEvent)
            .where(SubtitleEvent.file_id == file_id)
            .where(SubtitleEvent.event_type == "dialogue")
            .where(SubtitleEvent.content_type == "dialogue")
            .where(SubtitleEvent.line_index >= chunk.translate_from_line)
            .where(SubtitleEvent.line_index <= chunk.translate_to_line)
            .order_by(SubtitleEvent.line_index)
        ).all())
        if not targets:
            return JobResult(status="failed", result=None, error_code="NO_TARGET_EVENTS",
                             error_message=f"No dialogue events in chunk {chunk_index}")

        # These rows are loaded after review_chunk_final committed, so any
        # deterministic row rebalancing is already reflected here.
        target_snapshot = [{
            "id": event.id,
            "line_index": event.line_index,
            "name": event.name,
            "source_text": event.source_text,
            "translated_text": event.translated_text,
        } for event in targets]

        identities = build_speaker_identity_map(session, file.project_id)
        characters = load_prompt_characters(session, file.project_id)
        unmapped_speakers = load_unmapped_gendered_speakers(session, file.project_id)
        style = load_style_context(session, file.project_id)
        glossary = load_glossary_terms(session, file.project_id)
        analysis = load_analysis_context(session, file_id)
        episode_context = load_episode_context(session, file)
        preceding = load_preceding_context_events(
            session, file_id, "dialogue", chunk.translate_from_line,
            max(0, ctx.options.prepend_context_size),
        )
        following = load_following_context_events(
            session, file_id, "dialogue", chunk.translate_to_line,
            max(0, ctx.options.lookahead_context_size),
        )
        # An adjacent chunk can still be in its Polish/final-review loop while
        # this audit runs.  Never expose that chunk's intermediate Czech draft
        # as if it were committed context; source-only context remains useful.
        final_context_ranges = [
            (other.translate_from_line, other.translate_to_line)
            for other in session.scalars(
                select(SubtitleChunk)
                .where(SubtitleChunk.file_id == file_id)
                .where(SubtitleChunk.content_type == "dialogue")
                .where(SubtitleChunk.status.in_(("final_reviewed", "audited", "complete")))
            ).all()
        ]

    def has_final_context_translation(event: SubtitleEvent) -> bool:
        return any(start <= event.line_index <= end for start, end in final_context_ranges)

    progress(0.2, f"Building audit prompt for {len(target_snapshot)} lines")
    context_parts: list[str] = []
    if episode_context:
        context_parts.append(episode_context)
    if analysis is not None:
        scene = build_scene_block(
            analysis, target_snapshot[0]["line_index"], target_snapshot[-1]["line_index"])
        if scene:
            context_parts.append(scene)

    style_parts = [
        build_character_block(characters),
        build_unmapped_speaker_block(unmapped_speakers),
        build_style_block(
            style,
            {item["name"] for item in target_snapshot if item["name"]},
            identities,
        ),
        build_glossary_block(glossary, [item["source_text"] or "" for item in target_snapshot]),
    ]

    sections: list[str] = []
    if context_parts:
        sections.append("## Episode context\n" + "\n".join(context_parts))
    relevant_style = "\n".join(part for part in style_parts if part)
    if relevant_style:
        sections.append("## Relevant style and character context\n" + relevant_style)
    if preceding:
        sections.append("## Previous dialogue (read-only)\n" + "\n".join(
            _context_line("CONTEXT", event, has_final_context_translation(event))
            for event in preceding
        ))

    target_lines: list[str] = []
    for item in target_snapshot:
        identity = identities.get(item["name"] or "", (item["name"], None))
        target_lines.extend([
            f"[LINE] {item['line_index']}{_identity_suffix(identity)}:",
            f"  SOURCE: {plain_text(item['source_text'] or '')}",
            f"  CZ: {plain_text(item['translated_text'] or '')}",
        ])
    sections.append("## Lines to audit\n" + "\n".join(target_lines))
    if following:
        sections.append("## Following dialogue (read-only)\n" + "\n".join(
            _context_line("AHEAD", event, has_final_context_translation(event))
            for event in following
        ))
    user_message = "\n\n".join(sections)

    progress(0.4, f"Calling final QA auditor ({model})")
    try:
        response, stats = llm_client.complete(
            task="final_audit",
            model=model,
            system=ctx.options.resolved_final_qa_prompt().strip(),
            user=user_message,
            schema=FinalAuditResponse,
            options=ctx.options,
            max_completion_tokens=llm_client.completion_budget(len(user_message), len(target_snapshot)),
            project_id=file.project_id,
            file_id=file_id,
            chunk_id=chunk.id,
        )
    except llm_client.LlmError as exc:
        return JobResult(status="failed", result=None,
                         error_code=exc.code, error_message=exc.message)

    target_by_index = {item["line_index"]: item for item in target_snapshot}
    findings, validation_error = _validate_issues(response, target_by_index)
    if validation_error is not None:
        return JobResult(status="failed", result=None,
                         error_code="RESPONSE_VALIDATION_ERROR",
                         error_message=validation_error)
    assert findings is not None

    progress(0.8, f"Persisting {len(findings)} audit finding(s)")
    event_ids = [item["id"] for item in target_snapshot]
    rows = [{
        "file_id": file_id,
        "subtitle_event_id": target_by_index[finding["line_index"]]["id"],
        "severity": finding["severity"],
        "qa_type": f"final_audit_{finding['category']}",
        "message": finding["explanation"],
        "details_json": json.dumps(
            {"suggestion": finding["suggestion"]}, ensure_ascii=False
        ) if finding["suggestion"] else None,
        "is_resolved": 0,
        "created_at": now,
    } for finding in findings]

    # Findings and successful state transition share one transaction.  No
    # subtitle-event columns are updated by this stage.
    with SyncSessionLocal() as session:
        fresh_chunk = session.scalar(
            select(SubtitleChunk)
            .where(SubtitleChunk.file_id == file_id)
            .where(SubtitleChunk.chunk_index == chunk_index)
        )
        if fresh_chunk is None or fresh_chunk.status not in ("final_reviewed", "audited"):
            return JobResult(status="failed", result=None, error_code="STALE_CHUNK_STATUS",
                             error_message="Chunk status changed while Final QA Audit was running")
        session.execute(delete(QaItem).where(
            QaItem.subtitle_event_id.in_(event_ids),
            QaItem.qa_type.in_(sorted(FINAL_AUDIT_QA_TYPES)),
            QaItem.is_resolved == 0,
        ))
        if rows:
            session.execute(QaItem.__table__.insert(), rows)
        fresh_chunk.status = "audited"
        fresh_chunk.updated_at = now
        session.commit()

    progress(1.0, "Final QA Audit complete")
    return JobResult(
        status="succeeded",
        result={
            "findings_created": len(rows),
            "audited_events": len(target_snapshot),
            "model_used": model,
            "response_mode": stats.response_mode,
            "prompt_tokens": stats.prompt_tokens,
            "completion_tokens": stats.completion_tokens,
        },
        error_code=None,
        error_message=None,
    )
