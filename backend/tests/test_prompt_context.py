from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.db.models import File, Project, SubtitleEvent
from app.jobs.handlers.prompt_context import (
    AnalysisContext,
    StyleContext,
    build_glossary_block,
    build_lookahead_lines,
    build_scene_block,
    build_style_block,
    build_tricky_notes_block,
    char_budget,
    load_following_context_events,
    load_preceding_context_events,
)


def _term(source, target, category="other", vocative=None, gender=None, note=None):
    return SimpleNamespace(source_term=source, target_term=target, category=category,
                           vocative=vocative, gender=gender, note=note)


# ---------------------------------------------------------------------------
# Glossary block
# ---------------------------------------------------------------------------

def test_glossary_names_always_included():
    terms = [_term("Aria", "Aria", category="name", vocative="Ario", gender="female")]
    block = build_glossary_block(terms, ["Nothing relevant here."])
    assert '"Aria"' in block
    assert "vocative: Ario" in block
    assert "keep as-is" in block


def test_glossary_other_categories_filtered_by_chunk_text():
    terms = [
        _term("Dragon Slash", "Dračí sek", category="technique"),
        _term("Moon Blade", "Měsíční čepel", category="technique"),
    ]
    block = build_glossary_block(terms, ["He used Dragon Slash again!"])
    assert "Dračí sek" in block
    assert "Měsíční čepel" not in block


def test_glossary_matches_through_ass_markup():
    terms = [_term("Dragon Slash", "Dračí sek", category="technique")]
    block = build_glossary_block(terms, [r"He used {\i1}Dragon Slash{\i0}!"])
    assert "Dračí sek" in block


def test_glossary_cap_never_evicts_names_for_matched_terms():
    """Regression: terms arrive ordered by category, and 'honorific'/'item'
    sort before 'name'. A flat slice of the merged list dropped character
    names — the one category that must never drift — as soon as a project's
    glossary outgrew the cap."""
    chunk_text = " ".join(f"item{i}" for i in range(120))
    terms = (
        [_term(f"item{i}", f"věc{i}", category="item") for i in range(120)]
        + [_term("Aria", "Aria", category="name", vocative="Ario")]
    )

    block = build_glossary_block(terms, [chunk_text])

    assert "vocative: Ario" in block
    assert len(block.splitlines()) <= 80


# ---------------------------------------------------------------------------
# Style block
# ---------------------------------------------------------------------------

def test_style_block_filters_voices_and_pairs_to_present_speakers():
    style = StyleContext(
        tone_summary="Dark fantasy.",
        voices={"Aria": ("blunt", "informal"), "Bob": ("flowery", "formal")},
        pairs=[("aria", "bob", "vykani"), ("carl", "bob", "tykani")],
    )
    identities = {"aria": ("Aria", "female"), "carl": ("Carl", "male")}

    block = build_style_block(style, {"aria"}, identities)

    assert "Dark fantasy." in block
    assert "Aria: blunt" in block
    assert "Bob: flowery" not in block            # Bob doesn't speak in this chunk
    assert "aria addresses bob: vykani" in block
    assert "carl addresses bob" not in block


def test_style_block_matches_canonical_pair_to_raw_speaker_identity():
    style = StyleContext(pairs=[("Luxion", "Leon Fou Bartfort", "vykani")])
    identities = {"LUXION": ("Luxion", None)}

    block = build_style_block(style, {"LUXION"}, identities)

    assert "Luxion addresses Leon Fou Bartfort: vykani" in block


# ---------------------------------------------------------------------------
# Scene / tricky notes
# ---------------------------------------------------------------------------

def test_scene_block_selects_overlapping_scenes():
    analysis = AnalysisContext(
        synopsis="Episode about a heist.",
        scenes=[
            {"from_line": 0, "to_line": 50, "summary": "Planning", "setting": "hideout"},
            {"from_line": 51, "to_line": 100, "summary": "The heist", "setting": "bank"},
        ],
    )
    block = build_scene_block(analysis, 60, 90)
    assert "Episode about a heist." in block
    assert "The heist" in block
    assert "Planning" not in block


def test_tricky_notes_only_for_given_lines():
    analysis = AnalysisContext(tricky_lines={5: "pun on 'bat'", 99: "sarcasm"})
    block = build_tricky_notes_block(analysis, [5, 6, 7])
    assert "pun on 'bat'" in block
    assert "sarcasm" not in block


# ---------------------------------------------------------------------------
# Preceding-context window (partition-scoped)
# ---------------------------------------------------------------------------

@pytest.fixture
def context_session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()
    engine.dispose()


def _context_file(session) -> int:
    now = datetime.utcnow().isoformat()
    project = Project(
        name="P", source_directory="p", anime_provider="test", anime_external_id="1",
        created_at=now, updated_at=now,
    )
    session.add(project)
    session.flush()
    file = File(
        project_id=project.id, filename="e1.mkv", relative_path="e1.mkv",
        created_at=now, updated_at=now,
    )
    session.add(file)
    session.flush()
    return file.id


