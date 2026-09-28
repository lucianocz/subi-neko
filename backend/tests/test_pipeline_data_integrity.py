from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.db.models import (
    File,
    Project,
    ProjectAddressPair,
    ProjectCharacter,
    ProjectSpeaker,
    QaItem,
    SubtitleChunk,
    SubtitleEvent,
)
from app.db.options import AppOptions
from app.jobs.context import JobContext
from app.jobs.handlers import analyze_script as analyze_module
from app.jobs.handlers import polish_chunk as polish_module
from app.jobs.handlers import repair_chunk as repair_module
from app.jobs.handlers import translate_chunk as translate_module
from app.jobs.handlers import validate_chunk as validate_module
from app.llm.schemas import AnalyzeResponse, PolishResponse, RepairResponse, TranslateResponse


@pytest.fixture
def session_factory():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine)
    engine.dispose()


def _ctx() -> JobContext:
    return JobContext(Path("."), Path("."), AppOptions())


def _progress(_fraction: float, _message: str) -> None:
    pass


def _seed(session_factory, *, chunk_status: str = "pending", polish_attempts: int = 0):
    with session_factory() as session:
        project = Project(
            name="P", source_directory="p", anime_provider="anidb", anime_external_id="1"
        )
        session.add(project)
        session.flush()
        file = File(project_id=project.id, filename="e01.mkv", relative_path="e01.mkv")
        session.add(file)
        session.flush()
        chunk = SubtitleChunk(
            file_id=file.id,
            chunk_index=0,
            translate_from_line=1,
            translate_to_line=100,
            content_type="dialogue",
            status=chunk_status,
            polish_attempt_count=polish_attempts,
        )
        session.add(chunk)
        session.commit()
        return project.id, file.id, chunk.id


def _event(session, file_id: int, line: int, source: str, translated: str | None = None,
           *, status: str = "pending", user_edited: int = 0, locked: int = 0):
    event = SubtitleEvent(
        file_id=file_id,
        line_index=line,
        event_type="dialogue",
        content_type="dialogue",
        layer=0,
        start_ms=line * 1000,
        end_ms=line * 1000 + 1000,
        style="Default",
        source_text=source,
        translated_text=translated,
        translation_status=status,
        is_user_edited=user_edited,
        is_locked=locked,
    )
    session.add(event)
    session.flush()
    return event


def _stats():
    return SimpleNamespace(
        response_mode="json_schema", prompt_tokens=1, completion_tokens=1
    )


def test_episode_analysis_preserves_canonical_pair_and_adds_new_direction(
    session_factory, monkeypatch
):
    project_id, file_id, _ = _seed(session_factory)
    with session_factory() as session:
        luxion = ProjectCharacter(project_id=project_id, name="Luxion")
        leon = ProjectCharacter(project_id=project_id, name="Leon Fou Bartfort")
        session.add_all([luxion, leon])
        session.flush()
        session.add_all([
            ProjectSpeaker(project_id=project_id, name="LUXION", character_id=luxion.id),
            ProjectSpeaker(project_id=project_id, name="LEON", character_id=leon.id),
            ProjectAddressPair(
                project_id=project_id,
                speaker_name="Luxion",
                addressee_name="Leon Fou Bartfort",
                mode="vykani",
                origin="llm",
                locked=0,
            ),
        ])
        _event(session, file_id, 1, "Hello")
        session.commit()

    monkeypatch.setattr(analyze_module, "SyncSessionLocal", session_factory)
    monkeypatch.setattr(
        analyze_module.llm_client,
        "complete",
        lambda **_kwargs: (
            AnalyzeResponse(
                synopsis="Synopsis",
                scenes=[],
                tricky_lines=[],
                address_pairs=[
                    {"speaker": "LUXION", "addressee": "LEON", "mode": "tykani"},
                    {"speaker": "LEON", "addressee": "LUXION", "mode": "tykani"},
                ],
                suggested_terms=[],
            ),
            _stats(),
        ),
    )

    result = analyze_module.analyze_script({"file_id": file_id}, _ctx(), _progress)

    assert result["status"] == "succeeded"
    with session_factory() as session:
        pairs = list(session.scalars(
            select(ProjectAddressPair).order_by(ProjectAddressPair.speaker_name)
        ))
        assert {(p.speaker_name, p.addressee_name, p.mode) for p in pairs} == {
            ("Luxion", "Leon Fou Bartfort", "vykani"),
            ("Leon Fou Bartfort", "Luxion", "tykani"),
        }


