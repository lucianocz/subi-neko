"""Progressive file metrics: computed/refreshed during the pipeline, independent
of mux/completion state; NULL (not 0) when source data is missing; stale-run
writes rejected; retranslate invalidates and the new run repopulates."""
from __future__ import annotations

import json
import unittest.mock as mock
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.db.models import (
    File,
    FileQualityMetric,
    FileStatus,
    LlmCall,
    Project,
    QaItem,
    SubtitleEvent,
)
from app.db.options import AppOptions
from app.jobs.context import JobContext
from app.jobs.handlers import compute_file_metrics as cfm
from app.jobs.handlers.compute_file_metrics import fingerprint_of, metrics_fingerprint_stmt

# Reuse the orchestrator test fixtures/helpers (in-memory async DB).
from tests.test_orchestrator import (  # noqa: F401
    _create_chunk,
    _create_event,
    _create_file,
    _create_project,
    db_session,
    enqueue_mock,
)


def _now() -> str:
    return datetime.utcnow().isoformat()


def _progress(_v: float, _m: str) -> None:
    pass


def _ctx() -> JobContext:
    return JobContext(import_root=Path("."), output_root=Path("."), options=AppOptions())


# ---------------------------------------------------------------------------
# Handler (sync DB)
# ---------------------------------------------------------------------------

@pytest.fixture
def factory(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    f = sessionmaker(bind=engine)
    monkeypatch.setattr(cfm, "SyncSessionLocal", f)
    yield f
    engine.dispose()


def _seed(factory, *, status="processing", polished=False, ai_text="Ahoj", confidence=None) -> int:
    with factory() as session:
        project = Project(
            name="P", source_directory="p", anime_provider="a", anime_external_id="1",
            created_at=_now(), updated_at=_now(),
        )
        session.add(project)
        session.flush()
        file = File(
            project_id=project.id, filename="e.mkv", relative_path="e.mkv", status=status,
            translation_requested_at="2026-01-01T00:00:00", created_at=_now(), updated_at=_now(),
        )
        session.add(file)
        session.flush()
        session.add(SubtitleEvent(
            file_id=file.id, line_index=0, event_type="dialogue", layer=0, start_ms=0,
            end_ms=1000,
            original_start_ms=0, original_end_ms=1000, style="Default", source_text="Hi", translated_text="Ahoj",
            original_ai_translated_text=ai_text, translation_status="translated",
            translation_confidence=confidence, created_at=_now(), updated_at=_now(),
        ))
        if polished:
            session.add(QaItem(
                file_id=file.id, severity="info", qa_type="polish_edit", message="m",
                is_resolved=1, created_at=_now(),
                details_json=json.dumps({"before": "Nazdar", "after": "Ahoj"}),
            ))
        session.commit()
        return file.id


def _metric(factory):
    with factory() as session:
        return session.scalar(select(FileQualityMetric))


def _set_file(factory, file_id, **fields):
    with factory() as session:
        f = session.get(File, file_id)
        for k, v in fields.items():
            setattr(f, k, v)
        session.commit()


def test_metrics_computed_for_unfinished_unmuxed_file(factory):
    file_id = _seed(factory, status="processing")

    cfm.compute_file_metrics({"file_id": file_id}, _ctx(), _progress)

    m = _metric(factory)
    assert m is not None and m.events_total == 1
    assert m.source_fingerprint


def test_missing_source_data_stays_null_not_zero(factory):
    file_id = _seed(factory, ai_text=None, confidence=None)

    cfm.compute_file_metrics({"file_id": file_id}, _ctx(), _progress)

    m = _metric(factory)
    assert m.edit_distance_norm is None
    assert m.polish_churn_norm is None
    assert m.mean_confidence is None
    assert m.mean_confidence_edited is None
    assert m.llm_cost_usd is None


def test_metrics_update_after_later_stage(factory):
    file_id = _seed(factory)
    cfm.compute_file_metrics({"file_id": file_id}, _ctx(), _progress)
    before = _metric(factory)
    assert before.polish_churn_norm is None

    with factory() as session:  # polish pass rewrites a line and records it
        session.add(QaItem(
            file_id=file_id, severity="info", qa_type="polish_edit", message="m",
            is_resolved=1, created_at=_now(),
            details_json=json.dumps({"before": "Nazdar", "after": "Ahoj"}),
        ))
        session.commit()
    cfm.compute_file_metrics({"file_id": file_id}, _ctx(), _progress)

    after = _metric(factory)
    assert after.polish_churn_norm is not None and after.polish_churn_norm > 0
    assert after.source_fingerprint != before.source_fingerprint


def test_completed_file_still_works(factory):
    file_id = _seed(factory, status="completed", polished=True)

    cfm.compute_file_metrics({"file_id": file_id}, _ctx(), _progress)

    m = _metric(factory)
    assert m.events_total == 1 and m.polish_edit_count == 1


def test_stale_computation_cannot_overwrite_reset_run(factory):
    """Retranslate flips translation_requested_at while a compute job sits
    between its read and write phases."""
    file_id = _seed(factory)

    def reset_midflight(value: float, _msg: str) -> None:
        if value == 0.7:
            _set_file(factory, file_id, translation_requested_at="2026-01-02T00:00:00")

    result = cfm.compute_file_metrics({"file_id": file_id}, _ctx(), reset_midflight)

    assert result["result"] == {"skipped": "stale translation attempt"}
    assert _metric(factory) is None


# ---------------------------------------------------------------------------
# Orchestrator refresh checkpoints + retranslate invalidation (async DB)
# ---------------------------------------------------------------------------

async def _fingerprint(session, file_id: int) -> str:
    return fingerprint_of((await session.execute(metrics_fingerprint_stmt(file_id))).one())


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["processing", "review_required", "accepted", "completed"])
async def test_translated_file_enqueues_metrics_regardless_of_status(db_session, enqueue_mock, status):
    from app.orchestrator.project_orchestrator import _ensure_file_metrics
    project = await _create_project(db_session, status="processing")
    file = await _create_file(db_session, project.id, status=status)
    await _create_event(db_session, file.id)

    await _ensure_file_metrics(project.id, [file], enqueue_mock)

    enqueue_mock.assert_awaited_once()
    assert enqueue_mock.call_args.kwargs["job_type"] == "compute_file_metrics"


