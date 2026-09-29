from __future__ import annotations

import json
from pathlib import Path

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
from app.jobs.handlers import review_chunk_final as review_module


@pytest.fixture
def session_factory():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine)
    engine.dispose()


def _ctx() -> JobContext:
    return JobContext(
        Path("."), Path("."),
        AppOptions(cps_limit=100.0, max_row_chars=100, auto_line_break=False),
    )


def _progress(_fraction: float, _message: str) -> None:
    pass


def _seed(session_factory, *, polish_attempts: int) -> tuple[int, dict[int, int]]:
    with session_factory() as session:
        project = Project(
            name="P", source_directory="p", anime_provider="anidb", anime_external_id="1"
        )
        session.add(project)
        session.flush()
        luxion = ProjectCharacter(project_id=project.id, name="Luxion")
        leon = ProjectCharacter(project_id=project.id, name="Leon Fou Bartfort", gender="male")
        session.add_all([luxion, leon])
        session.flush()
        session.add_all([
            ProjectSpeaker(project_id=project.id, name="LUXION", character_id=luxion.id),
            ProjectSpeaker(project_id=project.id, name="LUXIN", character_id=luxion.id),
            ProjectSpeaker(project_id=project.id, name="LEON", character_id=leon.id),
            ProjectAddressPair(
                project_id=project.id, speaker_name="Luxion",
                addressee_name="Leon Fou Bartfort", mode="vykani",
                origin="manual", locked=1,
            ),
            ProjectAddressPair(
                project_id=project.id, speaker_name="Leon Fou Bartfort",
                addressee_name="Luxion", mode="tykani",
                origin="manual", locked=1,
            ),
            ProjectGlossaryTerm(
                project_id=project.id, source_term="Leon Fou Bartfort", target_term="Leon",
                category="name", gender="male", vocative="Leone", origin="manual",
                locked=1, is_active=1,
            ),
            ProjectGlossaryTerm(
                project_id=project.id, source_term="Luxion", target_term="Luxion",
                category="name", vocative="Luxione", origin="manual",
                locked=1, is_active=1,
            ),
        ])
        file = File(project_id=project.id, filename="e01.mkv", relative_path="e01.mkv")
        session.add(file)
        session.flush()
        session.add(SubtitleChunk(
            file_id=file.id, chunk_index=0, translate_from_line=1, translate_to_line=9,
            content_type="dialogue", status="polished",
            polish_attempt_count=polish_attempts,
        ))
        rows = [
            (1, "LEON", "Luxione, můžeš začít."),
            (2, "LUXION", "Leone, nikoho nepozveš?"),
            # Same canonical speaker under another raw ASS alias: explicit
            # addressee from line 2 remains established for this turn.
            (3, "LUXIN", "Jen abys je naštval."),
            (4, "LUXION", "Už byste si měl vyjasnit, co k nim cítíte."),
            (5, "LUXION", "Svatá, ty dvě mi nejsou po chuti."),
            (6, "LUXION", "Vás dva tu nepotřebuju."),
            (7, "LUXION", "Pane Leone, můžete odejít."),
            # Speaker change ends the established Luxion→Leon turn.
            (8, "LEON", "Luxione, můžeš jít."),
            (9, "LUXION", "Nikoho nepozveš?"),
        ]
        event_ids: dict[int, int] = {}
        for line, speaker, translated in rows:
            event = SubtitleEvent(
                file_id=file.id, line_index=line, event_type="dialogue",
                content_type="dialogue", layer=0, start_ms=line * 5000,
                end_ms=line * 5000 + 5000, style="Default", name=speaker,
                source_text=f"Source line {line}", translated_text=translated,
                translation_status="validated",
            )
            session.add(event)
            session.flush()
            event_ids[line] = event.id
        session.add(QaItem(
            file_id=file.id, subtitle_event_id=event_ids[1], severity="warning",
            qa_type="polish_meaning", message="unrelated finding", is_resolved=0,
        ))
        session.commit()
        return file.id, event_ids


@pytest.mark.parametrize(
    ("polish_attempts", "expected_status", "expected_needs_polish"),
    [(0, "needs_polish", True), (2, "final_reviewed", False)],
)
def test_final_review_tv_is_addressee_aware_and_preserves_pipeline_semantics(
    session_factory, monkeypatch, polish_attempts, expected_status, expected_needs_polish,
):
    file_id, event_ids = _seed(session_factory, polish_attempts=polish_attempts)
    monkeypatch.setattr(review_module, "SyncSessionLocal", session_factory)

    result = review_module.review_chunk_final(
        {"file_id": file_id, "chunk_index": 0}, _ctx(), _progress)

    assert result["status"] == "succeeded"
    assert result["result"]["needs_polish"] is expected_needs_polish
    with session_factory() as session:
        chunk = session.scalar(select(SubtitleChunk).where(SubtitleChunk.file_id == file_id))
        assert chunk.status == expected_status
        items = list(session.scalars(select(QaItem).where(QaItem.file_id == file_id)))
        assert any(item.qa_type == "polish_meaning" and item.message == "unrelated finding"
                   for item in items)

        mismatches = [item for item in items if item.qa_type == "tv_address_mismatch"]
        assert {item.subtitle_event_id for item in mismatches} == {
            event_ids[2], event_ids[3],
        }
        for item in mismatches:
            details = json.loads(item.details_json)
            assert details["speaker"] == "Luxion"
            assert details["addressee"] == "Leon Fou Bartfort"
            assert details["expected"] == "vykani"

        assert not any(item.qa_type == "tv_address_mixed" for item in items)
        # Matching formal singular; demonstrative ty; explicit plural;
        # ambiguous formal/plural; matching reverse pair; and a line whose
        # addressee was reset are all intentionally quiet.
        quiet_lines = {4, 5, 6, 7, 8, 9}
        assert not any(
            item.qa_type.startswith("tv_address_")
            and item.subtitle_event_id in {event_ids[line] for line in quiet_lines}
            for item in items
        )
