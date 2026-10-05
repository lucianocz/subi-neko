"""Orchestrator entry points — ties project/file/chunk orchestration together."""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from sqlalchemy import select

from app.core.database import AsyncSessionLocal
from app.db.models import JobRecord, Project, ProjectStatus

from app.ws.connection_manager import connection_manager
from app.orchestrator.project_orchestrator import orchestrate_project
from app.orchestrator.file_orchestrator import orchestrate_file
from app.orchestrator.publish import PUBLISH_JOB_TYPE, reconcile_publish_state

logger = logging.getLogger(__name__)

EnqueueFn = Callable[..., Awaitable[Any]]

# Project statuses that need orchestration
_ACTIVE_PROJECT_STATUSES = frozenset({
    ProjectStatus.NEW.value,
    ProjectStatus.DISCOVERING.value,
    ProjectStatus.CONTEXT_REVIEW.value,
    ProjectStatus.PROCESSING.value,
    ProjectStatus.REVIEW_REQUIRED.value,
})

# Per-project locks to prevent concurrent orchestration
_project_locks: dict[int, asyncio.Lock] = {}


def _get_project_lock(project_id: int) -> asyncio.Lock:
    if project_id not in _project_locks:
        _project_locks[project_id] = asyncio.Lock()
    return _project_locks[project_id]


async def orchestrate_on_job_complete(
    job_id: int,
    enqueue_fn: EnqueueFn,
) -> None:
    """Called after a job completes (success or fail). Triggers orchestration
    for the affected project/file."""
    async with AsyncSessionLocal() as session:
        record = await session.get(JobRecord, job_id)
        if record is None:
            return
        project_id = record.project_id
        file_id = record.file_id
        job_type = record.job_type
        job_status = record.status

    logger.info(
        "Job id=%d type=%s status=%s completed — triggering orchestration for project_id=%d",
        job_id, job_type, job_status, project_id,
    )

    try:
        # Serialize orchestration per project to avoid race conditions
        lock = _get_project_lock(project_id)
        async with lock:
            await orchestrate_project(project_id, enqueue_fn)
    except Exception:
        logger.exception(
            "Orchestration failed after job id=%d (project=%d, file=%s)",
            job_id, project_id, file_id,
        )

    if job_type == PUBLISH_JOB_TYPE:
        # The handler records its own outcome; this only catches a run that
        # died without doing so (never retries — the user clicks Publish again).
        try:
            await reconcile_publish_state()
        except Exception:
            logger.exception("Publish reconciliation failed after job id=%d", job_id)


async def sweep_all_projects(enqueue_fn: EnqueueFn) -> None:
    """Periodic safety sweep — find all active projects and orchestrate them.

    Publishing is never started here: the sweep only reconciles a publish run
    that died without recording its outcome (output is explicit-only)."""
    try:
        for pid in await reconcile_publish_state():
            await connection_manager.broadcast("project_updated", {"project_id": pid})
    except Exception:
        logger.exception("Sweep: publish reconciliation failed")

    async with AsyncSessionLocal() as session:
        project_ids = (await session.scalars(
            select(Project.id).where(
                Project.status.in_(_ACTIVE_PROJECT_STATUSES),
                Project.is_paused == False,  # noqa: E712
            )
        )).all()

    if not project_ids:
        return

    logger.debug("Sweep: orchestrating %d active project(s)", len(project_ids))

    for pid in project_ids:
        try:
            lock = _get_project_lock(pid)
            async with lock:
                await orchestrate_project(pid, enqueue_fn)
        except Exception:
            logger.exception("Sweep: orchestration failed for project id=%d", pid)


_sweep_task: asyncio.Task | None = None


async def start_sweep_loop(
    enqueue_fn: EnqueueFn,
    interval_seconds: float = 30.0,
) -> None:
    """Start the periodic sweep as a background asyncio task."""
    global _sweep_task

    async def _loop() -> None:
        logger.info("Orchestrator sweep loop started (interval=%.0fs)", interval_seconds)
        while True:
            await asyncio.sleep(interval_seconds)
            try:
                await sweep_all_projects(enqueue_fn)
            except Exception:
                logger.exception("Orchestrator sweep error")

    _sweep_task = asyncio.create_task(_loop(), name="orchestrator-sweep")


async def stop_sweep_loop() -> None:
    """Cancel the periodic sweep task."""
    global _sweep_task
    if _sweep_task is not None:
        _sweep_task.cancel()
        try:
            await _sweep_task
        except asyncio.CancelledError:
            pass
        _sweep_task = None
        logger.info("Orchestrator sweep loop stopped")