def test_targeted_polish_cleans_only_processed_events_and_keeps_audit(
    session_factory, monkeypatch
):
    _, file_id, _ = _seed(
        session_factory, chunk_status="needs_polish", polish_attempts=1
    )
    with session_factory() as session:
        event50 = _event(session, file_id, 50, "Fifty", "Padesát", status="validated")
        event90 = _event(session, file_id, 90, "Ninety", "Devadesát", status="validated")
        session.add_all([
            QaItem(file_id=file_id, subtitle_event_id=event50.id, severity="warning",
                   qa_type="polish_meaning", message="keep me", is_resolved=0),
            QaItem(file_id=file_id, subtitle_event_id=event90.id, severity="warning",
                   qa_type="gender_agreement", message="fix gender", is_resolved=0),
            QaItem(file_id=file_id, subtitle_event_id=event90.id, severity="warning",
                   qa_type="polish_naturalness", message="stale", is_resolved=0),
            QaItem(file_id=file_id, subtitle_event_id=event90.id, severity="info",
                   qa_type="polish_edit", message="audit", is_resolved=1),
        ])
        session.commit()
        event50_id, event90_id = event50.id, event90.id

    monkeypatch.setattr(polish_module, "SyncSessionLocal", session_factory)
    monkeypatch.setattr(
        polish_module.llm_client,
        "complete",
        lambda **_kwargs: (
            PolishResponse(
                edits=[{"i": 90, "t": "Opraveno", "reason": "gender"}],
                issues=[{"i": 90, "severity": "warning", "category": "meaning",
                         "comment": "new issue"}],
            ),
            _stats(),
        ),
    )

    result = polish_module.polish_chunk(
        {"file_id": file_id, "chunk_index": 0}, _ctx(), _progress
    )

    assert result["status"] == "succeeded"
    with session_factory() as session:
        event50_issues = list(session.scalars(
            select(QaItem).where(QaItem.subtitle_event_id == event50_id)
        ))
        event90_issues = list(session.scalars(
            select(QaItem).where(QaItem.subtitle_event_id == event90_id)
        ))
        assert [(q.qa_type, q.message, q.is_resolved) for q in event50_issues] == [
            ("polish_meaning", "keep me", 0)
        ]
        assert not any(q.message == "stale" for q in event90_issues)
        assert sum(q.qa_type == "polish_meaning" and not q.is_resolved
                   for q in event90_issues) == 1
        assert any(q.qa_type == "polish_edit" and q.message == "audit"
                   for q in event90_issues)
        assert any(q.qa_type == "polish_edit" and q.message != "audit"
                   for q in event90_issues)
        assert next(q for q in event90_issues if q.qa_type == "gender_agreement").is_resolved

    # A later targeted run replaces the processed row's issue instead of
    # accumulating another unresolved duplicate.
    with session_factory() as session:
        chunk = session.scalar(select(SubtitleChunk).where(SubtitleChunk.file_id == file_id))
        chunk.status = "needs_polish"
        session.add(QaItem(
            file_id=file_id, subtitle_event_id=event90_id, severity="warning",
            qa_type="gender_agreement", message="fix again", is_resolved=0,
        ))
        session.commit()
    polish_module.polish_chunk(
        {"file_id": file_id, "chunk_index": 0}, _ctx(), _progress
    )
    with session_factory() as session:
        assert session.scalar(select(QaItem).where(
            QaItem.subtitle_event_id == event50_id,
            QaItem.qa_type == "polish_meaning",
            QaItem.is_resolved == 0,
        )) is not None
        assert len(list(session.scalars(select(QaItem).where(
            QaItem.subtitle_event_id == event90_id,
            QaItem.qa_type == "polish_meaning",
            QaItem.is_resolved == 0,
        )))) == 1


