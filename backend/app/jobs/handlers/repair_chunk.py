from __future__ import annotations

import logging

from datetime import datetime
from typing import Any

from sqlalchemy import select

from app.core.database import SyncSessionLocal
from app.db.models import File, ProjectCharacter, ProjectSpeaker, QaItem, SubtitleChunk, SubtitleEvent
from app.jobs.context import JobContext, JobResult, ProgressFn
from app.jobs.handlers.prompt_context import (
    StyleContext,
    build_character_block,
    build_glossary_block,
    build_style_block,
    build_speaker_identity_map,
    build_unmapped_speaker_block,
    load_glossary_terms,
    load_prompt_characters,
    load_style_context,
    load_unmapped_gendered_speakers,
)
from app.jobs.handlers.utils import allows_ai_edit
from app.jobs.registry import register_job_handler
from app.llm import client as llm_client
from app.llm.schemas import RepairResponse
from app.subs.tag_masking import MaskedLine, force_unmask, mask_line, plain_text, unmask_line

logger = logging.getLogger(__name__)

_CONTEXT_WINDOW = 2  # lines before and after each failed line


def _build_repair_block(
    rejected: list[dict],
    all_events_by_pos: list[dict],
    qa_errors: dict[int, list[str]],
    masked: dict[int, MaskedLine],
) -> str:
    """
    Build the user message block for repair.

    rejected: list of event dicts (line_index, source_text, translated_text, id)
    all_events_by_pos: all dialogue events in chunk, sorted by line_index, as dicts
    qa_errors: event_id → list of qa_type strings
    masked: line_index → MaskedLine of the source text
    """
    pos_map = {e["line_index"]: i for i, e in enumerate(all_events_by_pos)}
    total = len(all_events_by_pos)

    parts = []
    for ev in rejected:
        li = ev["line_index"]
        pos = pos_map.get(li)
        errors = qa_errors.get(ev["id"], [])

        lines = []
        lines.append(f"### FAILED line {li} — errors: {', '.join(errors) if errors else 'unknown'}")
        lines.append(f"  source: {masked[li].text}")
        if ev["translated_text"]:
            lines.append(f"  faulty: {plain_text(ev['translated_text'])}")

        # context before
        ctx_before = []
        if pos is not None:
            for i in range(max(0, pos - _CONTEXT_WINDOW), pos):
                ctx_ev = all_events_by_pos[i]
                text = ctx_ev["translated_text"] or ctx_ev["source_text"]
                ctx_before.append(f"  [CONTEXT {ctx_ev['line_index']}]: {plain_text(text)}")
        if ctx_before:
            lines.append("Context before:")
            lines.extend(ctx_before)

        # context after
        ctx_after = []
        if pos is not None:
            for i in range(pos + 1, min(total, pos + 1 + _CONTEXT_WINDOW)):
                ctx_ev = all_events_by_pos[i]
                text = ctx_ev["translated_text"] or ctx_ev["source_text"]
                ctx_after.append(f"  [CONTEXT {ctx_ev['line_index']}]: {plain_text(text)}")
        if ctx_after:
            lines.append("Context after:")
            lines.extend(ctx_after)

        parts.append("\n".join(lines))

    return "\n\n".join(parts)


