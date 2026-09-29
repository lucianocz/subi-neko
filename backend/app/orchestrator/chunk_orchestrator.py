"""Chunk-level orchestrator — drives the sequential translation pipeline.

Chunk state machine:

    pending               → translate_chunk   (dialogue only: gated on the
                                               previous dialogue chunk
                                               having been polished)
    translated            → validate_chunk
    validate_trans_failed → repair_chunk      (one attempt, then terminal)
    validated             → polish_chunk      (full-coverage quality pass)
    polished              → review_chunk_final
    needs_polish          → polish_chunk      (targeted re-pass, max 1)
    final_reviewed        → audit_chunk_final (dialogue only)
    audited               → complete          (auto)

Terminal: job_failed, validate_repair_failed (require user action).

Dialogue translation is serialized per file: dialogue chunk N waits until
dialogue chunk N-1 has been through its polish pass, so the rolling context
window carries the wording that actually ships rather than the cheap model's
draft. Polish always runs for a chunk that reaches "validated" (there is no
skip option, and even a chunk with nothing editable advances to "polished"),
so the gate cannot deadlock on a chunk that simply had no edits.

The sign, karaoke and song partitions are NOT gated — they carry no
conversational continuity, so serializing them would only cost wall clock.
They translate in parallel with each other and with the dialogue chain,
bounded by the job worker pool.

Stages other than translate are not gated either — review of chunk N runs
while chunk N+1 translates.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Awaitable, Callable

from sqlalchemy import select, update

from app.core.database import AsyncSessionLocal
from app.db import options as options_store
from app.db.models import SubtitleChunk, SubtitleEvent

logger = logging.getLogger(__name__)

EnqueueFn = Callable[..., Awaitable[Any]]

# Chunk status → next job type.
_CHUNK_TRANSITIONS: dict[str, str] = {
    "pending":                "translate_chunk",
    "translated":             "validate_chunk",
    "validate_trans_failed":  "repair_chunk",
    "validated":              "polish_chunk",
    "polished":               "review_chunk_final",
    "needs_polish":           "polish_chunk",
    "final_reviewed":         "audit_chunk_final",
}

# Terminal statuses that require user action — orchestrator must not enqueue anything.
CHUNK_TERMINAL_STATUSES = {"job_failed", "validate_repair_failed"}

# Statuses a chunk holds before its polish pass has produced final wording.
# While dialogue chunk N-1 is in one of these, dialogue chunk N must not
# translate: its context window would quote text that polish is about to
# rewrite. "needs_polish" is deliberately absent — the full-coverage pass has
# already run and only a handful of flagged lines get a targeted second pass.
_PRE_POLISH_STATUSES = {
    "pending",
    "translated",
    "validate_trans_failed",
    "validated",
}

# The only partition whose chunks depend on their predecessors' wording.
_GATED_CONTENT_TYPE = "dialogue"


async def orchestrate_chunks(
    file_id: int,
    project_id: int,
    enqueue_fn: EnqueueFn,
) -> bool | None:
    """Drive chunk pipeline for a file.

    Returns:
        True  — all chunks are complete
        False — still working (jobs are queued/running)
        None  — blocked; one or more chunks are in a terminal error state
                requiring user action (job_failed / validate_repair_failed)
    """
    async with AsyncSessionLocal() as session:
        chunks = (await session.scalars(
            select(SubtitleChunk)
            .where(SubtitleChunk.file_id == file_id)
            .order_by(SubtitleChunk.chunk_index)
        )).all()

    if not chunks:
        return False  # no chunks planned yet

    translate_karaoke: bool | None = None  # read lazily, only when needed

    any_auto_completed = False
    has_pending_work = False
    has_blocked = False
    # Status of the preceding chunk *within each content-type partition*.
    # Only the dialogue partition is gated, but tracking per partition keeps
    # the gate correct regardless of how plan_translation_chunks lays the
    # partitions out in chunk_index space.
    previous_status: dict[str, str] = {}

    for chunk in chunks:
        status = chunk.status
        content_type = chunk.content_type or _GATED_CONTENT_TYPE

        if status == "complete":
            previous_status[content_type] = status
            continue

        # --- Terminal statuses: require user action, stop processing ---
        if status in CHUNK_TERMINAL_STATUSES:
            has_blocked = True
            previous_status[content_type] = status
            continue

        # --- Karaoke skip: keep original \k-timed lines untouched ---
        if status == "pending" and content_type == "karaoke":
            if translate_karaoke is None:
                translate_karaoke = (
                    (await options_store.aget("TRANSLATE_KARAOKE", "0") or "").strip().lower()
                    in ("1", "true", "yes", "on")
                )
            if not translate_karaoke:
                await _skip_karaoke_chunk(file_id, chunk)
                any_auto_completed = True
                previous_status[content_type] = "complete"
                continue

        # Non-dialogue review intentionally remains deterministic-only.
        # Dialogue must pass the read-only semantic audit before completion.
        if status == "final_reviewed" and content_type != _GATED_CONTENT_TYPE:
            await _set_chunk_complete(file_id, chunk.chunk_index)
            any_auto_completed = True
            previous_status[content_type] = "complete"
            continue

        # --- successfully audited dialogue: auto-complete ---
        if status == "audited":
            await _set_chunk_complete(file_id, chunk.chunk_index)
            any_auto_completed = True
            previous_status[content_type] = "complete"
            continue

        has_pending_work = True

        # --- Sequential translate gating: a dialogue chunk translates only
        # after the previous dialogue chunk has been polished, so the rolling
        # context window quotes the shipped wording instead of a draft polish
        # is about to rewrite. Signs, karaoke and songs are ungated — they
        # have no conversational continuity to preserve. A terminal/failed
        # predecessor does not block either (its context is simply missing) —
        # otherwise one broken chunk would wedge the whole file. ---
        if (
            status == "pending"
            and content_type == _GATED_CONTENT_TYPE
            and previous_status.get(content_type) in _PRE_POLISH_STATUSES
        ):
            previous_status[content_type] = status
            continue

        # --- Standard transitions ---
        job_type = _CHUNK_TRANSITIONS.get(status)
        if job_type is None:
            logger.warning(
                "Chunk file_id=%d index=%d has unknown status '%s'",
                file_id, chunk.chunk_index, status,
            )
            previous_status[content_type] = status
            continue

        await _ensure_chunk_job(
            enqueue_fn, job_type,
            file_id, project_id, chunk.chunk_index,
        )
        previous_status[content_type] = status

    # Blocked chunks take priority when nothing else is progressing.
    # If there's still pending work alongside blocked chunks, keep the file
    # in processing state so other chunks can finish.
    if has_blocked:
        if has_pending_work or any_auto_completed:
            return False  # some chunks still running; file stays processing
        return None  # all remaining chunks are blocked → need user action
    # If we auto-completed some chunks and nothing else is pending,
    # all chunks are now complete
    if has_pending_work:
        return False
    if any_auto_completed:
        return True
    # All were already "complete" when we started
    return True


async def _ensure_chunk_job(
    enqueue_fn: EnqueueFn,
    job_type: str,
    file_id: int,
    project_id: int,
    chunk_index: int,
) -> None:
    dedupe_key = f"{job_type}:{file_id}:{chunk_index}"
    await enqueue_fn(
        job_type=job_type,
        project_id=project_id,
        payload={"file_id": file_id, "chunk_index": chunk_index},
        file_id=file_id,
        dedupe_key=dedupe_key,
    )


async def _set_chunk_complete(file_id: int, chunk_index: int) -> None:
    now = datetime.utcnow().isoformat()
    async with AsyncSessionLocal() as session:
        chunk = await session.scalar(
            select(SubtitleChunk)
            .where(SubtitleChunk.file_id == file_id)
            .where(SubtitleChunk.chunk_index == chunk_index)
        )
        if chunk is not None and chunk.status != "complete":
            chunk.status = "complete"
            chunk.updated_at = now
            await session.commit()
            logger.info(
                "Chunk file_id=%d index=%d → complete (orchestrator)",
                file_id, chunk_index,
            )


async def _skip_karaoke_chunk(file_id: int, chunk: SubtitleChunk) -> None:
    """TRANSLATE_KARAOKE is off: keep the original karaoke lines (with their
    per-syllable \\k timing intact) and complete the chunk without any LLM
    work. Rendering falls back to source_text when translated_text is NULL."""
    now = datetime.utcnow().isoformat()
    async with AsyncSessionLocal() as session:
        await session.execute(
            update(SubtitleEvent)
            .where(SubtitleEvent.file_id == file_id)
            .where(SubtitleEvent.event_type == "dialogue")
            .where(SubtitleEvent.content_type == "karaoke")
            .where(SubtitleEvent.line_index >= chunk.translate_from_line)
            .where(SubtitleEvent.line_index <= chunk.translate_to_line)
            .values(translation_status="skipped", updated_at=now)
        )
        fresh = await session.scalar(
            select(SubtitleChunk)
            .where(SubtitleChunk.file_id == file_id)
            .where(SubtitleChunk.chunk_index == chunk.chunk_index)
        )
        if fresh is not None and fresh.status == "pending":
            fresh.status = "complete"
            fresh.updated_at = now
        await session.commit()
    logger.info(
        "Chunk file_id=%d index=%d (karaoke) → complete without translation "
        "(TRANSLATE_KARAOKE off)",
        file_id, chunk.chunk_index,
    )