def test_empty_targeted_polish_response_keeps_unrelated_finding(
    session_factory, monkeypatch
):
    _, file_id, _ = _seed(
        session_factory, chunk_status="needs_polish", polish_attempts=1
    )
    with session_factory() as session:
        event50 = _event(session, file_id, 50, "Fifty", "Padesát", status="validated")
        event90 = _event(session, file_id, 90, "Ninety", "Devadesát", status="validated")
        session.add_all([
            QaItem(file_id=file_id, subtitle_event_id=event50.id, severity="warning",
                   qa_type="polish_meaning", message="keep me", is_resolved=0),
            QaItem(file_id=file_id, subtitle_event_id=event90.id, severity="warning",
                   qa_type="gender_agreement", message="fix gender", is_resolved=0),
            QaItem(file_id=file_id, subtitle_event_id=event90.id, severity="warning",
                   qa_type="polish_meaning", message="stale", is_resolved=0),
        ])
        session.commit()
        event50_id, event90_id = event50.id, event90.id

    monkeypatch.setattr(polish_module, "SyncSessionLocal", session_factory)
    monkeypatch.setattr(
        polish_module.llm_client,
        "complete",
        lambda **_kwargs: (PolishResponse(edits=[], issues=[]), _stats()),
    )

    polish_module.polish_chunk({"file_id": file_id, "chunk_index": 0}, _ctx(), _progress)

    with session_factory() as session:
        assert session.scalar(select(QaItem).where(
            QaItem.subtitle_event_id == event50_id,
            QaItem.qa_type == "polish_meaning",
            QaItem.is_resolved == 0,
        )) is not None
        assert session.scalar(select(QaItem).where(
            QaItem.subtitle_event_id == event90_id,
            QaItem.qa_type == "polish_meaning",
            QaItem.is_resolved == 0,
        )) is None


def test_translate_skips_locked_and_user_edited_events(session_factory, monkeypatch):
    _, file_id, _ = _seed(session_factory)
    with session_factory() as session:
        locked = _event(session, file_id, 1, "Locked", "Zamčeno", locked=1)
        edited = _event(session, file_id, 2, "Edited", "Ručně", user_edited=1)
        normal = _event(session, file_id, 3, "Normal")
        session.commit()
        ids = locked.id, edited.id, normal.id

    monkeypatch.setattr(translate_module, "SyncSessionLocal", session_factory)
    monkeypatch.setattr(translate_module.tm, "fuzzy_suggest", lambda *_args: {})
    monkeypatch.setattr(
        translate_module.llm_client,
        "complete",
        lambda **_kwargs: (
            TranslateResponse(translations=[{"i": 1, "t": "Přepsáno"},
                                            {"i": 2, "t": "Přepsáno"},
                                            {"i": 3, "t": "Normální"}]),
            _stats(),
        ),
    )

    translate_module.translate_chunk({"file_id": file_id, "chunk_index": 0}, _ctx(), _progress)

    with session_factory() as session:
        rows = [session.get(SubtitleEvent, event_id) for event_id in ids]
        assert [row.translated_text for row in rows] == ["Zamčeno", "Ručně", "Normální"]


