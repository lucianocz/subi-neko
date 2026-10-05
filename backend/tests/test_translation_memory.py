from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.db.models import File, Project, SubtitleEvent, TranslationMemoryEntry
from app.subs import translation_memory as tm


@pytest.fixture
def session_factory():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine)
    engine.dispose()


def _now() -> str:
    return datetime.utcnow().isoformat()


def _make_project_file(session) -> tuple[int, int]:
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
    session.add(file)
    session.flush()
    return project.id, file.id


def _add_event(session, file_id: int, line_index: int, source: str,
               translated: str | None, *, content_type: str = "dialogue",
               user_edited: bool = False, approved: bool = False) -> SubtitleEvent:
    e = SubtitleEvent(
        file_id=file_id, line_index=line_index, event_type="dialogue",
        content_type=content_type, layer=0, start_ms=0, end_ms=2000, original_start_ms=0, original_end_ms=2000,
        style="Default", source_text=source, translated_text=translated,
        translation_status="translated",
        is_user_edited=1 if user_edited else 0,
        is_approved=1 if approved else 0,
        created_at=_now(), updated_at=_now(),
    )
    session.add(e)
    session.flush()
    return e


def test_normalize_strips_markup_case_and_whitespace():
    assert tm.normalize_source(r"{\an8}Hello   WORLD\N!") == tm.normalize_source("hello world !")


def test_populate_and_lookup_roundtrip(session_factory):
    with session_factory() as session:
        project_id, file_id = _make_project_file(session)
        _add_event(session, file_id, 0, "Never give up!", "Nikdy se nevzdávej!")
        written = tm.populate_from_file(session, project_id, file_id)
        session.commit()
        assert written == 1

        matches = tm.lookup(session, project_id, {5: "Never give up!"})
        assert 5 in matches
        assert matches[5].target_text == "Nikdy se nevzdávej!"
        assert matches[5].origin == "ai"
        assert matches[5].exact_raw is True


def test_normalized_match_is_not_exact_raw(session_factory):
    with session_factory() as session:
        project_id, file_id = _make_project_file(session)
        _add_event(session, file_id, 0, r"{\i1}Never give up!{\i0}", "Nikdy se nevzdávej!")
        tm.populate_from_file(session, project_id, file_id)
        session.commit()

        # Same normalized text, different raw markup → suggestion only.
        matches = tm.lookup(session, project_id, {0: "Never give up!"})
        assert matches[0].exact_raw is False


def test_human_entry_never_downgraded_by_ai(session_factory):
    with session_factory() as session:
        project_id, file_id = _make_project_file(session)
        event = _add_event(session, file_id, 0, "See you.", "Zatím ahoj.", user_edited=True)
        tm.populate_from_file(session, project_id, file_id)
        session.commit()

        # A later AI re-run of the same line must not replace the human entry.
        event.translated_text = "Nashle."
        event.is_user_edited = 0
        tm.populate_from_file(session, project_id, file_id)
        session.commit()

        row = session.scalar(select(TranslationMemoryEntry))
        assert row.origin == "human"
        assert row.target_text == "Zatím ahoj."


def test_human_entry_updates_existing_ai_entry(session_factory):
    with session_factory() as session:
        project_id, file_id = _make_project_file(session)
        event = _add_event(session, file_id, 0, "See you.", "Nashle.")
        tm.populate_from_file(session, project_id, file_id)
        session.commit()

        event.translated_text = "Zatím ahoj."
        event.is_user_edited = 1
        tm.populate_from_file(session, project_id, file_id)
        session.commit()

        row = session.scalar(select(TranslationMemoryEntry))
        assert row.origin == "human"
        assert row.target_text == "Zatím ahoj."


