from __future__ import annotations

import json
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
    ProjectGlossaryTerm,
    ProjectSpeaker,
    QaItem,
    SubtitleChunk,
    SubtitleEvent,
)
from app.db.options import AppOptions
from app.jobs.context import JobContext
from app.jobs.handlers import analyze_script as analyze_module
from app.jobs.handlers import audit_chunk_final as audit_module
from app.jobs.handlers import polish_chunk as polish_module
from app.jobs.handlers import repair_chunk as repair_module
from app.jobs.handlers import translate_chunk as translate_module
from app.jobs.handlers import validate_chunk as validate_module
from app.llm.schemas import (
    AnalyzeResponse,
    FinalAuditResponse,
    PolishResponse,
    RepairResponse,
    TranslateResponse,
)


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


def _seed_canonical_prompt_context(session_factory, status: str) -> tuple[int, int]:
    project_id, file_id, _ = _seed(
        session_factory,
        chunk_status=status,
        polish_attempts=2 if status == "final_reviewed" else 0,
    )
    with session_factory() as session:
        luxion = ProjectCharacter(project_id=project_id, name="Luxion", gender=None)
        leon = ProjectCharacter(project_id=project_id, name="Leon Fou Bartfort", gender="male")
        session.add_all([luxion, leon])
        session.flush()
        session.add_all([
            ProjectSpeaker(project_id=project_id, name="LUXION", character_id=luxion.id),
            ProjectSpeaker(project_id=project_id, name="LUXIN", character_id=luxion.id),
            ProjectSpeaker(project_id=project_id, name="LEON", character_id=leon.id),
            ProjectAddressPair(
                project_id=project_id, speaker_name="Luxion",
                addressee_name="Leon Fou Bartfort", mode="tykani", origin="llm", locked=0,
            ),
            # Same canonical direction through raw aliases: locked/manual wins.
            ProjectAddressPair(
                project_id=project_id, speaker_name="LUXION", addressee_name="LEON",
                mode="vykani", origin="manual", locked=1,
            ),
            ProjectAddressPair(
                project_id=project_id, speaker_name="Leon Fou Bartfort",
                addressee_name="Luxion", mode="tykani", origin="manual", locked=1,
            ),
            ProjectGlossaryTerm(
                project_id=project_id, source_term="Leon Fou Bartfort", target_term="Leon",
                category="name", gender="male", vocative="Leone", origin="manual",
                locked=1, is_active=1,
            ),
        ])
        for line, name in enumerate(("LUXION", "LUXIN", "LEON", "UNKNOWN"), start=1):
            event = _event(
                session, file_id, line, f"Source {line}", f"Překlad {line}",
                status="validated",
            )
            event.name = name
        session.commit()
    return project_id, file_id


def _assert_canonical_context(user_prompt: str) -> None:
    assert "Luxion addresses Leon Fou Bartfort: vykani" in user_prompt
    assert "Leon Fou Bartfort addresses Luxion: tykani" in user_prompt
    assert "Luxion addresses Leon Fou Bartfort: tykani" not in user_prompt
    assert "(Luxion)" in user_prompt
    assert "(Leon Fou Bartfort, male)" in user_prompt
    assert "(UNKNOWN)" in user_prompt


def test_canonical_aliases_and_pair_reach_translate(session_factory, monkeypatch):
    project_id, file_id = _seed_canonical_prompt_context(session_factory, "pending")
    captured = {}
    monkeypatch.setattr(translate_module, "SyncSessionLocal", session_factory)
    monkeypatch.setattr(translate_module.tm, "fuzzy_suggest", lambda *_args: {})

    def complete(**kwargs):
        captured.update(kwargs)
        return TranslateResponse(translations=[
            {"i": i, "t": f"Překlad {i}"} for i in range(1, 5)
        ]), _stats()

    monkeypatch.setattr(translate_module.llm_client, "complete", complete)
    result = translate_module.translate_chunk(
        {"file_id": file_id, "chunk_index": 0}, _ctx(), _progress)
    assert result["status"] == "succeeded"
    _assert_canonical_context(captured["user"])

    from app.jobs.handlers.prompt_context import build_speaker_identity_map
    with session_factory() as session:
        identities = build_speaker_identity_map(session, project_id)
    assert identities["LUXION"] == ("Luxion", None)
    assert identities["LUXIN"] == ("Luxion", None)
    assert identities["LEON"] == ("Leon Fou Bartfort", "male")


