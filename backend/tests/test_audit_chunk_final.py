from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.db.models import File, Project, QaItem, SubtitleChunk, SubtitleEvent
from app.db.options import AppOptions
from app.jobs.context import JobContext
from app.jobs.handlers import audit_chunk_final as audit_module
from app.llm.client import LlmError
from app.llm.schemas import FinalAuditResponse


@pytest.fixture
def session_factory():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine)
    engine.dispose()


def _ctx() -> JobContext:
    return JobContext(Path("."), Path("."), AppOptions(target_lang_name="Czech"))


def _progress(_fraction: float, _message: str) -> None:
    pass


def _seed(session_factory, *, status: str = "final_reviewed") -> tuple[int, int, list[int]]:
    with session_factory() as session:
        project = Project(
            name="P", source_directory="p", anime_provider="anidb", anime_external_id="1"
        )
        session.add(project)
        session.flush()
        file = File(project_id=project.id, filename="e01.mkv", relative_path="e01.mkv")
        session.add(file)
        session.flush()
        session.add(SubtitleChunk(
            file_id=file.id, chunk_index=0, translate_from_line=10, translate_to_line=11,
            content_type="dialogue", status=status, polish_attempt_count=2,
        ))
        events = [
            SubtitleEvent(
                file_id=file.id, line_index=10, event_type="dialogue", content_type="dialogue",
                layer=0, start_ms=0, end_ms=1000, style="Default", name="ALICE",
                source_text="I did not do it.", translated_text="Já jsem to udělala.",
                original_ai_translated_text="Starší verze", translation_status="validated",
                is_locked=1,
            ),
            SubtitleEvent(
                file_id=file.id, line_index=11, event_type="dialogue", content_type="dialogue",
                layer=0, start_ms=1000, end_ms=2000, style="Default", name="BOB",
                source_text="Believe me.", translated_text="Věř mi.",
                original_ai_translated_text="Jiný koncept", translation_status="validated",
                is_user_edited=1,
            ),
        ]
        session.add_all(events)
        session.commit()
        return project.id, file.id, [event.id for event in events]


def _stats():
    return SimpleNamespace(response_mode="json_schema", prompt_tokens=12, completion_tokens=5)


def _run(session_factory, monkeypatch, response: FinalAuditResponse):
    _, file_id, event_ids = _seed(session_factory)
    captured = {}
    monkeypatch.setattr(audit_module, "SyncSessionLocal", session_factory)

    def complete(**kwargs):
        captured.update(kwargs)
        return response, _stats()

    monkeypatch.setattr(audit_module.llm_client, "complete", complete)
    result = audit_module.audit_chunk_final(
        {"file_id": file_id, "chunk_index": 0}, _ctx(), _progress
    )
    return file_id, event_ids, captured, result


def test_empty_response_audits_without_modifying_protected_final_text(session_factory, monkeypatch):
    file_id, event_ids, captured, result = _run(
        session_factory, monkeypatch, FinalAuditResponse(issues=[])
    )

    assert result["status"] == "succeeded"
    assert captured["task"] == "final_audit"
    assert captured["model"] == "gpt-5.6-terra"
    assert "CZ: Já jsem to udělala." in captured["user"]
    assert "Starší verze" not in captured["user"]
    with session_factory() as session:
        chunk = session.scalar(select(SubtitleChunk).where(SubtitleChunk.file_id == file_id))
        events = [session.get(SubtitleEvent, event_id) for event_id in event_ids]
        assert chunk.status == "audited"
        assert chunk.polish_attempt_count == 2
        assert [event.translated_text for event in events] == ["Já jsem to udělala.", "Věř mi."]
        assert [event.original_ai_translated_text for event in events] == [
            "Starší verze", "Jiný koncept",
        ]