def test_untranslated_and_empty_lines_skipped(session_factory):
    with session_factory() as session:
        project_id, file_id = _make_project_file(session)
        _add_event(session, file_id, 0, "Hello", None)
        _add_event(session, file_id, 1, "Hello again", "   ")
        _add_event(session, file_id, 2, r"{\pos(1,1)}", "text")  # empty after normalization
        written = tm.populate_from_file(session, project_id, file_id)
        assert written == 0


def test_fuzzy_suggest_finds_near_match(session_factory):
    with session_factory() as session:
        project_id, file_id = _make_project_file(session)
        _add_event(session, file_id, 0, "I will never give up on you!", "Nikdy se tě nevzdám!")
        tm.populate_from_file(session, project_id, file_id)
        session.commit()

        # Trailing punctuation changed — close enough for a suggestion.
        matches = tm.fuzzy_suggest(session, project_id, {7: "I will never give up on you..."})
        assert 7 in matches
        assert matches[7].target_text == "Nikdy se tě nevzdám!"
        assert matches[7].exact_raw is False  # suggestion only, never auto-applied


def test_fuzzy_suggest_reports_score_and_its_own_source(session_factory):
    with session_factory() as session:
        project_id, file_id = _make_project_file(session)
        _add_event(session, file_id, 0, "I will never give up on you!", "Nikdy se tě nevzdám!")
        tm.populate_from_file(session, project_id, file_id)
        session.commit()

        match = tm.fuzzy_suggest(session, project_id, {7: "I will never give up on you..."})[7]
        # The hint is rendered with both, so the model can see what it is
        # being offered and how far it is from the line it must translate.
        assert match.score < 100.0
        assert match.tm_source_text == "I will never give up on you!"


def test_lookup_reports_full_score_and_source(session_factory):
    with session_factory() as session:
        project_id, file_id = _make_project_file(session)
        _add_event(session, file_id, 0, "Good morning!", "Dobré ráno!")
        tm.populate_from_file(session, project_id, file_id)
        session.commit()

        match = tm.lookup(session, project_id, {3: "Good morning!"})[3]
        assert match.score == 100.0
        assert match.tm_source_text == "Good morning!"


def test_fuzzy_suggest_rejects_negation_flip(session_factory):
    """A negation present on one side only inverts the line, and at the 92 %
    cutoff that can be the ONLY difference — the match must be dropped."""
    from rapidfuzz import fuzz

    stored = "I can do it on my own, you know."
    queried = "I can't do it on my own, you know."

    # Guard the guard: without the negation check this pair scores high
    # enough to be offered, so the assertion below really tests the filter.
    assert fuzz.ratio(tm.normalize_source(stored), tm.normalize_source(queried)) >= 92.0

    with session_factory() as session:
        project_id, file_id = _make_project_file(session)
        _add_event(session, file_id, 0, stored, "Zvládnu to sám, víš.")
        tm.populate_from_file(session, project_id, file_id)
        session.commit()

        assert tm.fuzzy_suggest(session, project_id, {5: queried}) == {}


def test_fuzzy_suggest_rejects_changed_numbers(session_factory):
    with session_factory() as session:
        project_id, file_id = _make_project_file(session)
        _add_event(session, file_id, 0, "We have to hold out for 12 more days.", "Musíme vydržet ještě 12 dní.")
        tm.populate_from_file(session, project_id, file_id)
        session.commit()

        assert tm.fuzzy_suggest(
            session, project_id, {5: "We have to hold out for 13 more days."}) == {}


def test_fuzzy_suggest_ignores_dissimilar_and_short_lines(session_factory):
    with session_factory() as session:
        project_id, file_id = _make_project_file(session)
        _add_event(session, file_id, 0, "I will never give up on you!", "Nikdy se tě nevzdám!")
        tm.populate_from_file(session, project_id, file_id)
        session.commit()

        assert tm.fuzzy_suggest(session, project_id, {0: "Completely different sentence here."}) == {}
        assert tm.fuzzy_suggest(session, project_id, {1: "Hi!"}) == {}  # too short


