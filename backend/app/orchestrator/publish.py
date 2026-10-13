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

import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

from sqlalchemy import case, func, or_, select, update

from app.core.database import AsyncSessionLocal
from app.core.media_paths import SourcePathError, resolve_source_path_parts
from app.db.archive_state import ProjectArchived, ProjectNotFound
from app.db.models import File, JobRecord, JobStatus, Project
from app.db.output_state import ACCEPTED_FOR_OUTPUT, PublishState

logger = logging.getLogger(__name__)

EnqueueFn = Callable[..., Awaitable[Any]]

PUBLISH_JOB_TYPE = "publish_project"


def publish_dedupe_key(project_id: int, attempt: int) -> str:
    return f"{PUBLISH_JOB_TYPE}:{project_id}:{attempt}"


__all__ = ["ProjectNotFound", "ProjectArchived"]  # re-exported for callers of start_publish


class PublishNotReady(Exception):
    def __init__(self, accepted: int, total: int) -> None:
        super().__init__(f"{accepted}/{total} files accepted")
        self.accepted = accepted
        self.total = total


class PublishSourcesMissing(Exception):
    def __init__(self, message: str, missing: list[str]) -> None:
        super().__init__(message)
        self.message = message
        self.missing = missing


_MAX_LISTED_MISSING = 20


def find_missing_sources(
    source_directory: str, relative_paths: list[str], import_root: Path | None = None,
) -> tuple[str | None, list[str]]:
    """Filesystem check shared by the preflight, the Publish request and the job.

    Returns ``(reason, missing_relative_paths)``; ``reason`` is None when the
    source directory and every media file are present and readable. Paths go
    through ``resolve_source_path_parts`` so nothing outside IMPORT_ROOT is probed.
    """
    try:
        directory = resolve_source_path_parts(
            source_directory, ".", import_root=import_root, must_exist=False,
        )
    except SourcePathError as exc:
        return f"Source folder is not valid: {exc}", []
    if not directory.is_dir() or not os.access(directory, os.R_OK | os.X_OK):
        return f"The source folder '{source_directory}' is missing or not accessible.", []

    missing: list[str] = []
    for rel in relative_paths:
        try:
            path = resolve_source_path_parts(
                source_directory, rel, import_root=import_root, must_exist=True,
            )
        except SourcePathError:
            missing.append(rel)
            continue
        if not os.access(path, os.R_OK):
            missing.append(rel)
    if missing:
        shown = ", ".join(missing[:3]) + (", …" if len(missing) > 3 else "")
        return f"{len(missing)} source media file(s) are missing or not accessible ({shown}).", missing
    return None, []


@dataclass
class PublishPreflight:
    reason: str | None
    missing: list[str]


async def _source_check(source_directory: str, relative_paths: list[str]) -> tuple[str | None, list[str]]:
    from app.core.config import settings
    reason, missing = await asyncio.to_thread(
        find_missing_sources, source_directory, relative_paths, settings.import_root,
    )
    return reason, missing[:_MAX_LISTED_MISSING]


async def publish_preflight(project_id: int) -> PublishPreflight:
    """Everything the Publish button depends on, in one cheap answer."""
    async with AsyncSessionLocal() as session:
        project = await session.get(Project, project_id)
        if project is None:
            raise ProjectNotFound()
        if project.archived_at is not None:
            return PublishPreflight("The project is archived. Unarchive it to publish.", [])
        accepted, total = (await accepted_counts(session, [project_id]))[project_id]
        if total == 0 or accepted < total:
            return PublishPreflight(
                f"All files must be accepted before publishing ({accepted}/{total}).", [],
            )
        source_directory = project.source_directory
        relative_paths = list((await session.scalars(
            select(File.relative_path).where(File.project_id == project_id).order_by(File.relative_path)
        )).all())
    reason, missing = await _source_check(source_directory, relative_paths)
    return PublishPreflight(reason, missing)


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
            if project.archived_at is not None:
                raise ProjectArchived()
            accepted, total = (await accepted_counts(session, [project_id]))[project_id]
            if total == 0 or accepted < total:
                raise PublishNotReady(accepted, total)
            # Re-validated here (not trusted from an earlier preflight): the
            # media may have been removed since the page last checked.
            reason, missing = await _source_check(
                project.source_directory,
                list((await session.scalars(
                    select(File.relative_path).where(File.project_id == project_id)
                )).all()),
            )
            if reason is not None:
                raise PublishSourcesMissing(reason, missing)

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
