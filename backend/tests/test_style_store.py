from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.db.models import (
    Project, ProjectAddressPair, ProjectCharacter, ProjectGlossaryTerm, ProjectSpeaker,
)
from app.jobs.handlers.style_store import (
    canonical_address_pairs, insert_new_glossary_terms, upsert_address_pairs,
)
from app.llm.schemas import AddressPairOut, GlossaryTermOut


def _now() -> str:
    return datetime.utcnow().isoformat()


@pytest.fixture
def session_factory():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine)
    engine.dispose()


def _make_project(session) -> int:
    project = Project(
        name="P", source_directory="p", anime_provider="anidb", anime_external_id="1",
        created_at=_now(), updated_at=_now(),
    )
    session.add(project)
    session.flush()
    return project.id


def _character(session, project_id: int, name: str) -> ProjectCharacter:
    row = ProjectCharacter(project_id=project_id, name=name, created_at=_now(), updated_at=_now())
    session.add(row)
    session.flush()
    return row


def _speaker(session, project_id: int, name: str,
             character: ProjectCharacter | None = None) -> ProjectSpeaker:
    row = ProjectSpeaker(
        project_id=project_id, name=name,
        character_id=character.id if character else None,
        created_at=_now(), updated_at=_now(),
    )
    session.add(row)
    session.flush()
    return row


def _pair(speaker: str, addressee: str, mode: str) -> AddressPairOut:
    return AddressPairOut(speaker=speaker, addressee=addressee, mode=mode)


def test_glossary_dedupe_is_source_term_only(session_factory):
    """A term seeded under one category must not be re-inserted by the LLM
    under a different one ('Aria' name + 'Aria' other duplicates)."""
    with session_factory() as session:
        project_id = _make_project(session)

        insert_new_glossary_terms(session, project_id, [
            GlossaryTermOut(source="Aria", target="Aria", category="name", gender="female"),
        ], "metadata", _now())
        session.commit()

        inserted = insert_new_glossary_terms(session, project_id, [
            GlossaryTermOut(source="Aria", target="Aria", category="other"),
            GlossaryTermOut(source="aria", target="Aria", category="catchphrase"),
            GlossaryTermOut(source="Dragon Slash", target="Dračí sek", category="technique"),
        ], "llm", _now())
        session.commit()

        assert inserted == 1  # only Dragon Slash is new
        rows = list(session.scalars(select(ProjectGlossaryTerm)))
        assert sorted(r.source_term for r in rows) == ["Aria", "Dragon Slash"]
        aria = next(r for r in rows if r.source_term == "Aria")
        assert aria.category == "name"  # the seeded row survived untouched


def test_llm_fills_missing_fields_on_metadata_seeded_terms(session_factory):
    """Metadata-seeded names start without vocative/note; a later LLM
    suggestion for the same source term fills exactly the empty fields."""
    with session_factory() as session:
        project_id = _make_project(session)

        insert_new_glossary_terms(session, project_id, [
            GlossaryTermOut(source="Aria", target="Aria", category="name", gender="female"),
        ], "metadata", _now())
        session.commit()

        inserted = insert_new_glossary_terms(session, project_id, [
            GlossaryTermOut(source="Aria", target="Árie", category="name",
                            gender="male", vocative="Ario", note="Hlavní hrdinka"),
        ], "llm", _now())
        session.commit()

        assert inserted == 0
        aria = session.scalar(
            select(ProjectGlossaryTerm).where(ProjectGlossaryTerm.source_term == "Aria"))
        assert aria.target_term == "Aria"       # existing rendering never replaced
        assert aria.origin == "metadata"        # origin preserved
        assert aria.gender == "female"          # non-empty field never overwritten
        assert aria.vocative == "Ario"          # empty field filled
        assert aria.note == "Hlavní hrdinka"    # empty field filled


def test_llm_never_touches_locked_terms(session_factory):
    with session_factory() as session:
        project_id = _make_project(session)

        insert_new_glossary_terms(session, project_id, [
            GlossaryTermOut(source="Aria", target="Aria", category="name"),
        ], "manual", _now())
        term = session.scalar(select(ProjectGlossaryTerm))
        term.locked = 1
        session.commit()

        insert_new_glossary_terms(session, project_id, [
            GlossaryTermOut(source="Aria", target="Árie", category="name",
                            vocative="Ario", note="poznámka"),
        ], "llm", _now())
        session.commit()

        aria = session.scalar(select(ProjectGlossaryTerm))
        assert aria.vocative is None
        assert aria.note is None