def test_translate_rechecks_lock_before_persistence(session_factory, monkeypatch):
    _, file_id, _ = _seed(session_factory)
    with session_factory() as session:
        event = _event(session, file_id, 1, "Hello", "Original")
        session.commit()
        event_id = event.id

    monkeypatch.setattr(translate_module, "SyncSessionLocal", session_factory)
    monkeypatch.setattr(translate_module.tm, "fuzzy_suggest", lambda *_args: {})

    def complete(**_kwargs):
        with session_factory() as session:
            current = session.get(SubtitleEvent, event_id)
            current.is_locked = 1
            session.commit()
        return TranslateResponse(translations=[{"i": 1, "t": "Changed"}]), _stats()

    monkeypatch.setattr(translate_module.llm_client, "complete", complete)
    translate_module.translate_chunk({"file_id": file_id, "chunk_index": 0}, _ctx(), _progress)

    with session_factory() as session:
        assert session.get(SubtitleEvent, event_id).translated_text == "Original"


def test_repair_rechecks_lock_before_persistence(session_factory, monkeypatch):
    _, file_id, _ = _seed(session_factory, chunk_status="validate_trans_failed")
    with session_factory() as session:
        event = _event(session, file_id, 1, "Hello", "Faulty", status="rejected")
        session.commit()
        event_id = event.id

    monkeypatch.setattr(repair_module, "SyncSessionLocal", session_factory)

    def complete(**_kwargs):
        with session_factory() as session:
            current = session.get(SubtitleEvent, event_id)
            current.is_locked = 1
            session.commit()
        return RepairResponse(repairs=[{"i": 1, "t": "Repaired"}]), _stats()

    monkeypatch.setattr(repair_module.llm_client, "complete", complete)
    repair_module.repair_chunk({"file_id": file_id, "chunk_index": 0}, _ctx(), _progress)

    with session_factory() as session:
        assert session.get(SubtitleEvent, event_id).translated_text == "Faulty"


def test_polish_keeps_locked_and_user_edited_events(session_factory, monkeypatch):
    _, file_id, _ = _seed(session_factory, chunk_status="validated")
    with session_factory() as session:
        locked = _event(session, file_id, 1, "Locked", "Zamčeno", locked=1)
        edited = _event(session, file_id, 2, "Edited", "Ručně", user_edited=1)
        session.commit()
        ids = locked.id, edited.id

    monkeypatch.setattr(polish_module, "SyncSessionLocal", session_factory)

    def unexpected_call(**_kwargs):
        raise AssertionError("protected-only Polish pass must not call the LLM")

    monkeypatch.setattr(polish_module.llm_client, "complete", unexpected_call)
    result = polish_module.polish_chunk(
        {"file_id": file_id, "chunk_index": 0}, _ctx(), _progress
    )

    assert result["status"] == "succeeded"
    with session_factory() as session:
        rows = [session.get(SubtitleEvent, event_id) for event_id in ids]
        chunk = session.scalar(select(SubtitleChunk).where(SubtitleChunk.file_id == file_id))
        assert [row.translated_text for row in rows] == ["Zamčeno", "Ručně"]
        assert chunk.status == "polished"


def test_protected_only_chunk_validates_without_repair_loop(session_factory, monkeypatch):
    _, file_id, _ = _seed(session_factory, chunk_status="translated")
    with session_factory() as session:
        event = _event(session, file_id, 1, "Hello", None, locked=1)
        session.commit()
        event_id = event.id

    monkeypatch.setattr(validate_module, "SyncSessionLocal", session_factory)
    result = validate_module.validate_chunk(
        {"file_id": file_id, "chunk_index": 0}, _ctx(), _progress
    )

    assert result["status"] == "succeeded"
    assert result["result"]["valid"] is True
    with session_factory() as session:
        event = session.get(SubtitleEvent, event_id)
        chunk = session.scalar(select(SubtitleChunk).where(SubtitleChunk.file_id == file_id))
        assert event.translation_status == "validated"
        assert chunk.status == "validated"
        assert session.scalar(select(QaItem).where(
            QaItem.subtitle_event_id == event_id,
            QaItem.qa_type == "missing_translation",
        )) is not None
