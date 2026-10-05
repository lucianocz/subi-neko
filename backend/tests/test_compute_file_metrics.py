from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.db.models import (
    File,
    FileQualityMetric,
    LlmCall,
    Project,
    QaItem,
    SubtitleChunk,
    SubtitleEvent,
)
from app.db.options import AppOptions
from app.jobs.context import JobContext
from app.jobs.handlers import compute_file_metrics as cfm


def _progress(_v: float, _m: str) -> None:
    pass


def _now() -> str:
    return datetime.utcnow().isoformat()


@pytest.fixture
def factory(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(cfm, "SyncSessionLocal", factory)
    yield factory
    engine.dispose()


def _seed(session) -> int:
    project = Project(
        name="P", source_directory="p", anime_provider="anidb", anime_external_id="1",
        created_at=_now(), updated_at=_now(),
    )
    session.add(project)
    session.flush()
    file = File(
        project_id=project.id, filename="e1.mkv", relative_path="e1.mkv",
        status="completed", created_at=_now(), updated_at=_now(),
    )
    session.add(file)
    session.flush()

    def _event(i, ai, final, *, edited=False, approved=False, confidence=None):
        session.add(SubtitleEvent(
            file_id=file.id, line_index=i, event_type="dialogue", layer=0,
            start_ms=0, end_ms=2000, original_start_ms=0, original_end_ms=2000, style="Default", source_text=f"src {i}",
            translated_text=final, original_ai_translated_text=ai,
            translation_status="validated", translation_confidence=confidence,
            is_user_edited=1 if edited else 0, is_approved=1 if approved else 0,
            created_at=_now(), updated_at=_now(),
        ))

    _event(0, "Ahoj světe", "Ahoj světe", confidence=0.9)          # untouched
    _event(1, "Nazdar", "Nazdar!", edited=True, confidence=0.4)    # small human edit
    _event(2, "Dobrý den", "Dobrý den", approved=True)             # approved (TM reuse)

    session.add(SubtitleChunk(
        file_id=file.id, chunk_index=0, translate_from_line=0, translate_to_line=2,
        status="complete", prompt_version="abc123def456",
        created_at=_now(), updated_at=_now(),
    ))
    session.add(QaItem(file_id=file.id, severity="warning", qa_type="high_cps",
                       message="m", is_resolved=1, created_at=_now()))
    session.add(QaItem(file_id=file.id, severity="info", qa_type="polish_edit",
                       message="m", is_resolved=1, created_at=_now()))
    session.add(LlmCall(project_id=project.id, file_id=file.id, task="translate",
                        model="m", attempt=1, status="succeeded",
                        prompt_tokens=1000, completion_tokens=500, cost_usd=0.01,
                        created_at=_now()))
    session.commit()
    return file.id


def test_compute_file_metrics_aggregates(factory):
    with factory() as session:
        file_id = _seed(session)

    ctx = JobContext(import_root=Path("."), output_root=Path("."), options=AppOptions())
    result = cfm.compute_file_metrics({"file_id": file_id}, ctx, _progress)

    assert result["status"] == "succeeded"
    with factory() as session:
        m = session.scalar(select(FileQualityMetric))
        assert m.events_total == 3
        assert m.events_user_edited == 1
        assert m.events_approved == 1
        # Two identical pairs (distance 0) and one tiny edit → small mean > 0.
        assert m.edit_distance_norm is not None
        assert 0 < m.edit_distance_norm < 0.1
        assert m.polish_edit_count == 1
        assert m.qa_warnings == 1
        assert m.qa_info == 1
        assert m.qa_blockers == 0
        assert m.mean_confidence == pytest.approx((0.9 + 0.4) / 2)
        assert m.mean_confidence_edited == pytest.approx(0.4)
        assert m.llm_cost_usd == pytest.approx(0.01)
        assert m.prompt_tokens == 1000 and m.completion_tokens == 500
        assert m.prompt_version == "abc123def456"


def test_polish_churn_from_edit_details(factory):
    """polish_churn_norm comes from polish_edit before/after pairs — the
    revert-reference sync makes edit_distance_norm blind to polish edits."""
    import json

    with factory() as session:
        file_id = _seed(session)
        session.add(QaItem(
            file_id=file_id, severity="info", qa_type="polish_edit",
            message="Polish edit (rewrap)", is_resolved=1, created_at=_now(),
            details_json=json.dumps({"before": "Ahoj svete", "after": "Ahoj světe", "reason": "diacritics"}),
        ))
        session.add(QaItem(
            file_id=file_id, severity="info", qa_type="polish_edit",
            message="Polish edit (identical)", is_resolved=1, created_at=_now(),
            details_json=json.dumps({"before": "Nazdar", "after": "Nazdar", "reason": "noop"}),
        ))
        session.commit()

    ctx = JobContext(import_root=Path("."), output_root=Path("."), options=AppOptions())
    cfm.compute_file_metrics({"file_id": file_id}, ctx, _progress)

    with factory() as session:
        m = session.scalar(select(FileQualityMetric))
        assert m.polish_edit_count == 3  # seed row + the two above
        # One substitution out of 10 chars (0.1) and one identical pair (0.0);
        # the detail-less seed row is skipped. Mean over the two parsed pairs.
        assert m.polish_churn_norm == pytest.approx(0.05, abs=0.01)


def test_polish_churn_none_without_details(factory):
    with factory() as session:
        file_id = _seed(session)

    ctx = JobContext(import_root=Path("."), output_root=Path("."), options=AppOptions())
    cfm.compute_file_metrics({"file_id": file_id}, ctx, _progress)

    with factory() as session:
        m = session.scalar(select(FileQualityMetric))
        assert m.polish_churn_norm is None


def test_compute_file_metrics_is_idempotent(factory):
    with factory() as session:
        file_id = _seed(session)

    ctx = JobContext(import_root=Path("."), output_root=Path("."), options=AppOptions())
    cfm.compute_file_metrics({"file_id": file_id}, ctx, _progress)
    cfm.compute_file_metrics({"file_id": file_id}, ctx, _progress)

    with factory() as session:
        rows = list(session.scalars(select(FileQualityMetric)))
        assert len(rows) == 1
