"""Project-level orchestrator — drives the overall project lifecycle."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Awaitable, Callable

from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.core.database import AsyncSessionLocal
from app.db.models import (
    File,
    FileBlockingReason,
    FileQualityMetric,
    FileStatus,
    Project,
    ProjectStatus,
    Subtitle,
)
from app.db import options as options_store
from app.db.models import ProjectStyleBible
from app.jobs.handlers.compute_file_metrics import fingerprint_of, metrics_fingerprint_stmt
from app.orchestrator.context_status import compute_context_status
from app.orchestrator.file_orchestrator import _job_failed_permanently, orchestrate_file

logger = logging.getLogger(__name__)

EnqueueFn = Callable[..., Awaitable[Any]]

# File statuses that mean "done for project-completion purposes"
_TERMINAL_FILE_STATUSES = frozenset({
    FileStatus.COMPLETED.value,
    FileStatus.FAILED.value,
    FileStatus.PAUSED.value,
    FileStatus.REVIEW_REQUIRED.value,
    FileStatus.ACCEPTED.value,
})


async def orchestrate_project(project_id: int, enqueue_fn: EnqueueFn) -> None:
    """Reconcile project-level state and drive files forward."""
    async with AsyncSessionLocal() as session:
        project = await session.get(
            Project, project_id,
            options=[selectinload(Project.files)],
        )
        if project is None:
            logger.warning("orchestrate_project: project_id=%d not found", project_id)
            return

        status = project.status
        files = list(project.files)

    logger.debug(
        "orchestrate_project: id=%d status=%s is_paused=%s files=%d",
        project_id, status, project.is_paused, len(files),
    )

    if status in (ProjectStatus.COMPLETED.value, ProjectStatus.FAILED.value):
        return

    if project.is_paused:
        logger.debug("orchestrate_project: id=%d is paused, skipping", project_id)
        return

    if status == ProjectStatus.NEW.value:
        await _handle_new(project_id, enqueue_fn)
    elif status == ProjectStatus.DISCOVERING.value:
        await _handle_discovering(project_id, files, enqueue_fn)
    elif status == ProjectStatus.CONTEXT_REVIEW.value:
        await _handle_context_review(project_id, files, enqueue_fn)
    elif status == ProjectStatus.PROCESSING.value:
        await _handle_processing(project_id, files, enqueue_fn)
    elif status == ProjectStatus.REVIEW_REQUIRED.value:
        await _handle_review_required(project_id, files, enqueue_fn)


# ------------------------------------------------------------------
# Status handlers
# ------------------------------------------------------------------

async def _handle_new(project_id: int, enqueue_fn: EnqueueFn) -> None:
    logger.info("Project id=%d (new) → enqueueing scan_project", project_id)
    dedupe_key = f"scan_project:{project_id}"
    await enqueue_fn(
        job_type="scan_project",
        project_id=project_id,
        payload={"project_id": project_id},
        dedupe_key=dedupe_key,
    )


async def _handle_discovering(
    project_id: int, files: list[File], enqueue_fn: EnqueueFn,
) -> None:
    # Drive each file in new/discovering state
    discovering = [f for f in files if f.status in (FileStatus.NEW.value, FileStatus.DISCOVERING.value)]
    logger.debug("Project id=%d (discovering): %d file(s) still in discovery", project_id, len(discovering))
    for f in discovering:
        await orchestrate_file(f.id, enqueue_fn)

    # Translation context is built here, one component at a time:
    # aggregate speakers → infer the speaker→character mapping → generate the
    # style bible. A permanently failed component (job FAILED after all
    # attempts) blocks the project in discovering — the failure surfaces in
    # the context-status panel and is retried explicitly by the user; the
    # perma-fail guards keep the sweep from re-enqueueing it forever.
    async with AsyncSessionLocal() as session:
        project = await session.get(Project, project_id, options=[selectinload(Project.files)])
        if project is None:
            return
        fresh_files = list(project.files)
        mapping_status = project.speaker_mapping_status

        files_with_subs = await session.scalar(
            select(func.count())
            .select_from(Subtitle)
            .join(File, Subtitle.file_id == File.id)
            .where(File.project_id == project_id)
        )

    # Context builds from the whole series: wait until every file is past
    # discovery so speaker line counts and samples cover all episodes.
    any_still_discovering = any(
        f.status in (FileStatus.NEW.value, FileStatus.DISCOVERING.value)
        for f in fresh_files
    )
    if any_still_discovering:
        return

    if mapping_status == "awaiting_discovery":
        if files_with_subs > 0:
            await enqueue_fn(
                job_type="aggregate_speakers",
                project_id=project_id,
                payload={"project_id": project_id},
                dedupe_key=f"aggregate_speakers:{project_id}",
            )
        return

    if mapping_status == "aggregated":
        dedupe_key = f"infer_character_mapping:{project_id}"
        if not await _job_failed_permanently(dedupe_key):
            await enqueue_fn(
                job_type="infer_character_mapping",
                project_id=project_id,
                payload={"project_id": project_id},
                dedupe_key=dedupe_key,
            )
        return

    if mapping_status != "complete":
        return

    if not await _ensure_style_bible(project_id, enqueue_fn):
        return

    # All context components built — hand over to the user for review.
    # (A grandfathered/approved project skips straight to processing.)
    status = await compute_context_status(project_id)
    if status is None:
        return
    if status["state"] == "approved":
        await _set_project_status(project_id, ProjectStatus.PROCESSING.value)
    elif status["state"] == "ready_for_review":
        await _set_project_status(project_id, ProjectStatus.CONTEXT_REVIEW.value)


async def pick_style_bible_sample_file_id(project_id: int) -> int | None:
    """Lowest-episode file with extracted subtitles — the sample the style
    bible is generated from. Shared with the context-retry endpoint."""
    async with AsyncSessionLocal() as session:
        return await session.scalar(
            select(File.id)
            .join(Subtitle, Subtitle.file_id == File.id)
            .where(File.project_id == project_id)
            .order_by(
                File.episode_number.is_(None),
                File.episode_number,
                File.relative_path,
            )
            .limit(1)
        )


async def _ensure_style_bible(project_id: int, enqueue_fn: EnqueueFn) -> bool:
    """Project-level style-bible gate. True when the gate is satisfied
    (bible exists, or generation not required); False while a generation job
    is pending/running or has permanently failed."""
    async with AsyncSessionLocal() as session:
        has_bible = await session.scalar(
            select(func.count())
            .select_from(ProjectStyleBible)
            .where(ProjectStyleBible.project_id == project_id)
        ) > 0

    if has_bible:
        return True

    require_bible = (
        (await options_store.aget("REQUIRE_STYLE_BIBLE", "1") or "").strip().lower()
        in ("1", "true", "yes", "on")
    )
    if not require_bible:
        return True

    dedupe_key = f"generate_style_bible:{project_id}"
    if await _job_failed_permanently(dedupe_key):
        return False

    sample_file_id = await pick_style_bible_sample_file_id(project_id)
    if sample_file_id is None:
        return False
    await enqueue_fn(
        job_type="generate_style_bible",
        project_id=project_id,
        payload={"project_id": project_id, "sample_file_id": sample_file_id},
        file_id=None,
        dedupe_key=dedupe_key,
    )
    return False


async def _handle_context_review(
    project_id: int, files: list[File], enqueue_fn: EnqueueFn,
) -> None:
    # Files added by a rescan still discover while the gate is open.
    for f in files:
        if f.status in (FileStatus.NEW.value, FileStatus.DISCOVERING.value):
            await orchestrate_file(f.id, enqueue_fn)

    # Self-heal: the approve endpoint sets processing itself, but if approval
    # was recorded without the transition (crash, migration), catch up here.
    async with AsyncSessionLocal() as session:
        project = await session.get(Project, project_id)
        if project is None:
            return
        approved = project.context_approved_at is not None
    if approved:
        await _set_project_status(project_id, ProjectStatus.PROCESSING.value)


async def _handle_processing(
    project_id: int, files: list[File], enqueue_fn: EnqueueFn,
) -> None:
    # Drive each non-terminal, non-inert file. Inert = ready but the user
    # hasn't clicked Translate yet — the file waits for its manual start.
    for f in files:
        if f.status in _TERMINAL_FILE_STATUSES or _is_inert(f):
            continue
        await orchestrate_file(f.id, enqueue_fn)

    await _ensure_file_metrics(project_id, files, enqueue_fn)

    # Re-read to check completion
    async with AsyncSessionLocal() as session:
        project = await session.get(Project, project_id, options=[selectinload(Project.files)])
        if project is None:
            return
        files = list(project.files)

    # Error-parked (waiting) files need user action just like review files:
    # they settle the project for the review transition — one stuck episode
    # must not hide eleven reviewable ones — but they always block COMPLETED.
    all_settled = all(
        f.status in _TERMINAL_FILE_STATUSES
        or f.status == FileStatus.WAITING.value
        or _is_inert(f)
        for f in files
    )
    if not all_settled:
        return

    needs_user = any(
        f.status in (FileStatus.REVIEW_REQUIRED.value, FileStatus.WAITING.value)
        for f in files
    )
    if needs_user:
        await _set_project_status(project_id, ProjectStatus.REVIEW_REQUIRED.value)
        return

    # Untranslated episodes remain — the project honestly stays in
    # processing until the user starts (or removes) them.
    if any(_is_inert(f) for f in files):
        return

    await _set_project_status(project_id, ProjectStatus.COMPLETED.value)


def _is_inert(f: File) -> bool:
    """Ready but never started: waiting for the user's Translate action."""
    return (
        f.status == FileStatus.READY.value
        and f.translation_requested_at is None
    )