@register_job_handler("repair_chunk")
def repair_chunk(
    payload: dict[str, Any],
    ctx: JobContext,
    progress: ProgressFn,
) -> JobResult:
    file_id: int = payload["file_id"]
    chunk_index: int = payload["chunk_index"]
    model: str = payload.get("model") or ctx.options.openai_model_better or ctx.options.openai_model_cheap
    now = datetime.utcnow().isoformat()

    progress(0.05, "Loading chunk definition")

    with SyncSessionLocal() as session:
        chunk = session.scalar(
            select(SubtitleChunk)
            .where(SubtitleChunk.file_id == file_id)
            .where(SubtitleChunk.chunk_index == chunk_index)
        )
        if chunk is None:
            return JobResult(status="failed", result=None,
                             error_code="CHUNK_NOT_FOUND",
                             error_message=f"Chunk {chunk_index} for file {file_id} not found")

        translate_from = chunk.translate_from_line
        translate_to = chunk.translate_to_line
        chunk_id = chunk.id
        content_type = chunk.content_type or "dialogue"
        file = session.get(File, file_id)
        project_id = file.project_id if file is not None else None
        characters: list[ProjectCharacter] = []
        unmapped_speakers: list[ProjectSpeaker] = []
        identities: dict[str, tuple[str | None, str | None]] = {}
        style: StyleContext | None = None
        if file is not None and content_type == "dialogue":
            characters = load_prompt_characters(session, file.project_id)
            unmapped_speakers = load_unmapped_gendered_speakers(session, file.project_id)
            identities = build_speaker_identity_map(session, file.project_id)
            style = load_style_context(session, file.project_id)

        glossary_terms = load_glossary_terms(session, project_id) if project_id else []

        # All events in chunk range, ordered (scoped to this chunk's own
        # content_type partition — partitions can interleave in line_index space)
        all_events = list(session.scalars(
            select(SubtitleEvent)
            .where(SubtitleEvent.file_id == file_id)
            .where(SubtitleEvent.line_index >= translate_from)
            .where(SubtitleEvent.line_index <= translate_to)
            .where(SubtitleEvent.event_type == "dialogue")
            .where(SubtitleEvent.content_type == content_type)
            .order_by(SubtitleEvent.line_index)
        ).all())

        all_events_data = [
            {
                "id": e.id,
                "line_index": e.line_index,
                "name": e.name,
                "source_text": e.source_text,
                "translated_text": e.translated_text,
                "translation_status": e.translation_status,
                "is_user_edited": e.is_user_edited,
                "is_locked": e.is_locked,
            }
            for e in all_events
        ]

        rejected_data = [
            e for e in all_events_data
            if e["translation_status"] == "rejected"
            and allows_ai_edit(e["is_user_edited"], e["is_locked"])
        ]

        if not rejected_data:
            for event in all_events:
                if event.translation_status == "rejected" and not allows_ai_edit(
                    event.is_user_edited, event.is_locked
                ):
                    event.translation_status = "validated"
                    event.updated_at = now
            chunk.status = "translated"
            chunk.repair_attempt_count = (chunk.repair_attempt_count or 0) + 1
            chunk.last_error_code = None
            chunk.last_error_message = None
            chunk.failed_job_type = None
            chunk.updated_at = now
            session.commit()
            return JobResult(status="succeeded", result={"repaired_events": 0},
                             error_code=None, error_message=None)

        rejected_ids = [e["id"] for e in rejected_data]

        # Load QA errors for rejected events
        qa_rows = list(session.scalars(
            select(QaItem)
            .where(QaItem.subtitle_event_id.in_(rejected_ids))
            .where(QaItem.is_resolved == 0)
        ).all())
        qa_errors: dict[int, list[str]] = {}
        for qa in qa_rows:
            qa_errors.setdefault(qa.subtitle_event_id, []).append(qa.qa_type)

        char_snapshot = list(characters)
        speaker_snapshot = list(unmapped_speakers)

    if not ctx.options.openai_api_key and not ctx.options.openai_api_base:
        return JobResult(status="failed", result=None,
                         error_code="OPENAI_API_KEY_MISSING",
                         error_message="OPENAI_API_KEY option is not configured")

    progress(0.2, f"Building repair prompt for {len(rejected_data)} rejected event(s)")

    system_prompt = ctx.options.resolved_repair_prompt().strip()

    masked: dict[int, MaskedLine] = {
        e["line_index"]: mask_line(e["source_text"]) for e in rejected_data
    }

    repair_block = _build_repair_block(rejected_data, all_events_data, qa_errors, masked)
    user_parts = []
    char_block = build_character_block(char_snapshot)
    speaker_block = build_unmapped_speaker_block(speaker_snapshot)
    if char_block:
        user_parts.append(f"## Characters\n{char_block}")
    if speaker_block:
        user_parts.append(f"## Unmapped Speakers\n{speaker_block}")
    if style is not None and style.has_content:
        speakers_present = {e["name"] for e in rejected_data if e.get("name")}
        style_block = build_style_block(style, speakers_present, identities)
        if style_block:
            user_parts.append(f"## Style\n{style_block}")
    glossary_block = build_glossary_block(
        glossary_terms, [e["source_text"] for e in rejected_data])
    if glossary_block:
        user_parts.append(
            "## Glossary\nEstablished translations — follow them exactly, "
            f"including vocative forms:\n{glossary_block}")
    user_parts.append(f"## Lines to Repair\n\n{repair_block}")
    user_message = "\n\n".join(user_parts)

    progress(0.4, f"Calling LLM ({model})")

    source_chars = sum(len(m.text) for m in masked.values())
    max_completion_tokens = llm_client.completion_budget(source_chars, len(rejected_data))

    try:
        response, stats = llm_client.complete(
            task="repair",
            model=model,
            system=system_prompt,
            user=user_message,
            schema=RepairResponse,
            options=ctx.options,
            max_completion_tokens=max_completion_tokens,
            project_id=project_id,
            file_id=file_id,
            chunk_id=chunk_id,
        )
    except llm_client.LlmError as exc:
        return JobResult(status="failed", result=None,
                         error_code=exc.code, error_message=exc.message)

    progress(0.7, "Unmasking and verifying markup")

    rejected_by_line: dict[int, int] = {e["line_index"]: e["id"] for e in rejected_data}
    repair_map: dict[int, str] = {}
    for item in response.repairs:
        if item.i not in rejected_by_line:
            continue
        final_text, errors = unmask_line(item.t, masked[item.i])
        if errors:
            logger.info("Repair marker verification failed for line %d: %s — best-effort assembly",
                        item.i, errors)
            final_text = force_unmask(item.t, masked[item.i])
        repair_map[item.i] = final_text

    progress(0.85, "Writing repaired translations")

    repaired_count = 0

    with SyncSessionLocal() as session:
        for line_index, event_id in rejected_by_line.items():
            text = repair_map.get(line_index)
            if text is None:
                logger.warning("No repair returned for line_index=%d (chunk %d, file %d)",
                               line_index, chunk_index, file_id)
                continue
            event = session.get(SubtitleEvent, event_id)
            if event is None or not allows_ai_edit(event.is_user_edited, event.is_locked):
                continue
            event.translated_text = text
            if event.original_ai_translated_text is None:
                event.original_ai_translated_text = text
            event.translation_status = "translated"
            event.updated_at = now
            repaired_count += 1

        # Reset chunk status so validator can run again.
        # Increment repair_attempt_count so validate_chunk knows a repair was attempted.
        chunk = session.scalar(
            select(SubtitleChunk)
            .where(SubtitleChunk.file_id == file_id)
            .where(SubtitleChunk.chunk_index == chunk_index)
        )
        if chunk is not None:
            chunk.status = "translated"
            chunk.repair_attempt_count = (chunk.repair_attempt_count or 0) + 1
            chunk.last_error_code = None
            chunk.last_error_message = None
            chunk.failed_job_type = None
            chunk.model = model
            chunk.updated_at = now

        session.commit()

    progress(1.0, "Done")
    return JobResult(
        status="succeeded",
        result={
            "repaired_events": repaired_count,
            "response_mode": stats.response_mode,
        },
        error_code=None,
        error_message=None,
    )
