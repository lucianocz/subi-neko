from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import Any, Callable, Awaitable

from sqlalchemy import case, func, select, update
from sqlalchemy.exc import IntegrityError

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.db import options as options_store
from app.db.models import JobRecord, JobStatus, SubtitleChunk
from app.jobs.context import JobContext, JobResult
from app.jobs.models import JobProgress
from app.jobs.registry import get_handler

logger = logging.getLogger(__name__)

BroadcastFn = Callable[[str, dict], Awaitable[None]]


class JobManager:
    def __init__(self) -> None:
        self._queue: asyncio.Queue[int] = asyncio.Queue()  # holds DB job IDs
        self._progress: dict[int, JobProgress] = {}        # in-flight progress only
        self._workers: list[asyncio.Task] = []
        self._stop_events: list[asyncio.Event] = []
        self._draining: list[asyncio.Task] = []            # shrunk workers finishing their last job
        self._broadcast: BroadcastFn | None = None
        self._on_complete: Callable[[int], Awaitable[None]] | None = None

    def set_broadcast(self, fn: BroadcastFn) -> None:
        self._broadcast = fn

    def set_on_complete(self, fn: Callable[[int], Awaitable[None]]) -> None:
        """Register a callback invoked after every job finishes (success or fail)."""
        self._on_complete = fn

    async def start(self, worker_count: int = 4) -> None:
        await self._reload_queued_jobs()
        for i in range(worker_count):
            self._spawn_worker(i)
        logger.info("JobManager started with %d workers", worker_count)

    async def stop(self) -> None:
        for ev in self._stop_events:
            ev.set()
        all_workers = self._workers + self._draining
        for w in all_workers:
            w.cancel()
        await asyncio.gather(*all_workers, return_exceptions=True)
        self._workers.clear()
        self._stop_events.clear()
        self._draining.clear()
        logger.info("JobManager stopped")

    async def resize(self, new_count: int) -> None:
        """Grow or gracefully shrink the worker pool."""
        current = len(self._workers)
        if new_count == current:
            return
        if new_count > current:
            for i in range(current, new_count):
                self._spawn_worker(i)
        else:
            retiring = self._workers[new_count:]
            retiring_events = self._stop_events[new_count:]
            self._workers = self._workers[:new_count]
            self._stop_events = self._stop_events[:new_count]
            for ev in retiring_events:
                ev.set()
            self._draining.extend(retiring)
        logger.info("JobManager resized to %d workers (was %d)", new_count, current)

    async def enqueue(
        self,
        job_type: str,
        project_id: int,
        payload: dict[str, Any] | None = None,
        file_id: int | None = None,
        dedupe_key: str | None = None,
        priority: int = 100,
        max_attempts: int | None = None,
    ) -> JobRecord | None:
        key = dedupe_key or f"{job_type}:{project_id}:{file_id}"
        async with AsyncSessionLocal() as session:
            # Honour dedupe: return existing queued/running job if key matches
            existing = await session.scalar(
                select(JobRecord).where(
                    JobRecord.dedupe_key == key,
                    JobRecord.status.in_(["queued", "running"]),
                )
            )
            if existing:
                logger.info("Job dedupe hit for key '%s', returning existing id=%d", key, existing.id)
                return existing

            record = JobRecord(
                job_type=job_type,
                project_id=project_id,
                file_id=file_id,
                status=JobStatus.QUEUED.value,
                dedupe_key=key,
                priority=priority,
                payload_json=json.dumps(payload or {}),
                max_attempts=max_attempts if max_attempts is not None else (1 if job_type in _CHUNK_JOB_TYPES else 3),
                scheduled_at=datetime.utcnow().isoformat(),
                created_at=datetime.utcnow().isoformat(),
                updated_at=datetime.utcnow().isoformat(),
            )
            session.add(record)
            try:
                await session.commit()
            except IntegrityError:
                # Race or stale key: another job with same dedupe_key exists
                await session.rollback()

                # Re-check: if queued/running, return it (race with another enqueue)
                existing = await session.scalar(
                    select(JobRecord).where(
                        JobRecord.dedupe_key == key,
                        JobRecord.status.in_(["queued", "running"]),
                    )
                )
                if existing:
                    logger.info("Job dedupe hit (race) for key '%s', id=%d", key, existing.id)
                    return existing

                # Stale completed/failed/cancelled job — reset it for re-enqueue
                stale = await session.scalar(
                    select(JobRecord).where(JobRecord.dedupe_key == key)
                )
                if stale:
                    stale.status = JobStatus.QUEUED.value
                    stale.payload_json = json.dumps(payload or {})
                    stale.result_json = None
                    stale.error_code = None
                    stale.error_message = None
                    stale.attempt_count = 0
                    stale.started_at = None
                    stale.finished_at = None
                    stale.scheduled_at = datetime.utcnow().isoformat()
                    stale.updated_at = datetime.utcnow().isoformat()
                    await session.commit()
                    await session.refresh(stale)
                    await self._queue.put(stale.id)
                    await self._emit("job_created", stale.id, status=JobStatus.QUEUED.value,
                                     job_type=stale.job_type, project_id=stale.project_id,
                                     payload_json=stale.payload_json,
                                     scheduled_at=stale.scheduled_at)
                    logger.info("Re-enqueued stale job id=%d type=%s", stale.id, job_type)
                    return stale

                logger.warning("Job dedupe key '%s' conflict but no row found", key)
                return None
            await session.refresh(record)

        await self._queue.put(record.id)
        await self._emit("job_created", record.id, status=JobStatus.QUEUED.value,
                         job_type=record.job_type, project_id=record.project_id,
                         payload_json=record.payload_json,
                         scheduled_at=record.scheduled_at)
        logger.info("Enqueued job id=%d type=%s", record.id, job_type)
        return record

    def get_progress(self, job_id: int) -> JobProgress | None:
        return self._progress.get(job_id)

    async def cancel_project_jobs(self, project_id: int) -> list[int]:
        """Cancel all queued/running jobs for a project (called before project deletion)."""
        async with AsyncSessionLocal() as session:
            rows = await session.scalars(
                select(JobRecord).where(
                    JobRecord.project_id == project_id,
                    JobRecord.status.in_([JobStatus.QUEUED.value, JobStatus.RUNNING.value]),
                )
            )
            job_ids: list[int] = []
            now = datetime.utcnow().isoformat()
            for record in rows.all():
                record.status = JobStatus.CANCELLED.value
                record.finished_at = now
                record.updated_at = now
                job_ids.append(record.id)
            if job_ids:
                await session.commit()

        for job_id in job_ids:
            self._progress.pop(job_id, None)
            await self._emit("job_update", job_id, status=JobStatus.CANCELLED.value)

        if job_ids:
            logger.info("Cancelled %d jobs for project_id=%d", len(job_ids), project_id)
        return job_ids

    async def cancel_queued_project_jobs(self, project_id: int) -> list[int]:
        """Cancel only queued (not running) jobs — used when pausing so in-flight jobs drain."""
        async with AsyncSessionLocal() as session:
            rows = await session.scalars(
                select(JobRecord).where(
                    JobRecord.project_id == project_id,
                    JobRecord.status == JobStatus.QUEUED.value,
                )
            )
            job_ids: list[int] = []
            now = datetime.utcnow().isoformat()
            for record in rows.all():
                record.status = JobStatus.CANCELLED.value
                record.finished_at = now
                record.updated_at = now
                job_ids.append(record.id)
            if job_ids:
                await session.commit()

        for job_id in job_ids:
            self._progress.pop(job_id, None)
            await self._emit("job_update", job_id, status=JobStatus.CANCELLED.value)

        if job_ids:
            logger.info("Cancelled %d queued jobs for project_id=%d (running jobs will drain)", len(job_ids), project_id)
        return job_ids

    async def cancel_job(self, job_id: int) -> bool:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                update(JobRecord)
                .where(JobRecord.id == job_id, JobRecord.status == JobStatus.QUEUED.value)
                .values(status=JobStatus.CANCELLED.value, updated_at=datetime.utcnow().isoformat())
                .returning(JobRecord.id)
            )
            await session.commit()
            if result.first():
                await self._emit("job_update", job_id, status=JobStatus.CANCELLED.value)
                return True
        return False

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _spawn_worker(self, index: int) -> None:
        stop_event = asyncio.Event()
        task = asyncio.create_task(self._worker(index, stop_event), name=f"job-worker-{index}")
        self._workers.append(task)
        self._stop_events.append(stop_event)

    async def _reload_queued_jobs(self) -> None:
        """On startup, re-queue DB-queued jobs and reset any stuck RUNNING jobs."""
        async with AsyncSessionLocal() as session:
            # Reset jobs stuck in RUNNING state (app crashed mid-run)
            stuck = (await session.scalars(
                select(JobRecord).where(JobRecord.status == JobStatus.RUNNING.value)
            )).all()
            if stuck:
                for record in stuck:
                    logger.warning(
                        "Startup: resetting stuck RUNNING job id=%d type=%s → QUEUED",
                        record.id, record.job_type,
                    )
                    record.status = JobStatus.QUEUED.value
                    record.started_at = None
                    record.updated_at = datetime.utcnow().isoformat()
                await session.commit()
                logger.warning("Reset %d stuck RUNNING job(s) to QUEUED on startup", len(stuck))

            rows = (await session.scalars(
                select(JobRecord)
                .where(JobRecord.status == JobStatus.QUEUED.value)
                .order_by(JobRecord.priority, JobRecord.scheduled_at)
            )).all()
            for record in rows:
                await self._queue.put(record.id)
        logger.info("Startup: reloaded %d queued job(s) from DB", len(rows))

    async def _worker(self, index: int, stop_event: asyncio.Event) -> None:
        logger.debug("Worker %d ready", index)
        while not stop_event.is_set():
            try:
                job_id = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            await self._run_job(job_id)
            self._queue.task_done()

    async def _run_job(self, job_id: int) -> None:
        async with AsyncSessionLocal() as session:
            record = await session.get(JobRecord, job_id)
            if record is None or record.status == JobStatus.CANCELLED.value:
                return
            if record.status != JobStatus.QUEUED.value:
                logger.info(
                    "Skipping stale queue entry for job id=%d type=%s status=%s",
                    job_id, record.job_type, record.status,
                )
                return
            if record.attempt_count >= record.max_attempts:
                logger.warning(
                    "Job id=%d type=%s exceeded max attempts (%d), failing permanently",
                    job_id, record.job_type, record.max_attempts,
                )
                record.status = JobStatus.FAILED.value
                record.error_code = "MAX_ATTEMPTS_EXCEEDED"
                record.error_message = "Max attempts exceeded"
                record.finished_at = datetime.utcnow().isoformat()
                record.updated_at = datetime.utcnow().isoformat()
                _early_file_id = record.file_id
                _early_job_type = record.job_type
                _early_payload = json.loads(record.payload_json or "{}")
                await session.commit()
                await self._emit("job_update", job_id, status=JobStatus.FAILED.value)
                # Ensure the chunk is marked as failed so the orchestrator stops re-enqueueing.
                if _early_file_id is not None:
                    await _mark_chunk_job_failed(
                        _early_file_id, _early_job_type, _early_payload,
                        "MAX_ATTEMPTS_EXCEEDED", "Max attempts exceeded",
                    )
                if self._on_complete:
                    try:
                        await self._on_complete(job_id)
                    except Exception:
                        logger.exception("on_complete callback failed for job id=%d (max attempts)", job_id)
                return

            handler = get_handler(record.job_type)
            if handler is None:
                logger.error(
                    "Job id=%d: no handler registered for type '%s', failing",
                    job_id, record.job_type,
                )
                record.status = JobStatus.FAILED.value
                record.error_code = "NO_HANDLER"
                record.error_message = f"No handler registered for job type '{record.job_type}'"
                record.finished_at = datetime.utcnow().isoformat()
                record.updated_at = datetime.utcnow().isoformat()
                await session.commit()
                await self._emit("job_update", job_id, status=JobStatus.FAILED.value)
                return

            payload = json.loads(record.payload_json or "{}")
            record.status = JobStatus.RUNNING.value
            record.attempt_count += 1
            record.started_at = datetime.utcnow().isoformat()
            record.updated_at = datetime.utcnow().isoformat()
            await session.commit()
            logger.info(
                "Job id=%d type=%s starting (attempt %d/%d)",
                job_id, record.job_type, record.attempt_count, record.max_attempts,
            )

        progress = JobProgress(job_id=job_id, job_type=record.job_type)
        self._progress[job_id] = progress
        _project_id = record.project_id  # capture before try/finally (record may be detached)
        _file_id = record.file_id
        _job_type = record.job_type
        _started_at = record.started_at
        await self._emit("job_update", job_id, status=JobStatus.RUNNING.value, started_at=_started_at)

        ctx = JobContext(
            import_root=settings.import_root,
            output_root=settings.output_root,
            options=options_store.snapshot(),
        )

        loop = asyncio.get_running_loop()

        def _sync_progress(pct: float, msg: str) -> None:
            progress.progress = max(0.0, min(1.0, pct))
            progress.message = msg
            progress.updated_at = datetime.utcnow()
            asyncio.run_coroutine_threadsafe(self._emit("job_progress", job_id), loop)

        _final_status: str | None = None
        _chunk_error_code: str | None = None
        _chunk_error_message: str | None = None

        try:
            result: JobResult = await asyncio.to_thread(handler, payload, ctx, _sync_progress)

            async with AsyncSessionLocal() as session:
                record = await session.get(JobRecord, job_id)
                if record is None:
                    logger.warning("Job id=%d record gone after completion (project deleted?)", job_id)
                    return
                # Don't overwrite if already cancelled externally (e.g. manual cancel)
                if record.status == JobStatus.CANCELLED.value:
                    _final_status = JobStatus.CANCELLED.value
                    return
                _final_status = (
                    JobStatus.COMPLETED.value
                    if result["status"] == "succeeded"
                    else JobStatus.FAILED.value
                )
                record.status = _final_status
                if result["result"] is not None:
                    record.result_json = json.dumps(result["result"])
                record.error_code = result["error_code"]
                record.error_message = result["error_message"]
                record.finished_at = datetime.utcnow().isoformat()
                record.updated_at = datetime.utcnow().isoformat()
                await session.commit()

            if result["status"] == "succeeded":
                logger.info("Job id=%d succeeded", job_id)
            else:
                logger.warning(
                    "Job id=%d failed: [%s] %s",
                    job_id,
                    result["error_code"] or "UNKNOWN",
                    result["error_message"] or "",
                )
                _chunk_error_code = result["error_code"]
                _chunk_error_message = result["error_message"]

        except asyncio.CancelledError:
            _final_status = JobStatus.CANCELLED.value
            async with AsyncSessionLocal() as session:
                record = await session.get(JobRecord, job_id)
                if record is not None:
                    record.status = JobStatus.CANCELLED.value
                    record.finished_at = datetime.utcnow().isoformat()
                    record.updated_at = datetime.utcnow().isoformat()
                    await session.commit()
            raise

        except Exception as exc:
            _final_status = JobStatus.FAILED.value
            _chunk_error_code = "UNEXPECTED_ERROR"
            _chunk_error_message = str(exc)
            logger.exception("Job id=%d raised unexpectedly", job_id)
            async with AsyncSessionLocal() as session:
                record = await session.get(JobRecord, job_id)
                if record is not None:
                    record.status = JobStatus.FAILED.value
                    record.error_code = "UNEXPECTED_ERROR"
                    record.error_message = str(exc)
                    record.finished_at = datetime.utcnow().isoformat()
                    record.updated_at = datetime.utcnow().isoformat()
                    await session.commit()

        finally:
            self._progress.pop(job_id, None)
            await self._emit("job_update", job_id, status=_final_status)

            # Mark chunk as job_failed BEFORE triggering orchestration,
            # so the orchestrator sees the terminal state and does not re-enqueue.
            if _final_status == JobStatus.FAILED.value and _file_id is not None:
                await _mark_chunk_job_failed(
                    _file_id, _job_type, payload,
                    _chunk_error_code, _chunk_error_message,
                )
            elif _final_status == JobStatus.COMPLETED.value and _file_id is not None:
                await _clear_chunk_retry_state(_file_id, _job_type, payload)

            # Trigger orchestration after job completes
            if self._on_complete:
                try:
                    await self._on_complete(job_id)
                except Exception:
                    logger.exception("on_complete callback failed for job id=%d", job_id)

            # Notify frontend that project/file state may have changed after orchestration
            if self._broadcast and _project_id:
                try:
                    await self._broadcast("project_updated", {"project_id": _project_id})
                except Exception:
                    logger.exception("project_updated broadcast failed for project_id=%d", _project_id)

            # Broadcast chunk progress for translate_chunk jobs
            if (
                self._broadcast
                and _job_type == "translate_chunk"
                and _final_status == JobStatus.COMPLETED.value
                and _file_id is not None
                and _project_id is not None
            ):
                try:
                    async with AsyncSessionLocal() as session:
                        row = (await session.execute(
                            select(
                                func.count().label("total"),
                                func.sum(case((SubtitleChunk.status != "pending", 1), else_=0)).label("done"),
                            ).where(SubtitleChunk.file_id == _file_id)
                        )).one_or_none()
                    if row and row.total:
                        await self._broadcast("chunk_progress", {
                            "file_id": _file_id,
                            "project_id": _project_id,
                            "chunks_done": int(row.done or 0),
                            "chunks_total": int(row.total),
                        })
                except Exception:
                    logger.exception("chunk_progress broadcast failed for file_id=%d", _file_id)

    async def _emit(self, event: str, job_id: int, status: str | None = None, **extra: Any) -> None:
        if not self._broadcast:
            return
        data: dict[str, Any] = {"job_id": job_id}
        try:
            async with AsyncSessionLocal() as session:
                record = await session.get(JobRecord, job_id)
                if record is not None:
                    data.update({
                        "project_id": record.project_id,
                        "file_id": record.file_id,
                        "job_type": record.job_type,
                        "payload_json": record.payload_json,
                        "result_json": record.result_json,
                        "attempt_count": record.attempt_count,
                        "error_code": record.error_code,
                        "error_message": record.error_message,
                        "scheduled_at": record.scheduled_at,
                        "started_at": record.started_at,
                        "finished_at": record.finished_at,
                        "updated_at": record.updated_at,
                    })
                    if status is None:
                        status = record.status
        except Exception:
            logger.exception("Failed to enrich websocket event for job id=%d", job_id)
        if status is not None:
            data["status"] = status
        if extra:
            data.update(extra)
        progress = self._progress.get(job_id)
        if progress:
            data.update(progress.to_dict())
        try:
            await self._broadcast(event, data)
        except Exception:
            logger.exception("Broadcast failed for event '%s'", event)