@pytest.mark.asyncio
async def test_no_translated_lines_means_no_metrics_job(db_session, enqueue_mock):
    from app.orchestrator.project_orchestrator import _ensure_file_metrics
    project = await _create_project(db_session, status="processing")
    file = await _create_file(db_session, project.id, status="processing")
    await _create_event(db_session, file.id, translated_text=None, original_ai_translated_text=None)

    await _ensure_file_metrics(project.id, [file], enqueue_mock)

    enqueue_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_file_without_translate_request_is_skipped(db_session, enqueue_mock):
    from app.orchestrator.project_orchestrator import _ensure_file_metrics
    project = await _create_project(db_session, status="processing")
    file = await _create_file(db_session, project.id, status="ready", translation_requested=False)
    await _create_event(db_session, file.id)

    await _ensure_file_metrics(project.id, [file], enqueue_mock)

    enqueue_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_current_snapshot_not_recomputed_but_changes_refresh_it(db_session, enqueue_mock):
    from app.orchestrator.project_orchestrator import _ensure_file_metrics
    project = await _create_project(db_session, status="processing")
    file = await _create_file(db_session, project.id, status="processing")
    event = await _create_event(db_session, file.id)
    db_session.add(FileQualityMetric(
        file_id=file.id, project_id=project.id,
        source_fingerprint=await _fingerprint(db_session, file.id),
    ))
    await db_session.commit()

    await _ensure_file_metrics(project.id, [file], enqueue_mock)
    enqueue_mock.assert_not_awaited()  # up to date

    event.translated_text = "Polished rewrite"  # a later stage changed the output
    await db_session.commit()
    await _ensure_file_metrics(project.id, [file], enqueue_mock)
    enqueue_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_retranslate_drops_snapshot_and_new_run_repopulates_before_mux(db_session, enqueue_mock):
    from app.api.routes.projects import retranslate_file
    from app.orchestrator.project_orchestrator import _ensure_file_metrics

    project = await _create_project(db_session, status="completed")
    file = await _create_file(db_session, project.id, status="completed")
    event = await _create_event(db_session, file.id)
    await _create_chunk(db_session, file.id, 0, status="complete")
    db_session.add(FileQualityMetric(
        file_id=file.id, project_id=project.id, polish_churn_norm=0.3,
        source_fingerprint=await _fingerprint(db_session, file.id),
    ))
    await db_session.commit()

    with mock.patch("app.api.routes.projects.orchestrate_file"):
        await retranslate_file(project.id, file.id)
    await db_session.refresh(file)
    assert await db_session.scalar(
        select(func.count()).select_from(FileQualityMetric).where(FileQualityMetric.file_id == file.id)
    ) == 0

    # Reset run, nothing translated yet: no snapshot, no job (unavailable).
    await _ensure_file_metrics(project.id, [file], enqueue_mock)
    enqueue_mock.assert_not_awaited()

    # New run translates a line; file is mid-pipeline, nowhere near muxing.
    await db_session.refresh(event)
    event.translated_text = "Nový překlad"
    await db_session.commit()
    await _ensure_file_metrics(project.id, [file], enqueue_mock)
    enqueue_mock.assert_awaited_once()
    assert file.status == FileStatus.READY.value
