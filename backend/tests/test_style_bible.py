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
from app.llm.schemas import AddressPairOut, StyleBibleUpdateResponse


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