job_manager = JobManager()


# ---------------------------------------------------------------------------
# Chunk failure helper — module-level so it can be easily unit-tested
# ---------------------------------------------------------------------------

_CHUNK_JOB_TYPES = {
    "translate_chunk",
    "validate_chunk",
    "repair_chunk",
    "polish_chunk",
    "review_chunk_final",
    "audit_chunk_final",
}

_FAILED_JOB_ALLOWED_CHUNK_STATUSES = {
    "translate_chunk": {"pending"},
    "validate_chunk": {"translated"},
    "repair_chunk": {"validate_trans_failed"},
    "polish_chunk": {"validated", "needs_polish"},
    "review_chunk_final": {"polished"},
    "audit_chunk_final": {"final_reviewed", "audited"},
}

# Error codes worth an automatic re-run of the same chunk stage: the LLM
# client already retried transient API noise in-call, but an exhausted retry
# budget or a still-unparseable response can succeed on a fresh attempt.
_RETRYABLE_CHUNK_ERROR_CODES = {
    "OPENAI_API_ERROR",
    "RESPONSE_PARSE_ERROR",
    "RESPONSE_VALIDATION_ERROR",
    "UNEXPECTED_ERROR",
}

# Automatic re-runs per chunk before the failure becomes terminal
# (job_failed) and requires a manual retry.
_MAX_CHUNK_AUTO_RETRIES = 3