def test_canonical_aliases_and_pair_reach_polish(session_factory, monkeypatch):
    _, file_id = _seed_canonical_prompt_context(session_factory, "validated")
    captured = {}
    monkeypatch.setattr(polish_module, "SyncSessionLocal", session_factory)

    def complete(**kwargs):
        captured.update(kwargs)
        return PolishResponse(edits=[], issues=[]), _stats()

    monkeypatch.setattr(polish_module.llm_client, "complete", complete)
    result = polish_module.polish_chunk(
        {"file_id": file_id, "chunk_index": 0}, _ctx(), _progress)
    assert result["status"] == "succeeded"
    _assert_canonical_context(captured["user"])


def test_canonical_aliases_and_pair_reach_final_qa(session_factory, monkeypatch):
    _, file_id = _seed_canonical_prompt_context(session_factory, "final_reviewed")
    captured = {}
    monkeypatch.setattr(audit_module, "SyncSessionLocal", session_factory)

    def complete(**kwargs):
        captured.update(kwargs)
        return FinalAuditResponse(issues=[]), _stats()

    monkeypatch.setattr(audit_module.llm_client, "complete", complete)
    result = audit_module.audit_chunk_final(
        {"file_id": file_id, "chunk_index": 0}, _ctx(), _progress)
    assert result["status"] == "succeeded"
    _assert_canonical_context(captured["user"])


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


def test_analyze_user_message_includes_current_glossary_and_address_pairs(
    session_factory, monkeypatch
):
    project_id, file_id, _ = _seed(session_factory)
    with session_factory() as session:
        session.add_all([
            ProjectGlossaryTerm(
                project_id=project_id, source_term="Sky Blade", target_term="Nebeský meč",
                category="technique", origin="manual", locked=1, is_active=1,
            ),
            ProjectAddressPair(
                project_id=project_id, speaker_name="Luxion",
                addressee_name="Leon", mode="vykani", origin="llm", locked=0,
            ),
        ])
        _event(session, file_id, 1, "Use the Sky Blade!")
        session.commit()

    captured = {}

    def complete(**kwargs):
        captured.update(kwargs)
        return AnalyzeResponse(
            synopsis="S", scenes=[], tricky_lines=[], address_pairs=[], suggested_terms=[],
        ), _stats()

    monkeypatch.setattr(analyze_module, "SyncSessionLocal", session_factory)
    monkeypatch.setattr(analyze_module.llm_client, "complete", complete)
    result = analyze_module.analyze_script({"file_id": file_id}, _ctx(), _progress)

    assert result["status"] == "succeeded"
    user = captured["user"]
    assert '## Current Glossary\n- "Sky Blade" => "Nebeský meč"' in user
    assert "## Current Address Pairs\n- Luxion addresses Leon: vykani" in user
    assert user.index("## Current Address Pairs") < user.index("## Script")
    assert captured["schema"] is AnalyzeResponse


def test_analyze_user_message_omits_empty_style_sections(session_factory, monkeypatch):
    _, file_id, _ = _seed(session_factory)
    with session_factory() as session:
        _event(session, file_id, 1, "Hello")
        session.commit()
    captured = {}

    def complete(**kwargs):
        captured.update(kwargs)
        return AnalyzeResponse(
            synopsis="S", scenes=[], tricky_lines=[], address_pairs=[], suggested_terms=[],
        ), _stats()

    monkeypatch.setattr(analyze_module, "SyncSessionLocal", session_factory)
    monkeypatch.setattr(analyze_module.llm_client, "complete", complete)
    analyze_module.analyze_script({"file_id": file_id}, _ctx(), _progress)
    assert "## Current Glossary" not in captured["user"]
    assert "## Current Address Pairs" not in captured["user"]


def test_default_analyze_prompt_is_additive_and_flags_preview_narration():
    from app.db.default_prompts import DEFAULT_ANALYZE_PROMPT as prompt

    assert "Return only NEW terms" in prompt
    assert '"Current Glossary"' in prompt and "authoritative" in prompt
    assert "Return only NEW pairs" in prompt
    assert '"Current Address Pairs"' in prompt
    assert "do not propose a different mode" in prompt
    assert "next-episode preview" in prompt
    assert "teaser" in prompt
    # Output contract unchanged.
    assert '"suggested_terms": [{"source"' in prompt
    assert '"address_pairs": [{"speaker"' in prompt


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