def test_findings_are_namespaced_and_preserve_existing_qa(session_factory, monkeypatch):
    _, file_id, event_ids = _seed(session_factory)
    with session_factory() as session:
        session.add_all([
            QaItem(file_id=file_id, subtitle_event_id=event_ids[0], severity="warning",
                   qa_type="gender_agreement", message="existing review", is_resolved=0),
            QaItem(file_id=file_id, subtitle_event_id=event_ids[1], severity="info",
                   qa_type="polish_edit", message="history", is_resolved=1),
        ])
        session.commit()

    monkeypatch.setattr(audit_module, "SyncSessionLocal", session_factory)
    response = FinalAuditResponse(issues=[
        {"i": 10, "category": "meaning", "severity": "warning",
         "explanation": "The English is negated, but the Czech says she did it.",
         "suggestion": "Já jsem to neudělala."},
        {"i": 11, "category": "ambiguity", "severity": "info",
         "explanation": "The addressee is context-dependent and should be checked.",
         "suggestion": None},
        {"i": 10, "category": "cross_event", "severity": "warning",
         "explanation": "The utterance spanning lines 10 and 11 loses its second clause.",
         "suggestion": None},
    ])
    monkeypatch.setattr(audit_module.llm_client, "complete", lambda **_kwargs: (response, _stats()))

    for expected_status in ("final_reviewed", "audited"):
        with session_factory() as session:
            chunk = session.scalar(select(SubtitleChunk).where(SubtitleChunk.file_id == file_id))
            chunk.status = expected_status
            session.commit()
        result = audit_module.audit_chunk_final(
            {"file_id": file_id, "chunk_index": 0}, _ctx(), _progress
        )
        assert result["status"] == "succeeded"

    with session_factory() as session:
        items = list(session.scalars(select(QaItem).where(QaItem.file_id == file_id)))
        assert any(item.qa_type == "gender_agreement" for item in items)
        assert any(item.qa_type == "polish_edit" for item in items)
        unresolved_audit = [item for item in items
                            if item.qa_type.startswith("final_audit_") and not item.is_resolved]
        assert sorted(item.qa_type for item in unresolved_audit) == [
            "final_audit_ambiguity", "final_audit_cross_event", "final_audit_meaning",
        ]
        meaning = next(item for item in unresolved_audit if item.qa_type == "final_audit_meaning")
        assert "ne udělala" not in (meaning.details_json or "")
        assert "Já jsem to neudělala." in meaning.details_json
        cross_event = next(item for item in unresolved_audit
                           if item.qa_type == "final_audit_cross_event")
        assert "lines 10 and 11" in cross_event.message


@pytest.mark.parametrize("bad_issue", [
    {"i": 999, "category": "grammar", "severity": "warning",
     "explanation": "A concrete defect.", "suggestion": None},
    {"i": 10, "category": "style", "severity": "warning",
     "explanation": "A concrete defect.", "suggestion": None},
    {"i": 10, "category": "grammar", "severity": "blocker",
     "explanation": "A concrete defect.", "suggestion": None},
    {"i": 10, "category": "grammar", "severity": "warning",
     "explanation": " ", "suggestion": None},
])
def test_invalid_findings_fail_without_persistence(session_factory, monkeypatch, bad_issue):
    _, file_id, _ = _seed(session_factory)
    monkeypatch.setattr(audit_module, "SyncSessionLocal", session_factory)
    response = FinalAuditResponse(issues=[bad_issue])
    monkeypatch.setattr(audit_module.llm_client, "complete", lambda **_kwargs: (response, _stats()))

    result = audit_module.audit_chunk_final(
        {"file_id": file_id, "chunk_index": 0}, _ctx(), _progress
    )

    assert result["status"] == "failed"
    assert result["error_code"] == "RESPONSE_VALIDATION_ERROR"
    with session_factory() as session:
        chunk = session.scalar(select(SubtitleChunk).where(SubtitleChunk.file_id == file_id))
        assert chunk.status == "final_reviewed"
        assert list(session.scalars(select(QaItem).where(
            QaItem.qa_type.like("final_audit_%")
        ))) == []


def test_llm_failure_does_not_mark_chunk_audited(session_factory, monkeypatch):
    _, file_id, _ = _seed(session_factory)
    monkeypatch.setattr(audit_module, "SyncSessionLocal", session_factory)

    def fail(**_kwargs):
        raise LlmError("OPENAI_API_ERROR", "temporary failure")

    monkeypatch.setattr(audit_module.llm_client, "complete", fail)
    result = audit_module.audit_chunk_final(
        {"file_id": file_id, "chunk_index": 0}, _ctx(), _progress
    )
    assert result["status"] == "failed"
    with session_factory() as session:
        chunk = session.scalar(select(SubtitleChunk).where(SubtitleChunk.file_id == file_id))
        assert chunk.status == "final_reviewed"


def test_schema_accepts_required_variants_and_rejects_malformed():
    parsed = FinalAuditResponse.model_validate({"issues": [
        {"i": 1, "category": "grammar", "severity": "warning",
         "explanation": "Wrong case government.", "suggestion": "Oprava"},
        {"i": 2, "category": "meaning", "severity": "warning",
         "explanation": "Agency is reversed.", "suggestion": "Oprava"},
        {"i": 3, "category": "ambiguity", "severity": "info",
         "explanation": "The reference may point elsewhere.", "suggestion": None},
    ]})
    assert len(parsed.issues) == 3
    assert FinalAuditResponse.model_validate({"issues": []}).issues == []
    with pytest.raises(ValidationError):
        FinalAuditResponse.model_validate({"issues": [{"i": 1, "category": "grammar"}]})