def test_record_uses_increments(session_factory):
    with session_factory() as session:
        project_id, file_id = _make_project_file(session)
        _add_event(session, file_id, 0, "Line", "Řádek")
        tm.populate_from_file(session, project_id, file_id)
        session.commit()

        matches = tm.lookup(session, project_id, {0: "Line"})
        tm.record_uses(session, [matches[0].entry_id])
        session.commit()

        row = session.scalar(select(TranslationMemoryEntry))
        assert row.use_count == 1


# ---------------------------------------------------------------------------
# Post-acceptance editor auto-sync (upsert / downgrade single entries)
# ---------------------------------------------------------------------------

def test_upsert_event_entry_creates_human_entry(session_factory):
    with session_factory() as session:
        project_id, file_id = _make_project_file(session)
        event = _add_event(session, file_id, 0, "See you.", "Zatím ahoj.", user_edited=True)

        changed = tm.upsert_event_entry(session, project_id, event, origin="human")
        session.commit()

        assert changed is True
        row = session.scalar(select(TranslationMemoryEntry))
        assert row.origin == "human"
        assert row.target_text == "Zatím ahoj."
        assert row.src_file_id == file_id
        assert row.src_line_index == 0


def test_upsert_event_entry_human_overwrites_existing(session_factory):
    """A post-acceptance human correction is the freshest truth — it replaces
    whatever the TM held for that source line, even a prior human entry."""
    with session_factory() as session:
        project_id, file_id = _make_project_file(session)
        event = _add_event(session, file_id, 0, "See you.", "Nashle.")
        tm.populate_from_file(session, project_id, file_id)
        session.commit()

        event.translated_text = "Zatím ahoj."
        event.is_user_edited = 1
        tm.upsert_event_entry(session, project_id, event, origin="human")
        session.commit()

        row = session.scalar(select(TranslationMemoryEntry))
        assert row.origin == "human"
        assert row.target_text == "Zatím ahoj."


def test_upsert_event_entry_skips_non_dialogue_and_empty(session_factory):
    with session_factory() as session:
        project_id, file_id = _make_project_file(session)
        event = _add_event(session, file_id, 0, "See you.", "Zatím ahoj.")
        event.event_type = "comment"
        assert tm.upsert_event_entry(session, project_id, event, origin="human") is False

        empty = _add_event(session, file_id, 1, r"{\pos(1,1)}", "text")
        assert tm.upsert_event_entry(session, project_id, empty, origin="human") is False


def test_downgrade_event_entry_demotes_owned_human_entry(session_factory):
    """Reverting an event to the AI text demotes the human TM entry that came
    from this exact line."""
    with session_factory() as session:
        project_id, file_id = _make_project_file(session)
        event = _add_event(session, file_id, 0, "See you.", "Zatím ahoj.", user_edited=True)
        tm.populate_from_file(session, project_id, file_id)
        session.commit()

        event.translated_text = "Nashle."  # reverted to the AI text
        event.is_user_edited = 0
        changed = tm.downgrade_event_entry(session, project_id, event)
        session.commit()

        assert changed is True
        row = session.scalar(select(TranslationMemoryEntry))
        assert row.origin == "ai"
        assert row.target_text == "Nashle."


def test_downgrade_event_entry_leaves_foreign_human_entry(session_factory):
    """A human entry owned by a DIFFERENT line must survive another event's
    revert — we don't own it."""
    with session_factory() as session:
        project_id, file_id = _make_project_file(session)
        owner = _add_event(session, file_id, 0, "See you.", "Zatím ahoj.", user_edited=True)
        tm.populate_from_file(session, project_id, file_id)
        session.commit()

        other = _add_event(session, file_id, 5, "See you.", "Nashle.")
        changed = tm.downgrade_event_entry(session, project_id, other)

        assert changed is False
        row = session.scalar(select(TranslationMemoryEntry))
        assert row.origin == "human"
        assert row.target_text == "Zatím ahoj."