def test_targeted_polish_coordinates_primary_and_optional_support(
    session_factory, monkeypatch,
):
    project_id, file_id, _ = _seed(
        session_factory, chunk_status="needs_polish", polish_attempts=1
    )
    before = {
        120: "Pokud tomu opravdu věříš",
        121: "že tohle je odpověď,",
        122: "pak mi řekni proč.",
        123: "Samostatná věta.",
    }
    with session_factory() as session:
        chunk = session.scalar(select(SubtitleChunk).where(SubtitleChunk.file_id == file_id))
        chunk.translate_to_line = 200
        character = ProjectCharacter(project_id=project_id, name="Alice", gender="female")
        session.add(character)
        session.flush()
        session.add_all([
            ProjectSpeaker(project_id=project_id, name="ALICE", character_id=character.id),
            ProjectSpeaker(project_id=project_id, name="ALYCE", character_id=character.id),
        ])
        rows = {}
        for line, source, name in [
            (120, "If you really believe", "ALICE"),
            (121, "that this is the answer,", "ALYCE"),
            (122, "then tell me why.", "ALICE"),
            (123, "This is separate.", "ALICE"),
        ]:
            rows[line] = _event(
                session, file_id, line, source, before[line], status="validated"
            )
            rows[line].name = name
        rows[124] = _event(
            session, file_id, 124, "Protected context", "Chráněný kontext",
            status="validated", locked=1,
        )
        session.add_all([
            QaItem(file_id=file_id, subtitle_event_id=rows[121].id, severity="warning",
                   qa_type="gender_agreement", message="repair split clause", is_resolved=0),
            QaItem(file_id=file_id, subtitle_event_id=rows[120].id, severity="warning",
                   qa_type="polish_meaning", message="support finding", is_resolved=0),
            QaItem(file_id=file_id, subtitle_event_id=rows[123].id, severity="warning",
                   qa_type="polish_meaning", message="unrelated finding", is_resolved=0),
            QaItem(file_id=file_id, subtitle_event_id=rows[124].id, severity="warning",
                   qa_type="polish_meaning", message="protected finding", is_resolved=0),
        ])
        session.commit()
        ids = {line: row.id for line, row in rows.items()}

    captured = {}
    drift_calls = []
    monkeypatch.setattr(polish_module, "SyncSessionLocal", session_factory)
    monkeypatch.setattr(
        polish_module, "check_polish_drift",
        lambda before_text, after_text, reason, glossary: (
            drift_calls.append((before_text, after_text, reason)) or []
        ),
    )

    def complete(**kwargs):
        captured.update(kwargs)
        return PolishResponse(edits=[
            {"i": 120, "t": "Jestli opravdu věříš", "reason": "cross-event grammar"},
            {"i": 121, "t": "že právě tohle je odpověď,", "reason": "cross-event grammar"},
        ], issues=[]), _stats()

    monkeypatch.setattr(polish_module.llm_client, "complete", complete)
    result = polish_module.polish_chunk(
        {"file_id": file_id, "chunk_index": 0}, _ctx(), _progress
    )

    assert result["status"] == "succeeded"
    prompt = captured["user"]
    assert "[LINE] 121 (Alice, female)" in prompt
    assert "fix: gender_agreement: repair split clause" in prompt
    assert "[LINE] 120 (Alice, female)" in prompt
    assert "support: Adjacent part of a flagged utterance" in prompt
    assert prompt.count("[LINE] 120") == 1
    assert "[LINE] 122 (Alice, female)" in prompt
    assert "[CONTEXT] 123 (Alice)" in prompt
    assert "[CONTEXT] 124" in prompt
    assert "[LINE] 124" not in prompt
    assert len(drift_calls) == 2

    with session_factory() as session:
        assert session.get(SubtitleEvent, ids[120]).translated_text == "Jestli opravdu věříš"
        assert session.get(SubtitleEvent, ids[121]).translated_text == "že právě tohle je odpověď,"
        assert session.get(SubtitleEvent, ids[122]).translated_text == before[122]
        assert session.get(SubtitleEvent, ids[123]).translated_text == before[123]
        assert session.get(SubtitleEvent, ids[124]).translated_text == "Chráněný kontext"
        for line in (120, 121):
            event = session.get(SubtitleEvent, ids[line])
            assert event.original_ai_translated_text == event.translated_text
            assert session.scalar(select(QaItem).where(
                QaItem.subtitle_event_id == ids[line], QaItem.qa_type == "polish_edit"
            )) is not None
        assert session.scalar(select(QaItem).where(
            QaItem.subtitle_event_id == ids[123], QaItem.message == "unrelated finding"
        )) is not None
        assert session.scalar(select(QaItem).where(
            QaItem.subtitle_event_id == ids[124], QaItem.message == "protected finding"
        )) is not None


