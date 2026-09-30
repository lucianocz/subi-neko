from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.db.models import (
    File, Project, ProjectAddressPair, ProjectCharacter, ProjectSpeaker,
    ProjectStyleBible, SubtitleEvent,
)
from app.jobs.handlers import style_bible
from app.llm.schemas import AddressPairOut, StyleBibleResponse, StyleBibleUpdateResponse


def _now() -> str:
    return datetime.utcnow().isoformat()


def test_update_prompt_supplies_episode_mapping_and_cannot_replace_pair(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as session:
        project = Project(
            name="P", source_directory="p", anime_provider="test", anime_external_id="1",
            created_at=_now(), updated_at=_now(),
        )
        session.add(project)
        session.flush()
        file = File(
            project_id=project.id, filename="e1.mkv", relative_path="e1.mkv",
            created_at=_now(), updated_at=_now(),
        )
        luxion = ProjectCharacter(
            project_id=project.id, name="Luxion", created_at=_now(), updated_at=_now())
        leon = ProjectCharacter(
            project_id=project.id, name="Leon Fou Bartfort", created_at=_now(), updated_at=_now())
        session.add_all([file, luxion, leon])
        session.flush()
        session.add_all([
            ProjectStyleBible(
                project_id=project.id, version=1, tone_summary="Measured adventure",
                register_notes="Keep ranks distinct", honorific_policy="Preserve -sama",
                created_at=_now(), updated_at=_now()),
            ProjectSpeaker(project_id=project.id, name="LUXION", character_id=luxion.id,
                           created_at=_now(), updated_at=_now()),
            ProjectSpeaker(project_id=project.id, name="LEON", character_id=leon.id,
                           created_at=_now(), updated_at=_now()),
            ProjectAddressPair(
                project_id=project.id, speaker_name="Luxion",
                addressee_name="Leon Fou Bartfort", mode="vykani", origin="llm", locked=0,
                created_at=_now(), updated_at=_now()),
            SubtitleEvent(
                file_id=file.id, line_index=0, event_type="dialogue", content_type="dialogue",
                layer=0, start_ms=0, end_ms=1000, style="Default", name="LUXION",
                source_text="Leon.", translated_text="Leone.", translation_status="translated",
                created_at=_now(), updated_at=_now()),
        ])
        session.commit()
        project_id, file_id = project.id, file.id

    captured: dict[str, str] = {}

    def fake_complete(**kwargs):
        captured["user"] = kwargs["user"]
        return (
            StyleBibleUpdateResponse(
                terms=[], character_voices=[],
                address_pairs=[AddressPairOut(
                    speaker="LUXION", addressee="LEON", mode="tykani")],
            ),
            SimpleNamespace(response_mode="json_schema"),
        )

    monkeypatch.setattr(style_bible, "SyncSessionLocal", factory)
    monkeypatch.setattr(style_bible.llm_client, "complete", fake_complete)
    options = SimpleNamespace(
        openai_model_better="better", openai_model_cheap="cheap",
        style_bible_character_description_max=600,
        style_bible_character_description_budget=32000,
        resolved_style_bible_update_prompt=lambda: "update prompt",
    )

    result = style_bible.update_style_bible(
        {"project_id": project_id, "file_id": file_id},
        SimpleNamespace(options=options), lambda *_: None,
    )
    repeated = style_bible.update_style_bible(
        {"project_id": project_id, "file_id": file_id},
        SimpleNamespace(options=options), lambda *_: None,
    )

    assert result["status"] == "succeeded"
    assert result["result"]["address_pairs"] == 0
    assert repeated["result"]["address_pairs"] == 0
    assert "## Speaker Identity Mapping" in captured["user"]
    assert "LUXION → Luxion" in captured["user"]
    assert "## Current Approved Style Bible" in captured["user"]
    assert "tone_summary: Measured adventure" in captured["user"]
    assert "register_notes: Keep ranks distinct" in captured["user"]
    assert "honorific_policy: Preserve -sama" in captured["user"]
    assert "[LINE 0]" in captured["user"]
    assert "speaker: LUXION" in captured["user"]
    assert "EN: Leon." in captured["user"]
    assert "CS: Leone." in captured["user"]
    assert captured["user"].index("EN: Leon.") < captured["user"].index("CS: Leone.")
    assert "canonical_name: Luxion" in captured["user"]
    assert "Existing address pairs are authoritative" in captured["user"]
    with factory() as session:
        rows = list(session.scalars(select(ProjectAddressPair)))
        assert len(rows) == 1
        assert rows[0].mode == "vykani"

    engine.dispose()


def test_initial_style_bible_receives_rich_separately_budgeted_character_context(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as session:
        project = Project(name="P", source_directory="style-budget", anime_provider="test",
                          anime_external_id="1", created_at=_now(), updated_at=_now())
        session.add(project)
        session.flush()
        file = File(project_id=project.id, filename="e1.mkv", relative_path="e1.mkv",
                    created_at=_now(), updated_at=_now())
        character = ProjectCharacter(
            project_id=project.id, external_id="c1", name="Aria", gender="female",
            role="MAIN", aliases="Hero, Princess", character_type="Human",
            voice_actor="Jane Actor", social_position="Princess", note="Uses formal speech",
            description="First sentence about Aria. " + "x" * 200,
            created_at=_now(), updated_at=_now())
        session.add_all([file, character])
        session.flush()
        session.add_all([
            ProjectSpeaker(project_id=project.id, name="ARIA", character_id=character.id,
                           created_at=_now(), updated_at=_now()),
            SubtitleEvent(file_id=file.id, line_index=0, event_type="dialogue",
                          content_type="dialogue", layer=0, start_ms=0, end_ms=1000,
                          style="Default", name="ARIA", source_text="Hello.",
                          created_at=_now(), updated_at=_now()),
        ])
        session.commit()
        project_id, file_id = project.id, file.id

    captured = {}
    def fake_complete(**kwargs):
        captured.update(kwargs)
        return StyleBibleResponse(
            tone_summary="tone", register_notes="register", honorific_policy="policy",
            terms=[], character_voices=[], address_pairs=[]), SimpleNamespace(response_mode="json_schema")

    monkeypatch.setattr(style_bible, "SyncSessionLocal", factory)
    monkeypatch.setattr(style_bible.llm_client, "complete", fake_complete)
    options = SimpleNamespace(
        openai_model_better="better", openai_model_cheap="cheap",
        style_bible_character_description_max=300,
        style_bible_character_description_budget=300,
        llm_max_completion_tokens=32768,
        resolved_style_bible_prompt=lambda: "style prompt",
    )
    result = style_bible.generate_style_bible(
        {"project_id": project_id, "sample_file_id": file_id},
        SimpleNamespace(options=options), lambda *_: None,
    )
    assert result["status"] == "succeeded"
    user = captured["user"]
    assert "external_id: c1" in user and "canonical_name: Aria" in user
    for expected in ("gender: female", "role: MAIN", "aliases: Hero, Princess",
                     "character_type: Human", "voice_actor: Jane Actor",
                     "social_position: Princess", "notes: Uses formal speech"):
        assert expected in user
    description_part = user.split("description: ", 1)[1].split("\n", 1)[0]
    assert len(description_part) > 200
    assert description_part == "First sentence about Aria. " + "x" * 200
    engine.dispose()


def test_update_dialogue_evidence_preserves_chronological_order(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as session:
        project = Project(name="P", source_directory="ordered", anime_provider="test",
                          anime_external_id="1", created_at=_now(), updated_at=_now())
        session.add(project)
        session.flush()
        file = File(project_id=project.id, filename="e.mkv", relative_path="e.mkv",
                    created_at=_now(), updated_at=_now())
        session.add(file)
        session.flush()
        for index, source, translated in [(20, "Second.", "Druhá."), (10, "First.", "První.")]:
            session.add(SubtitleEvent(
                file_id=file.id, line_index=index, event_type="dialogue",
                content_type="dialogue", layer=0, start_ms=index, end_ms=index + 1,
                style="Default", name="N", source_text=source,
                translated_text=translated, created_at=_now(), updated_at=_now()))
        session.commit()
        project_id, file_id = project.id, file.id

    captured = {}
    monkeypatch.setattr(style_bible, "SyncSessionLocal", factory)
    monkeypatch.setattr(style_bible.llm_client, "complete", lambda **kwargs: (
        captured.update(kwargs) or StyleBibleUpdateResponse(
            terms=[], character_voices=[], address_pairs=[]),
        SimpleNamespace(response_mode="json_schema"),
    ))
    options = SimpleNamespace(
        openai_model_better="better", openai_model_cheap="cheap",
        style_bible_character_description_max=600,
        style_bible_character_description_budget=32000,
        resolved_style_bible_update_prompt=lambda: "update prompt",
    )
    result = style_bible.update_style_bible(
        {"project_id": project_id, "file_id": file_id},
        SimpleNamespace(options=options), lambda *_: None,
    )
    assert result["status"] == "succeeded"
    assert captured["user"].index("[LINE 10]") < captured["user"].index("[LINE 20]")
    engine.dispose()


def test_initial_style_bible_completion_budget_is_scaled_bounded_and_deterministic():
    assert style_bible.style_bible_completion_budget(0, 0, 32768) == 4096
    expected = 4096 + 10 * 128 + 4 * 96
    assert style_bible.style_bible_completion_budget(10, 4, 32768) == expected
    assert style_bible.style_bible_completion_budget(1000, 1000, 32768) == 16000
    assert style_bible.style_bible_completion_budget(1000, 1000, 6000) == 6000


def test_initial_style_bible_excludes_tagged_speaker_evidence_and_budget_count(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as session:
        project = Project(name="P", source_directory="tag-filter", anime_provider="test",
                          anime_external_id="1", created_at=_now(), updated_at=_now())
        session.add(project)
        session.flush()
        file = File(project_id=project.id, filename="e.mkv", relative_path="e.mkv",
                    created_at=_now(), updated_at=_now())
        character = ProjectCharacter(
            project_id=project.id, external_id="c1", name="Aria",
            created_at=_now(), updated_at=_now())
        session.add_all([file, character])
        session.flush()
        session.add_all([
            ProjectSpeaker(project_id=project.id, name="ARIA", character_id=character.id,
                           created_at=_now(), updated_at=_now()),
            ProjectSpeaker(project_id=project.id, name="MYSTERY",
                           created_at=_now(), updated_at=_now()),
            ProjectSpeaker(project_id=project.id, name="SIGN CENTER", content_tag="sign",
                           is_extra=1, created_at=_now(), updated_at=_now()),
        ])
        for index, name, source in [
            (1, "ARIA", "Ordinary mapped dialogue."),
            (2, "SIGN CENTER", "Typesetting text that must be excluded."),
            (3, "MYSTERY", "Ordinary unmapped dialogue."),
        ]:
            # These deliberately still look like dialogue at event level;
            # content_tag propagation happens later during chunk planning.
            session.add(SubtitleEvent(
                file_id=file.id, line_index=index, event_type="dialogue",
                content_type="dialogue", layer=0, start_ms=index, end_ms=index + 1,
                style="Default", name=name, source_text=source,
                created_at=_now(), updated_at=_now()))
        session.commit()
        project_id, file_id = project.id, file.id

    captured = {}
    monkeypatch.setattr(style_bible, "SyncSessionLocal", factory)
    monkeypatch.setattr(style_bible.llm_client, "complete", lambda **kwargs: (
        captured.update(kwargs) or StyleBibleResponse(
            tone_summary="tone", register_notes="register", honorific_policy="policy",
            terms=[], character_voices=[], address_pairs=[]),
        SimpleNamespace(response_mode="json_schema"),
    ))
    options = SimpleNamespace(
        openai_model_better="better", openai_model_cheap="cheap",
        style_bible_character_description_max=600,
        style_bible_character_description_budget=32000,
        llm_max_completion_tokens=32768,
        resolved_style_bible_prompt=lambda: "style prompt",
    )

    result = style_bible.generate_style_bible(
        {"project_id": project_id, "sample_file_id": file_id},
        SimpleNamespace(options=options), lambda *_: None,
    )

    assert result["status"] == "succeeded"
    assert "Ordinary mapped dialogue." in captured["user"]
    assert "Ordinary unmapped dialogue." in captured["user"]
    assert "Typesetting text that must be excluded." not in captured["user"]
    assert captured["max_completion_tokens"] == style_bible.style_bible_completion_budget(
        1, 2, 32768)
    engine.dispose()
