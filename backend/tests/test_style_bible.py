from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.db.models import (
    File, Project, ProjectAddressPair, ProjectCharacter, ProjectSpeaker, SubtitleEvent,
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
        style_bible_character_description_max=50,
        style_bible_character_description_budget=50,
        resolved_style_bible_prompt=lambda: "style prompt",
    )
    result = style_bible.generate_style_bible(
        {"project_id": project_id, "sample_file_id": file_id},
        SimpleNamespace(options=options), lambda *_: None,
    )
    assert result["status"] == "succeeded"
    user = captured["user"]
    assert "id=c1, name=Aria" in user
    for expected in ("gender=female", "role=MAIN", "aliases=Hero, Princess",
                     "type=Human", "voice actor=Jane Actor",
                     "social position=Princess", "note=Uses formal speech"):
        assert expected in user
    description_part = user.split(" — ", 1)[1].split("\n", 1)[0]
    assert len(description_part) <= 50
    engine.dispose()