def test_targeted_polish_ignores_support_rewrite_without_primary_correction(
    session_factory, monkeypatch,
):
    _, file_id, _ = _seed(
        session_factory, chunk_status="needs_polish", polish_attempts=1
    )
    with session_factory() as session:
        support = _event(session, file_id, 1, "If you believe", "Jestli věříš",
                         status="validated")
        primary = _event(session, file_id, 2, "that this is right.", "že je to správně.",
                         status="validated")
        session.add_all([
            QaItem(file_id=file_id, subtitle_event_id=primary.id, severity="warning",
                   qa_type="gender_agreement", message="fix primary", is_resolved=0),
            QaItem(file_id=file_id, subtitle_event_id=support.id, severity="warning",
                   qa_type="polish_meaning", message="retain me", is_resolved=0),
        ])
        session.commit()
        support_id = support.id

    monkeypatch.setattr(polish_module, "SyncSessionLocal", session_factory)
    monkeypatch.setattr(
        polish_module.llm_client, "complete",
        lambda **_kwargs: (
            PolishResponse(edits=[{
                "i": 1, "t": "Když tomu věříš", "reason": "unrelated naturalness",
            }], issues=[]),
            _stats(),
        ),
    )

    result = polish_module.polish_chunk(
        {"file_id": file_id, "chunk_index": 0}, _ctx(), _progress
    )
    assert result["status"] == "succeeded"
    assert result["result"]["edits_applied"] == 0
    with session_factory() as session:
        assert session.get(SubtitleEvent, support_id).translated_text == "Jestli věříš"
        assert session.scalar(select(QaItem).where(
            QaItem.subtitle_event_id == support_id, QaItem.message == "retain me"
        )) is not None


def test_targeted_polish_can_change_only_primary_and_leave_support_untouched(
    session_factory, monkeypatch,
):
    _, file_id, _ = _seed(
        session_factory, chunk_status="needs_polish", polish_attempts=1
    )
    with session_factory() as session:
        support = _event(session, file_id, 1, "If you believe", "Jestli věříš",
                         status="validated")
        primary = _event(session, file_id, 2, "that this is right.", "že je to správný.",
                         status="validated")
        session.add_all([
            QaItem(file_id=file_id, subtitle_event_id=primary.id, severity="warning",
                   qa_type="gender_agreement", message="fix primary", is_resolved=0),
            QaItem(file_id=file_id, subtitle_event_id=support.id, severity="warning",
                   qa_type="polish_meaning", message="retain support QA", is_resolved=0),
        ])
        session.commit()
        support_id, primary_id = support.id, primary.id

    monkeypatch.setattr(polish_module, "SyncSessionLocal", session_factory)
    monkeypatch.setattr(
        polish_module.llm_client, "complete",
        lambda **_kwargs: (
            PolishResponse(edits=[{
                "i": 2, "t": "že je to správné.", "reason": "gender agreement",
            }], issues=[]),
            _stats(),
        ),
    )

    result = polish_module.polish_chunk(
        {"file_id": file_id, "chunk_index": 0}, _ctx(), _progress
    )
    assert result["status"] == "succeeded"
    assert result["result"]["edits_applied"] == 1
    with session_factory() as session:
        assert session.get(SubtitleEvent, support_id).translated_text == "Jestli věříš"
        assert session.get(SubtitleEvent, primary_id).translated_text == "že je to správné."
        assert session.scalar(select(QaItem).where(
            QaItem.subtitle_event_id == support_id, QaItem.message == "retain support QA"
        )) is not None


@pytest.mark.parametrize(("bad_edits", "context_locked"), [
    ([
        {"i": 1, "t": "Pokud věříš", "reason": "grammar"},
        {"i": 2, "t": "Přepsaný kontext.", "reason": "unauthorized"},
    ], False),
    ([
        {"i": 1, "t": "Pokud věříš", "reason": "grammar"},
        {"i": 2, "t": "Přepsaný chráněný kontext.", "reason": "unauthorized"},
    ], True),
    ([
        {"i": 1, "t": "Pokud věříš", "reason": "grammar"},
        {"i": 1, "t": "Jestli tomu věříš", "reason": "duplicate"},
    ], False),
])
def test_invalid_targeted_response_is_atomic(
    session_factory, monkeypatch, bad_edits, context_locked,
):
    _, file_id, _ = _seed(
        session_factory, chunk_status="needs_polish", polish_attempts=1
    )
    with session_factory() as session:
        primary = _event(session, file_id, 1, "If you believe", "Jestli věříš",
                         status="validated")
        context = _event(session, file_id, 2, "Separate sentence.", "Samostatná věta.",
                         status="validated", locked=int(context_locked))
        context.name = "BOB"
        session.add(QaItem(
            file_id=file_id, subtitle_event_id=primary.id, severity="warning",
            qa_type="gender_agreement", message="fix primary", is_resolved=0,
        ))
        session.commit()
        primary_id, context_id = primary.id, context.id

    monkeypatch.setattr(polish_module, "SyncSessionLocal", session_factory)
    monkeypatch.setattr(
        polish_module.llm_client, "complete",
        lambda **_kwargs: (
            PolishResponse(edits=bad_edits, issues=[]),
            _stats(),
        ),
    )

    result = polish_module.polish_chunk(
        {"file_id": file_id, "chunk_index": 0}, _ctx(), _progress
    )
    assert result["status"] == "failed"
    assert result["error_code"] == "INVALID_POLISH_RESPONSE"
    with session_factory() as session:
        assert session.get(SubtitleEvent, primary_id).translated_text == "Jestli věříš"
        assert session.get(SubtitleEvent, context_id).translated_text == "Samostatná věta."
        chunk = session.scalar(select(SubtitleChunk).where(SubtitleChunk.file_id == file_id))
        assert chunk.status == "needs_polish"
        assert chunk.polish_attempt_count == 1