async def _mark_chunk_job_failed(
    file_id: int,
    job_type: str,
    payload: dict,
    error_code: str | None,
    error_message: str | None,
) -> None:
    """Handle a technical chunk-job failure.

    Retryable errors (API/parse) burn one unit of the chunk's auto-retry
    budget and leave the chunk status untouched — the orchestrator then
    re-enqueues the same stage. Terminal errors, or an exhausted budget,
    set status job_failed so the orchestrator stops and the UI surfaces
    the error for manual retry.
    """
    if job_type not in _CHUNK_JOB_TYPES:
        return

    chunk_index = payload.get("chunk_index")
    if not isinstance(chunk_index, int):
        return

    now = datetime.utcnow().isoformat()
    try:
        async with AsyncSessionLocal() as session:
            chunk = await session.scalar(
                select(SubtitleChunk)
                .where(SubtitleChunk.file_id == file_id)
                .where(SubtitleChunk.chunk_index == chunk_index)
            )
            if chunk is None:
                return
            allowed_statuses = _FAILED_JOB_ALLOWED_CHUNK_STATUSES.get(job_type)
            if allowed_statuses is not None and chunk.status not in allowed_statuses:
                logger.info(
                    "Ignoring stale failure for chunk file_id=%d index=%d "
                    "(job_type=%s current_status=%s)",
                    file_id, chunk_index, job_type, chunk.status,
                )
                return

            retryable = (
                error_code in _RETRYABLE_CHUNK_ERROR_CODES
                and (chunk.retry_count or 0) < _MAX_CHUNK_AUTO_RETRIES
            )
            new_retry_count = (chunk.retry_count or 0) + 1
            chunk.retry_count = new_retry_count
            chunk.failed_job_type = job_type
            chunk.last_error_code = error_code
            chunk.last_error_message = error_message
            chunk.updated_at = now
            if not retryable:
                chunk.status = "job_failed"
            await session.commit()

        if retryable:
            logger.warning(
                "Chunk file_id=%d index=%d %s failed with %s — auto-retry %d/%d",
                file_id, chunk_index, job_type, error_code,
                new_retry_count, _MAX_CHUNK_AUTO_RETRIES,
            )
        else:
            logger.warning(
                "Chunk file_id=%d index=%d → job_failed (job_type=%s, error=%s)",
                file_id, chunk_index, job_type, error_code,
            )
    except Exception:
        logger.exception(
            "Failed to mark chunk file_id=%d index=%d as job_failed",
            file_id, chunk_index,
        )


async def _clear_chunk_retry_state(file_id: int, job_type: str, payload: dict) -> None:
    """A chunk stage succeeded — reset the auto-retry budget so each stage
    gets the full allowance."""
    if job_type not in _CHUNK_JOB_TYPES:
        return
    chunk_index = payload.get("chunk_index")
    if not isinstance(chunk_index, int):
        return
    try:
        async with AsyncSessionLocal() as session:
            chunk = await session.scalar(
                select(SubtitleChunk)
                .where(SubtitleChunk.file_id == file_id)
                .where(SubtitleChunk.chunk_index == chunk_index)
            )
            if chunk is not None and (chunk.retry_count or 0) > 0:
                chunk.retry_count = 0
                chunk.updated_at = datetime.utcnow().isoformat()
                await session.commit()
    except Exception:
        logger.exception(
            "Failed to clear retry state for chunk file_id=%d index=%d",
            file_id, chunk_index,
        )
