"""Explicit project publishing — the single entry point into output generation.

Nothing in the orchestrators starts a publish. ``start_publish`` is called only
by ``POST /projects/{id}/publish``; the periodic sweep only *reconciles* a
publish run that died without recording its outcome (never retries it).

Concurrency: a Publish click atomically claims the project
(``publish_state = 'publishing'``, ``publish_attempt += 1``) with one conditional
UPDATE, so two clicks can never both start a run. The claimed attempt number is
part of the job's dedupe key, which is what lets every deliberate click start a
fresh run even though the job framework skips completed jobs with an old key.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Awaitable, Callable

from sqlalchemy import case, func, or_, select, update

from app.core.database import AsyncSessionLocal
from app.db.models import File, JobRecord, JobStatus, Project
from app.db.output_state import ACCEPTED_FOR_OUTPUT, PublishState

logger = logging.getLogger(__name__)

EnqueueFn = Callable[..., Awaitable[Any]]

PUBLISH_JOB_TYPE = "publish_project"


def publish_dedupe_key(project_id: int, attempt: int) -> str:
    return f"{PUBLISH_JOB_TYPE}:{project_id}:{attempt}"


class ProjectNotFound(Exception):
    pass


class PublishNotReady(Exception):
    def __init__(self, accepted: int, total: int) -> None:
        super().__init__(f"{accepted}/{total} files accepted")
        self.accepted = accepted
        self.total = total


@dataclass
class PublishStart:
    started: bool          # False → a run was already active (safe no-op)
    attempt: int


async def accepted_counts(session, project_ids: list[int]) -> dict[int, tuple[int, int]]:
    """{project_id: (accepted_files, total_files)} for the given projects."""
    if not project_ids:
        return {}
    rows = await session.execute(
        select(
            File.project_id,
            func.count(File.id),
            func.sum(case((File.status.in_(ACCEPTED_FOR_OUTPUT), 1), else_=0)),
        )
        .where(File.project_id.in_(project_ids))
        .group_by(File.project_id)
    )
    counts = {pid: (int(acc or 0), int(total)) for pid, total, acc in rows.all()}
    return {pid: counts.get(pid, (0, 0)) for pid in project_ids}


async def start_publish(project_id: int, enqueue_fn: EnqueueFn) -> PublishStart:
    """Claim the project and enqueue one publish run.

    Raises ProjectNotFound / PublishNotReady. Returns started=False (without
    enqueueing anything) when a run is already active.
    """
    from app.orchestrator.orchestrator import _get_project_lock

    # Same per-project lock the orchestrator and the reconciler use, so the
    # reconciler can never observe a claimed project whose job isn't enqueued yet.
    async with _get_project_lock(project_id):
        now = datetime.utcnow().isoformat()
        async with AsyncSessionLocal() as session:
            project = await session.get(Project, project_id)
            if project is None:
                raise ProjectNotFound()
            accepted, total = (await accepted_counts(session, [project_id]))[project_id]
            if total == 0 or accepted < total:
                raise PublishNotReady(accepted, total)

            claimed = await session.execute(
                update(Project)
                .where(
                    Project.id == project_id,
                    or_(
                        Project.publish_state.is_(None),
                        Project.publish_state != PublishState.PUBLISHING.value,
                    ),
                )
                .values(
                    publish_state=PublishState.PUBLISHING.value,
                    publish_attempt=Project.publish_attempt + 1,
                    publish_error=None,
                    publish_target_revision=Project.output_revision,
                    updated_at=now,
                )
                .execution_options(synchronize_session=False)
            )
            await session.commit()
            attempt = await session.scalar(
                select(Project.publish_attempt).where(Project.id == project_id)
            )
            if not claimed.rowcount:
                logger.info("Publish for project %d ignored: a run is already active", project_id)
                return PublishStart(started=False, attempt=attempt)

        try:
            await enqueue_fn(
                job_type=PUBLISH_JOB_TYPE,
                project_id=project_id,
                payload={"project_id": project_id, "publish_attempt": attempt},
                dedupe_key=publish_dedupe_key(project_id, attempt),
                max_attempts=1,  # no automatic retry: the user clicks Publish again
            )
        except Exception as exc:
            await _mark_failed(project_id, attempt, f"Could not start publish: {exc}")
            raise

    logger.info("Publish started for project %d (attempt %d)", project_id, attempt)
    return PublishStart(started=True, attempt=attempt)


async def _mark_failed(project_id: int, attempt: int, message: str) -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(
            update(Project)
            .where(Project.id == project_id, Project.publish_attempt == attempt)
            .values(
                publish_state=PublishState.FAILED.value,
                publish_error=message[:2000],
                updated_at=datetime.utcnow().isoformat(),
            )
            .execution_options(synchronize_session=False)
        )
        await session.commit()


async def reconcile_publish_state() -> list[int]:
    """Fail any run recorded as 'publishing' whose job is no longer queued or
    running (handler crashed, job cancelled by a pause, process killed after the
    row was marked failed…). The user retries by clicking Publish; nothing here
    re-enqueues. Returns the project ids that were changed."""
    from app.orchestrator.orchestrator import _get_project_lock

    async with AsyncSessionLocal() as session:
        project_ids = list((await session.scalars(
            select(Project.id).where(Project.publish_state == PublishState.PUBLISHING.value)
        )).all())

    changed: list[int] = []
    for pid in project_ids:
        async with _get_project_lock(pid):
            async with AsyncSessionLocal() as session:
                project = await session.get(Project, pid)
                if project is None or project.publish_state != PublishState.PUBLISHING.value:
                    continue
                attempt = project.publish_attempt
                job = await session.scalar(
                    select(JobRecord).where(JobRecord.dedupe_key == publish_dedupe_key(pid, attempt))
                )
                if job is not None and job.status in (
                    JobStatus.QUEUED.value, JobStatus.RUNNING.value,
                ):
                    continue
                message = (
                    (job.error_message if job is not None else None)
                    or "Publish was interrupted before it finished"
                )
            await _mark_failed(pid, attempt, message)
            logger.warning("Publish run %d for project %d reconciled to failed: %s",
                           attempt, pid, message)
            changed.append(pid)
    return changed
