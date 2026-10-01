"""Per-file script analysis pass — runs once before a file's chunks start.

Reads the whole script in order (all content types) plus the previous
episode's synopsis and the character roster, and produces: an episode
synopsis, scene segmentation, tricky-line translator notes, T–V address
pairs, and suggested glossary terms. Consumed by translate/polish prompts.
"""
from __future__ import annotations

import json
import logging

from datetime import datetime
from typing import Any

from sqlalchemy import delete, select

from app.core.database import SyncSessionLocal
from app.db.models import File, FileAnalysis, SubtitleEvent
from app.jobs.context import JobContext, JobResult, ProgressFn
from app.jobs.handlers.prompt_context import (
    build_character_block,
    build_glossary_block,
    load_episode_context,
    load_glossary_terms,
    load_prompt_characters,
)
from app.jobs.handlers.style_store import (
    canonical_address_pairs,
    insert_new_glossary_terms,
    upsert_address_pairs,
)
from app.jobs.registry import register_job_handler
from app.llm import client as llm_client
from app.llm.schemas import AnalyzeResponse
from app.subs.tag_masking import plain_text

logger = logging.getLogger(__name__)


def _previous_synopsis(session, file: File) -> str | None:
    """Synopsis of the analyzed file that precedes this one — by parsed
    episode number when known, otherwise by path order (episode files sort
    lexicographically in practice)."""
    if file.episode_number is not None:
        row = session.execute(
            select(FileAnalysis.synopsis)
            .join(File, FileAnalysis.file_id == File.id)
            .where(File.project_id == file.project_id)
            .where(File.episode_number.isnot(None))
            .where(File.episode_number < file.episode_number)
            .order_by(File.episode_number.desc())
            .limit(1)
        ).first()
        if row:
            return row.synopsis

    row = session.execute(
        select(FileAnalysis.synopsis)
        .join(File, FileAnalysis.file_id == File.id)
        .where(File.project_id == file.project_id)
        .where(File.relative_path < file.relative_path)
        .order_by(File.relative_path.desc())
        .limit(1)
    ).first()
    return row.synopsis if row else None


@register_job_handler("analyze_script")
def analyze_script(
    payload: dict[str, Any],
    ctx: JobContext,
    progress: ProgressFn,
) -> JobResult:
    file_id: int = payload["file_id"]
    model: str = payload.get("model") or ctx.options.openai_model_better or ctx.options.openai_model_cheap
    now = datetime.utcnow().isoformat()

    progress(0.05, "Loading script")

    with SyncSessionLocal() as session:
        file = session.get(File, file_id)
        if file is None:
            return JobResult(status="failed", result=None,
                             error_code="FILE_NOT_FOUND",
                             error_message=f"File id={file_id} not found")
        project_id = file.project_id

        events = list(session.scalars(
            select(SubtitleEvent)
            .where(SubtitleEvent.file_id == file_id)
            .where(SubtitleEvent.event_type == "dialogue")
            .order_by(SubtitleEvent.line_index)
        ).all())

        script_lines = []
        script_texts = []
        for e in events:
            text = plain_text(e.source_text)
            if not text:
                continue
            speaker = f" ({e.name})" if e.name else ""
            script_lines.append(f"{e.line_index}{speaker}: {text}")
            script_texts.append(text)

        characters = load_prompt_characters(session, project_id)
        char_block = build_character_block(characters)
        prev_synopsis = _previous_synopsis(session, file)
        episode_line = load_episode_context(session, file)
        glossary_block = build_glossary_block(
            load_glossary_terms(session, project_id), script_texts,
        )
        pair_block = "\n".join(
            f"- {speaker} addresses {addressee}: {mode}"
            for speaker, addressee, mode in canonical_address_pairs(session, project_id)
        )

    if not script_lines:
        return JobResult(status="failed", result=None,
                         error_code="NO_EVENTS",
                         error_message=f"File id={file_id} has no dialogue events to analyze")

    progress(0.2, f"Building analysis prompt ({len(script_lines)} lines)")

    system_prompt = ctx.options.resolved_analyze_prompt().strip()

    user_parts = []
    if episode_line:
        user_parts.append(f"## Episode\n{episode_line}")
    if char_block:
        user_parts.append(f"## Characters\n{char_block}")
    if prev_synopsis:
        user_parts.append(f"## Previous Episode\n{prev_synopsis}")
    if glossary_block:
        user_parts.append(f"## Current Glossary\n{glossary_block}")
    if pair_block:
        user_parts.append(f"## Current Address Pairs\n{pair_block}")
    user_parts.append("## Script\n" + "\n".join(script_lines))
    user_message = "\n\n".join(user_parts)

    progress(0.35, f"Calling LLM ({model})")

    try:
        response, stats = llm_client.complete(
            task="analyze",
            model=model,
            system=system_prompt,
            user=user_message,
            schema=AnalyzeResponse,
            options=ctx.options,
            max_completion_tokens=8000,
            project_id=project_id,
            file_id=file_id,
        )
    except llm_client.LlmError as exc:
        return JobResult(status="failed", result=None,
                         error_code=exc.code, error_message=exc.message)

    progress(0.8, "Writing analysis")

    with SyncSessionLocal() as session:
        session.execute(delete(FileAnalysis).where(FileAnalysis.file_id == file_id))
        session.add(FileAnalysis(
            file_id=file_id,
            synopsis=response.synopsis,
            scenes_json=json.dumps([s.model_dump() for s in response.scenes]),
            tricky_lines_json=json.dumps([t.model_dump() for t in response.tricky_lines]),
            model=model,
            created_at=now,
        ))
        new_pairs = upsert_address_pairs(
            session, project_id, response.address_pairs, "llm", now,
            update_existing_mode=False,
        )
        new_terms = insert_new_glossary_terms(session, project_id, response.suggested_terms, "llm", now)
        session.commit()

    progress(1.0, "Done")
    return JobResult(
        status="succeeded",
        result={
            "scenes": len(response.scenes),
            "tricky_lines": len(response.tricky_lines),
            "new_address_pairs": new_pairs,
            "new_glossary_terms": new_terms,
            "response_mode": stats.response_mode,
        },
        error_code=None,
        error_message=None,
    )