async def _handle_review_required(
    project_id: int, files: list[File], enqueue_fn: EnqueueFn,
) -> None:
    # Re-read files to get current state
    async with AsyncSessionLocal() as session:
        current_files = (await session.scalars(
            select(File).where(File.project_id == project_id)
        )).all()

    await _ensure_file_metrics(project_id, list(current_files), enqueue_fn)

    # Waiting files with a partial block and files reopened by a retranslation
    # keep progressing even while a sibling holds the project in review.
    for f in current_files:
        if (
            f.status == FileStatus.WAITING.value
            or (f.status not in _TERMINAL_FILE_STATUSES and not _is_inert(f))
        ):
            await orchestrate_file(f.id, enqueue_fn)

    # Symmetric with _handle_processing: waiting files hold the project in
    # review_required (they need user action), never bounce it back to
    # processing.
    needs_user = any(
        f.status in (FileStatus.REVIEW_REQUIRED.value, FileStatus.WAITING.value)
        for f in current_files
    )
    if not needs_user:
        await _set_project_status(project_id, ProjectStatus.PROCESSING.value)
        # Re-drive files
        for f in current_files:
            if f.status not in _TERMINAL_FILE_STATUSES:
                await orchestrate_file(f.id, enqueue_fn)


async def _ensure_file_metrics(
    project_id: int, files: list[File], enqueue_fn: EnqueueFn,
) -> None:
    """Keep each file's quality snapshot current with the pipeline output.

    Independent of file status (muxing included): any file whose translation
    was requested and that has translated dialogue gets a snapshot, refreshed
    whenever the fingerprint of its metric inputs differs from the stored one.
    Runs after every job completion, i.e. at chunk-stage checkpoints, and the
    fingerprint makes it a no-op when nothing relevant changed. A permanently
    failed compute job is skipped so it can't loop through the sweep."""
    candidates = [f for f in files if f.translation_requested_at is not None]
    if not candidates:
        return

    async with AsyncSessionLocal() as session:
        stored = dict((await session.execute(
            select(FileQualityMetric.file_id, FileQualityMetric.source_fingerprint)
            .where(FileQualityMetric.project_id == project_id)
        )).all())
        current: dict[int, tuple] = {}
        for f in candidates:
            row = (await session.execute(metrics_fingerprint_stmt(f.id))).one()
            if row[0]:  # no translated dialogue yet -> metrics stay unavailable
                current[f.id] = row

    for f in candidates:
        row = current.get(f.id)
        if row is None:
            continue
        if f.id in stored and stored[f.id] == fingerprint_of(row):
            continue
        dedupe_key = f"compute_file_metrics:{f.id}"
        if await _job_failed_permanently(dedupe_key):
            continue
        await enqueue_fn(
            job_type="compute_file_metrics",
            project_id=project_id,
            payload={"file_id": f.id},
            file_id=f.id,
            dedupe_key=dedupe_key,
        )


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

async def _set_project_status(project_id: int, status: str) -> None:
    now = datetime.utcnow().isoformat()
    async with AsyncSessionLocal() as session:
        project = await session.get(Project, project_id)
        if project is None:
            return
        old = project.status
        project.status = status
        project.updated_at = now
        await session.commit()
    logger.info("Project id=%d: %s → %s", project_id, old, status)
