"""Project archive state.

Archiving is a pure database flag (``Project.archived_at``): nothing is
deleted, moved or re-derived, and no other project column is touched. An
archived project is inert — the API rejects mutations for it (see
``app/api/archive_guard.py``), the job manager refuses to enqueue for it and the
orchestrator/sweep skip it — until it is unarchived.

Eligibility: at least one file, every file ``accepted``, no queued/running job
and no publish in progress. The checks give a readable reason; the final write is
one conditional UPDATE that re-asserts the same conditions in SQL, so a job
enqueued (or a file un-accepted) between the check and the write can't slip
through. ``JobManager.enqueue`` closes the other half of the race by re-checking
the flag after its insert.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import case, exists, func, not_, or_, select, update

from app.core.database import AsyncSessionLocal
from app.db.models import File, FileStatus, JobRecord, JobStatus, Project
from app.db.output_state import PublishState

_ACTIVE_JOB_STATUSES = (JobStatus.QUEUED.value, JobStatus.RUNNING.value)


class ProjectNotFound(Exception):
    pass


class ProjectArchived(Exception):
    """A mutation was attempted on an archived project."""


class ArchiveNotAllowed(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


async def is_project_archived(session, project_id: int) -> bool:
    return (
        await session.scalar(select(Project.archived_at).where(Project.id == project_id))
    ) is not None


def _reason(total: int, accepted: int, active_jobs: int, publishing: bool) -> str | None:
    if total == 0:
        return "The project has no files."
    if accepted < total:
        return f"{total - accepted} of {total} file(s) are not accepted yet."
    if active_jobs:
        return f"{active_jobs} job(s) are still queued or running."
    if publishing:
        return "A publish is in progress."
    return None


async def archive_blockers(session, project_ids: list[int]) -> dict[int, str | None]:
    """{project_id: reason it cannot be archived, or None when eligible}.

    Two aggregate queries regardless of how many projects are asked about."""
    if not project_ids:
        return {}
    file_rows = (await session.execute(
        select(
            File.project_id,
            func.count(File.id),
            func.sum(case((File.status == FileStatus.ACCEPTED.value, 1), else_=0)),
        )
        .where(File.project_id.in_(project_ids))
        .group_by(File.project_id)
    )).all()
    files = {pid: (int(total), int(acc or 0)) for pid, total, acc in file_rows}
    job_rows = (await session.execute(
        select(JobRecord.project_id, func.count(JobRecord.id))
        .where(JobRecord.project_id.in_(project_ids), JobRecord.status.in_(_ACTIVE_JOB_STATUSES))
        .group_by(JobRecord.project_id)
    )).all()
    jobs = {pid: int(n) for pid, n in job_rows}
    publishing = set((await session.scalars(
        select(Project.id).where(
            Project.id.in_(project_ids),
            or_(Project.publish_state == PublishState.PUBLISHING.value,
                Project.web_publish_state == "publishing"),
        )
    )).all())

    out: dict[int, str | None] = {}
    for pid in project_ids:
        total, accepted = files.get(pid, (0, 0))
        out[pid] = _reason(total, accepted, jobs.get(pid, 0), pid in publishing)
    return out


async def archive_project(project_id: int) -> bool:
    """Archive the project. Returns False when it already was archived (a safe
    no-op). Raises ProjectNotFound / ArchiveNotAllowed."""
    from app.orchestrator.orchestrator import _get_project_lock

    # Same per-project lock the orchestrator uses, so a pass that is mid-way
    # through enqueueing can't interleave with the eligibility check.
    async with _get_project_lock(project_id):
        async with AsyncSessionLocal() as session:
            project = await session.get(Project, project_id)
            if project is None:
                raise ProjectNotFound()
            if project.archived_at is not None:
                return False
            reason = (await archive_blockers(session, [project_id]))[project_id]
            if reason:
                raise ArchiveNotAllowed(reason)

            now = datetime.utcnow().isoformat()
            result = await session.execute(
                update(Project)
                .where(
                    Project.id == project_id,
                    Project.archived_at.is_(None),
                    or_(Project.web_publish_state.is_(None), Project.web_publish_state != "publishing"),
                    or_(
                        Project.publish_state.is_(None),
                        Project.publish_state != PublishState.PUBLISHING.value,
                    ),
                    exists().where(File.project_id == project_id),
                    not_(exists().where(
                        File.project_id == project_id,
                        File.status != FileStatus.ACCEPTED.value,
                    )),
                    not_(exists().where(
                        JobRecord.project_id == project_id,
                        JobRecord.status.in_(_ACTIVE_JOB_STATUSES),
                    )),
                )
                # updated_at is pinned to itself so the column's onupdate hook
                # doesn't make archiving look like a data change.
                .values(archived_at=now, updated_at=Project.updated_at)
                .execution_options(synchronize_session=False)
            )
            await session.commit()
            if not result.rowcount:
                raise ArchiveNotAllowed(
                    "The project changed while archiving (a job started or a file changed). Try again."
                )
    return True


async def unarchive_project(project_id: int) -> bool:
    """Clear archived_at. Returns False when the project was not archived.
    Nothing else changes — no job is resumed or enqueued."""
    from app.orchestrator.orchestrator import _get_project_lock

    async with _get_project_lock(project_id):
        async with AsyncSessionLocal() as session:
            project = await session.get(Project, project_id)
            if project is None:
                raise ProjectNotFound()
            if project.archived_at is None:
                return False
            await session.execute(
                update(Project)
                .where(Project.id == project_id)
                .values(archived_at=None, updated_at=Project.updated_at)
                .execution_options(synchronize_session=False)
            )
            await session.commit()
    return True
