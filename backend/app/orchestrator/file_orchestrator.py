"""File-level orchestrator — state machine for individual file processing."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any, Awaitable, Callable

from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.core.database import AsyncSessionLocal
from app.db import options as options_store
from app.db.models import (
    File,
    FileAnalysis,
    FileBlockingReason,
    FileStatus,
    JobRecord,
    JobStatus,
    Project,
    QaItem,
    Subtitle,
    SubtitleChunk,
    SubtitleEvent,
    SubtitleStyle,
    file_subtitle_styles,
)
from app.orchestrator.chunk_orchestrator import CHUNK_TERMINAL_STATUSES, orchestrate_chunks

logger = logging.getLogger(__name__)

EnqueueFn = Callable[..., Awaitable[Any]]


async def orchestrate_file(file_id: int, enqueue_fn: EnqueueFn) -> None:
    """Reconcile state for a single file and enqueue the next valid job."""
    async with AsyncSessionLocal() as session:
        file = await session.get(
            File, file_id,
            options=[selectinload(File.project)],
        )
        if file is None:
            logger.warning("orchestrate_file: file_id=%d not found", file_id)
            return

        project = file.project
        status = file.status

    logger.debug("orchestrate_file: id=%d status=%s project_paused=%s", file_id, status, project.is_paused)

    # Terminal / no-action statuses
    if status in (FileStatus.COMPLETED.value, FileStatus.PAUSED.value, FileStatus.FAILED.value):
        return

    # Check project-level pause
    if project.is_paused:
        return

    if status == FileStatus.NEW.value:
        await _handle_new(file_id, project.id, enqueue_fn)
    elif status == FileStatus.DISCOVERING.value:
        await _handle_discovering(file_id, project, enqueue_fn)
    elif status == FileStatus.WAITING.value:
        await _handle_waiting(file_id, project, enqueue_fn)
    elif status == FileStatus.READY.value:
        await _handle_ready(file_id, project.id, enqueue_fn)
    elif status == FileStatus.PROCESSING.value:
        await _handle_processing(file_id, project.id, enqueue_fn)
    elif status == FileStatus.REVIEW_REQUIRED.value:
        await _handle_review_required(file_id, project.id, enqueue_fn)
    elif status == FileStatus.MUXING.value:
        # Retired status (output is a project-level, explicit Publish now):
        # nothing produces it, but heal a stray row back to the accepted state
        # it really represents instead of leaving it parked.
        await _set_file_status(file_id, FileStatus.ACCEPTED.value, None)


# ------------------------------------------------------------------
# Status handlers
# ------------------------------------------------------------------

async def _handle_new(file_id: int, project_id: int, enqueue_fn: EnqueueFn) -> None:
    await _ensure_file_job(enqueue_fn, "inspect_mkv", file_id, project_id)


async def _handle_discovering(
    file_id: int, project: Project, enqueue_fn: EnqueueFn,
) -> None:
    async with AsyncSessionLocal() as session:
        file = await session.get(File, file_id)
        if file is None:
            return

        has_track = file.subtitle_track_index is not None

        has_subtitle = await session.scalar(
            select(func.count()).select_from(Subtitle).where(Subtitle.file_id == file_id)
        ) > 0

    if not has_track:
        await _ensure_file_job(enqueue_fn, "inspect_mkv", file_id, project.id)
        return

    if not has_subtitle:
        await _ensure_file_job(enqueue_fn, "extract_subtitles", file_id, project.id)
        return

    # Subtitles extracted → ready. The file then waits for the project-level
    # context approval and its own Translate action (see _handle_ready);
    # speaker mapping is enforced by the project's context gate, not here.
    await _set_file_status(file_id, FileStatus.READY.value, None)


async def _handle_waiting(
    file_id: int, project: Project, enqueue_fn: EnqueueFn,
) -> None:
    async with AsyncSessionLocal() as session:
        file = await session.get(File, file_id)
        if file is None:
            return
        blocking_reason = file.blocking_reason

    # Partial-block: one or more chunks need user action but others may still
    # be progressing (e.g. chunk 3 is validate_repair_failed while chunk 0
    # is polished and waiting for its final review).  Keep scheduling
    # work for the non-blocked chunks so the pipeline doesn't stall.
    if blocking_reason in (
        FileBlockingReason.TRANSLATION_FAILED.value,
        FileBlockingReason.VALIDATION_FAILED.value,
    ):
        all_complete = await orchestrate_chunks(file_id, project.id, enqueue_fn)
        await _handle_chunks_result(file_id, project.id, enqueue_fn, all_complete)
        return

    # All other blocking reasons: wait for user/manual action
    # (user_review_required, subtitle_missing, subtitle_parse_failed,
    #  analysis_failed — retried via the file's Translate action —,
    #  mux_failed, paused)


async def _handle_ready(file_id: int, project_id: int, enqueue_fn: EnqueueFn) -> None:
    async with AsyncSessionLocal() as session:
        file = await session.get(File, file_id)
        project = await session.get(Project, project_id)
        if file is None or project is None:
            return

        # Both quality gates must be open before any work starts: the
        # project's translation context approved, and this file's Translate
        # action clicked. Until then the file is inert (endpoints guard this
        # too — this keeps the sweep from starting anything on its own).
        if project.context_approved_at is None or file.translation_requested_at is None:
            return

        # Check fonts
        unchecked_fonts = await session.scalar(
            select(func.count())
            .select_from(SubtitleStyle)
            .join(file_subtitle_styles, file_subtitle_styles.c.subtitle_style_id == SubtitleStyle.id)
            .where(
                file_subtitle_styles.c.file_id == file_id,
                SubtitleStyle.font_check_status == "unchecked",
            )
        )

        # Check chunks
        chunk_count = await session.scalar(
            select(func.count())
            .select_from(SubtitleChunk)
            .where(SubtitleChunk.file_id == file_id)
        )

        has_analysis = await session.scalar(
            select(func.count())
            .select_from(FileAnalysis)
            .where(FileAnalysis.file_id == file_id)
        ) > 0

    if unchecked_fonts > 0:
        await _ensure_file_job(enqueue_fn, "resolve_style_fonts", file_id, project_id)
        return

    if chunk_count == 0:
        await _ensure_file_job(enqueue_fn, "plan_translation_chunks", file_id, project_id)
        return

    # Script analysis gate (per file) — hard: translation never starts
    # without the analysis context. A permanently failed job parks the file
    # for the user, who retries via the Translate action.
    if not has_analysis:
        dedupe_key = f"analyze_script:{file_id}"
        if await _job_failed_permanently(dedupe_key):
            await _set_file_status(
                file_id,
                FileStatus.WAITING.value,
                FileBlockingReason.ANALYSIS_FAILED.value,
            )
            return
        await enqueue_fn(
            job_type="analyze_script",
            project_id=project_id,
            payload={"file_id": file_id},
            file_id=file_id,
            dedupe_key=dedupe_key,
        )
        return

    # All prerequisites met → processing
    await _set_file_status(file_id, FileStatus.PROCESSING.value, None)


async def _job_failed_permanently(dedupe_key: str) -> bool:
    """True when the job behind dedupe_key has run and failed — used by the
    soft gates above so a broken LLM/config can't wedge files in READY (the
    orchestrator would otherwise re-enqueue the failed job every sweep)."""
    async with AsyncSessionLocal() as session:
        status = await session.scalar(
            select(JobRecord.status).where(JobRecord.dedupe_key == dedupe_key)
        )
    return status == JobStatus.FAILED.value


async def _handle_processing(
    file_id: int, project_id: int, enqueue_fn: EnqueueFn,
) -> None:
    all_complete = await orchestrate_chunks(file_id, project_id, enqueue_fn)
    await _handle_chunks_result(file_id, project_id, enqueue_fn, all_complete)


async def _handle_chunks_result(
    file_id: int,
    project_id: int,
    enqueue_fn: EnqueueFn,
    all_complete: bool | None,
) -> None:
    if all_complete is None:
        # One or more chunks are in a terminal error state — determine blocking reason.
        await _set_file_blocked_by_chunks(file_id)
        return

    if not all_complete:
        return

    # All chunks complete — risk-based acceptance:
    #   fully_clean (default): auto-accept when zero unresolved QA items of ANY
    #                          severity remain; anything flagged → human review
    #   no_blockers:           auto-accept unless a blocker-severity item remains
    #   manual:                always require an explicit accept
    policy = (await options_store.aget("AUTO_ACCEPT_POLICY", "fully_clean") or "fully_clean").strip().lower()
    if policy in ("fully_clean", "no_blockers"):
        async with AsyncSessionLocal() as session:
            query = (
                select(func.count())
                .select_from(QaItem)
                .where(QaItem.file_id == file_id, QaItem.is_resolved == 0)
            )
            if policy == "no_blockers":
                query = query.where(QaItem.severity == "blocker")
            unresolved = await session.scalar(query)
        if unresolved == 0:
            logger.info("File id=%d clean under policy '%s' — auto-accepting", file_id, policy)
            await finalize_accepted_file(file_id, project_id, enqueue_fn)
            return

    await _set_file_status(
        file_id,
        FileStatus.REVIEW_REQUIRED.value,
        FileBlockingReason.USER_REVIEW_REQUIRED.value,
    )


async def finalize_accepted_file(file_id: int, project_id: int, enqueue_fn: EnqueueFn) -> None:
    """Shared acceptance path (auto-accept and the accept endpoint): the
    translations are final — feed them into the translation memory, let the
    style bible learn from the episode, and mark the file accepted. Acceptance
    never produces output: once every file is accepted the project's output
    state becomes READY and the user publishes explicitly (orchestrator/publish).
    TM/bible steps are best-effort and never block the acceptance."""
    try:
        written = await asyncio.to_thread(_populate_translation_memory_sync, project_id, file_id)
        logger.info("TM populated from file %d: %d entries", file_id, written)
    except Exception:
        logger.exception("Translation memory population failed for file %d", file_id)
    try:
        await enqueue_fn(
            job_type="update_style_bible",
            project_id=project_id,
            payload={"project_id": project_id, "file_id": file_id},
            file_id=file_id,
            dedupe_key=f"update_style_bible:{project_id}:{file_id}",
        )
    except Exception:
        logger.exception("Failed to enqueue style bible update for file %d", file_id)

    now = datetime.utcnow().isoformat()
    async with AsyncSessionLocal() as session:
        file = await session.get(File, file_id)
        if file is None or file.project_id != project_id:
            return

        file.status = FileStatus.ACCEPTED.value
        file.blocking_reason = None
        file.updated_at = now
        await session.commit()

    logger.info("File id=%d accepted (project id=%d)", file_id, project_id)


def _populate_translation_memory_sync(project_id: int, file_id: int) -> int:
    from app.core.database import SyncSessionLocal
    from app.subs import translation_memory as tm

    with SyncSessionLocal() as session:
        written = tm.populate_from_file(session, project_id, file_id)
        session.commit()
    return written


async def _handle_review_required(
    file_id: int, project_id: int, enqueue_fn: EnqueueFn,
) -> None:
    # Stay in review until the user explicitly accepts the file.
    await _set_file_status(
        file_id,
        FileStatus.REVIEW_REQUIRED.value,
        FileBlockingReason.USER_REVIEW_REQUIRED.value,
    )


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

async def _ensure_file_job(
    enqueue_fn: EnqueueFn,
    job_type: str,
    file_id: int,
    project_id: int,
) -> None:
    dedupe_key = f"{job_type}:{file_id}"
    logger.debug("_ensure_file_job: type=%s file_id=%d key=%s", job_type, file_id, dedupe_key)
    await enqueue_fn(
        job_type=job_type,
        project_id=project_id,
        payload={"file_id": file_id},
        file_id=file_id,
        dedupe_key=dedupe_key,
    )


async def _set_file_status(
    file_id: int,
    status: str,
    blocking_reason: str | None,
) -> None:
    now = datetime.utcnow().isoformat()
    async with AsyncSessionLocal() as session:
        file = await session.get(File, file_id)
        if file is None:
            return
        old = file.status
        file.status = status
        file.blocking_reason = blocking_reason
        file.updated_at = now
        if status == FileStatus.COMPLETED.value:
            file.completed_at = now
        await session.commit()
    logger.info("File id=%d: %s → %s (reason=%s)", file_id, old, status, blocking_reason)


async def _set_file_blocked_by_chunks(file_id: int) -> None:
    """Set file to waiting when chunks are in a terminal error state.

    Uses the most severe blocking reason: validation_failed > translation_failed.
    """
    async with AsyncSessionLocal() as session:
        blocked_chunks = (await session.scalars(
            select(SubtitleChunk)
            .where(SubtitleChunk.file_id == file_id)
            .where(SubtitleChunk.status.in_(list(CHUNK_TERMINAL_STATUSES)))
        )).all()

    has_validate_failed = any(c.status == "validate_repair_failed" for c in blocked_chunks)
    reason = (
        FileBlockingReason.VALIDATION_FAILED.value
        if has_validate_failed
        else FileBlockingReason.TRANSLATION_FAILED.value
    )
    await _set_file_status(file_id, FileStatus.WAITING.value, reason)