def test_incremental_pair_resolves_raw_speakers_and_preserves_established_mode(session_factory):
    with session_factory() as session:
        project_id = _make_project(session)
        luxion = _character(session, project_id, "Luxion")
        leon = _character(session, project_id, "Leon Fou Bartfort")
        _speaker(session, project_id, "LUXION", luxion)
        _speaker(session, project_id, "LEON", leon)
        session.add(ProjectAddressPair(
            project_id=project_id, speaker_name="Luxion",
            addressee_name="Leon Fou Bartfort", mode="vykani", origin="llm", locked=0,
            created_at=_now(), updated_at=_now(),
        ))
        session.commit()

        written = upsert_address_pairs(
            session, project_id, [_pair("luxion", "leon", "tykani")], "llm", _now(),
            update_existing_mode=False,
        )
        session.commit()

        rows = list(session.scalars(select(ProjectAddressPair)))
        assert written == 0
        assert len(rows) == 1
        assert rows[0].mode == "vykani"


def test_new_mapped_pair_is_stored_under_canonical_names_and_direction_is_preserved(session_factory):
    with session_factory() as session:
        project_id = _make_project(session)
        luxion = _character(session, project_id, "Luxion")
        leon = _character(session, project_id, "Leon Fou Bartfort")
        _speaker(session, project_id, "LUXION", luxion)
        _speaker(session, project_id, "LEON", leon)

        upsert_address_pairs(session, project_id, [
            _pair("LUXION", "Leon Fou Bartfort", "vykani"),
            _pair("LEON", "LUXION", "tykani"),
        ], "llm", _now(), update_existing_mode=False)
        session.commit()

        rows = list(session.scalars(select(ProjectAddressPair).order_by(ProjectAddressPair.id)))
        assert [(r.speaker_name, r.addressee_name, r.mode) for r in rows] == [
            ("Luxion", "Leon Fou Bartfort", "vykani"),
            ("Leon Fou Bartfort", "Luxion", "tykani"),
        ]


def test_unmapped_speaker_fallback_is_case_insensitive_without_inventing_match(session_factory):
    with session_factory() as session:
        project_id = _make_project(session)
        _speaker(session, project_id, "MYSTERY")

        assert upsert_address_pairs(
            session, project_id, [_pair("MYSTERY", "Crowd", "tykani")],
            "llm", _now(), update_existing_mode=False) == 1
        assert upsert_address_pairs(
            session, project_id, [_pair("mystery", "crowd", "vykani")],
            "llm", _now(), update_existing_mode=False) == 0
        session.commit()

        row = session.scalar(select(ProjectAddressPair))
        assert (row.speaker_name, row.addressee_name, row.mode) == ("MYSTERY", "Crowd", "tykani")


def test_initial_generation_mode_can_still_refine_unlocked_pair(session_factory):
    with session_factory() as session:
        project_id = _make_project(session)
        upsert_address_pairs(
            session, project_id, [_pair("Alice", "Bob", "mixed")], "llm", _now())
        session.commit()

        written = upsert_address_pairs(
            session, project_id, [_pair("alice", "bob", "vykani")], "llm", _now())
        session.commit()

        row = session.scalar(select(ProjectAddressPair))
        assert written == 1
        assert row.mode == "vykani"


def test_locked_alias_pair_wins_existing_conflict_for_prompt_context(session_factory):
    with session_factory() as session:
        project_id = _make_project(session)
        luxion = _character(session, project_id, "Luxion")
        leon = _character(session, project_id, "Leon Fou Bartfort")
        _speaker(session, project_id, "LUXION", luxion)
        _speaker(session, project_id, "LEON", leon)
        session.add_all([
            ProjectAddressPair(
                project_id=project_id, speaker_name="Luxion", addressee_name="Leon Fou Bartfort",
                mode="vykani", origin="manual", locked=1, created_at=_now(), updated_at=_now()),
            ProjectAddressPair(
                project_id=project_id, speaker_name="LUXION", addressee_name="LEON",
                mode="tykani", origin="llm", locked=0, created_at=_now(), updated_at=_now()),
        ])
        session.commit()

        assert canonical_address_pairs(session, project_id) == [
            ("Luxion", "Leon Fou Bartfort", "vykani")
        ]

        written = upsert_address_pairs(
            session, project_id, [_pair("LUXION", "LEON", "tykani")],
            "llm", _now(), update_existing_mode=True)
        assert written == 0