def test_polish_persists_equivalent_negative_rewrite_without_drift(
    session_factory, monkeypatch,
):
    _, file_id, _ = _seed(session_factory, chunk_status="validated")
    before = "Nemáme žádné námitky."
    after = "Všechno je v pořádku."
    with session_factory() as session:
        event = _event(session, file_id, 1, "Then there are no objections.", before,
                       status="validated")
        session.add(QaItem(
            file_id=file_id, subtitle_event_id=event.id, severity="warning",
            qa_type="number_format", message="unrelated", is_resolved=0,
        ))
        session.commit()
        event_id = event.id

    monkeypatch.setattr(polish_module, "SyncSessionLocal", session_factory)
    monkeypatch.setattr(
        polish_module.llm_client,
        "complete",
        lambda **_kwargs: (
            PolishResponse(
                edits=[{"i": 1, "t": after, "reason": "naturalness"}], issues=[]
            ),
            _stats(),
        ),
    )

    result = polish_module.polish_chunk(
        {"file_id": file_id, "chunk_index": 0}, _ctx(), _progress
    )

    assert result["status"] == "succeeded"
    assert result["result"]["drift_flagged"] == 0
    with session_factory() as session:
        event = session.get(SubtitleEvent, event_id)
        items = list(session.scalars(
            select(QaItem).where(QaItem.subtitle_event_id == event_id)
        ))
        assert event.translated_text == after
        assert event.original_ai_translated_text == after
        assert not any(item.qa_type == "polish_drift" for item in items)
        assert any(item.qa_type == "number_format" and not item.is_resolved
                   for item in items)
        history = next(item for item in items if item.qa_type == "polish_edit")
        assert json.loads(history.details_json) == {
            "before": before, "after": after, "reason": "naturalness",
        }
        chunk = session.scalar(
            select(SubtitleChunk).where(SubtitleChunk.file_id == file_id)
        )
        assert chunk.status == "polished"
        assert chunk.polish_attempt_count == 1


def test_polish_persists_narrow_polarity_drift_with_evidence(
    session_factory, monkeypatch,
):
    _, file_id, _ = _seed(session_factory, chunk_status="validated")
    with session_factory() as session:
        event = _event(
            session, file_id, 1, "I know what you want.", "Vím, co chceš.",
            status="validated",
        )
        session.commit()
        event_id = event.id

    monkeypatch.setattr(polish_module, "SyncSessionLocal", session_factory)
    monkeypatch.setattr(
        polish_module.llm_client,
        "complete",
        lambda **_kwargs: (
            PolishResponse(edits=[{
                "i": 1, "t": "Nevím, co chceš.", "reason": "naturalness",
            }], issues=[]),
            _stats(),
        ),
    )

    result = polish_module.polish_chunk(
        {"file_id": file_id, "chunk_index": 0}, _ctx(), _progress
    )

    assert result["status"] == "succeeded"
    assert result["result"]["drift_flagged"] == 1
    with session_factory() as session:
        drift = session.scalar(select(QaItem).where(
            QaItem.subtitle_event_id == event_id,
            QaItem.qa_type == "polish_drift",
        ))
        details = json.loads(drift.details_json)
        assert details["reasons"] == ["negation_changed"]
        assert details["polarity_changes"] == [{
            "positive": "vím", "negative": "nevím", "direction": "added",
        }]
        assert "negation_count" not in details
        assert details["before"] == "Vím, co chceš."
        assert details["after"] == "Nevím, co chceš."
        assert session.scalar(select(QaItem).where(
            QaItem.subtitle_event_id == event_id,
            QaItem.qa_type == "polish_edit",
        )) is not None


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