def _event(session, file_id: int, line_index: int, content_type: str,
           event_type: str = "dialogue") -> None:
    now = datetime.utcnow().isoformat()
    session.add(SubtitleEvent(
        file_id=file_id, line_index=line_index, event_type=event_type,
        content_type=content_type, layer=0, start_ms=0, end_ms=2000,
        style="Default", source_text=f"line {line_index}",
        translated_text=f"řádek {line_index}", translation_status="translated",
        created_at=now, updated_at=now,
    ))
    session.flush()


def test_context_window_excludes_other_partitions(context_session):
    file_id = _context_file(context_session)
    for line_index, content_type in [
        (0, "dialogue"), (1, "sign"), (2, "dialogue"), (3, "song"), (4, "dialogue"),
    ]:
        _event(context_session, file_id, line_index, content_type)

    rows = load_preceding_context_events(
        context_session, file_id, "dialogue", before_line=5, limit=10)

    assert [r.line_index for r in rows] == [0, 2, 4]


def test_context_window_limit_counts_only_own_partition(context_session):
    """Regression: a cross-partition window spends its slots on interleaved
    signs, leaving fewer real dialogue lines than the configured size."""
    file_id = _context_file(context_session)
    for line_index in range(12):
        _event(context_session, file_id, line_index,
               "sign" if line_index % 2 else "dialogue")

    rows = load_preceding_context_events(
        context_session, file_id, "dialogue", before_line=12, limit=3)

    # The three nearest DIALOGUE lines, not the three nearest lines overall
    # (which would be 9/10/11 — only one of them dialogue).
    assert [r.line_index for r in rows] == [6, 8, 10]


def test_context_window_is_chronological(context_session):
    file_id = _context_file(context_session)
    for line_index in range(5):
        _event(context_session, file_id, line_index, "dialogue")

    rows = load_preceding_context_events(
        context_session, file_id, "dialogue", before_line=5, limit=3)

    assert [r.line_index for r in rows] == [2, 3, 4]


def test_context_window_scopes_to_sign_partition(context_session):
    file_id = _context_file(context_session)
    for line_index, content_type in [
        (0, "dialogue"), (1, "sign"), (2, "dialogue"), (3, "sign"),
    ]:
        _event(context_session, file_id, line_index, content_type)

    rows = load_preceding_context_events(
        context_session, file_id, "sign", before_line=4, limit=10)

    assert [r.line_index for r in rows] == [1, 3]


def test_context_window_skips_comment_events(context_session):
    file_id = _context_file(context_session)
    _event(context_session, file_id, 0, "dialogue")
    _event(context_session, file_id, 1, "dialogue", event_type="comment")

    rows = load_preceding_context_events(
        context_session, file_id, "dialogue", before_line=2, limit=10)

    assert [r.line_index for r in rows] == [0]


def test_context_window_disabled_by_zero_limit(context_session):
    file_id = _context_file(context_session)
    _event(context_session, file_id, 0, "dialogue")

    assert load_preceding_context_events(
        context_session, file_id, "dialogue", before_line=1, limit=0) == []


# ---------------------------------------------------------------------------
# Lookahead window
# ---------------------------------------------------------------------------

def test_lookahead_window_scopes_to_own_partition(context_session):
    file_id = _context_file(context_session)
    for line_index, content_type in [
        (0, "dialogue"), (1, "dialogue"), (2, "sign"), (3, "dialogue"),
        (4, "song"), (5, "dialogue"),
    ]:
        _event(context_session, file_id, line_index, content_type)

    rows = load_following_context_events(
        context_session, file_id, "dialogue", after_line=1, limit=10)

    assert [r.line_index for r in rows] == [3, 5]


def test_lookahead_window_is_chronological_and_limited(context_session):
    file_id = _context_file(context_session)
    for line_index in range(10):
        _event(context_session, file_id, line_index, "dialogue")

    rows = load_following_context_events(
        context_session, file_id, "dialogue", after_line=4, limit=3)

    # The three lines immediately after the chunk, in reading order.
    assert [r.line_index for r in rows] == [5, 6, 7]


def test_lookahead_window_skips_comment_events(context_session):
    file_id = _context_file(context_session)
    _event(context_session, file_id, 0, "dialogue")
    _event(context_session, file_id, 1, "dialogue", event_type="comment")
    _event(context_session, file_id, 2, "dialogue")

    rows = load_following_context_events(
        context_session, file_id, "dialogue", after_line=0, limit=10)

    assert [r.line_index for r in rows] == [2]


def test_lookahead_window_disabled_by_zero_limit(context_session):
    file_id = _context_file(context_session)
    _event(context_session, file_id, 1, "dialogue")

    assert load_following_context_events(
        context_session, file_id, "dialogue", after_line=0, limit=0) == []


def test_lookahead_lines_are_source_only_and_drop_empty_rows(context_session):
    file_id = _context_file(context_session)
    _event(context_session, file_id, 1, "dialogue")
    rows = load_following_context_events(
        context_session, file_id, "dialogue", after_line=0, limit=10)

    lines = build_lookahead_lines(rows)

    assert lines == ["[AHEAD] 1: line 1"]
    # The translation must never leak into the lookahead: these lines exist
    # to show what is coming, and are usually untranslated anyway.
    assert "řádek" not in lines[0]
