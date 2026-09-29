from __future__ import annotations

from app.jobs.handlers.polish_chunk import select_targeted_support_events


def _row(
    line: int,
    source: str,
    *,
    name: str = "ALICE",
    translated: str | None = "Překlad",
    edited: int = 0,
    locked: int = 0,
) -> dict:
    return {
        "line_index": line,
        "source_text": source,
        "translated_text": translated,
        "name": name,
        "is_user_edited": edited,
        "is_locked": locked,
    }


def _select(rows: list[dict], primary: set[int], identities=None):
    return select_targeted_support_events(rows, primary, identities or {})


def test_middle_of_three_event_sentence_includes_both_sides():
    support, associations = _select([
        _row(120, "If you really believe"),
        _row(121, "that this is the answer,"),
        _row(122, "then tell me why."),
    ], {121})
    assert support == {120, 122}
    assert associations == {120: {121}, 122: {121}}


def test_sentence_beginning_can_include_forward_support():
    support, _ = _select([
        _row(1, "Although I warned you,"),
        _row(2, "you went there anyway."),
    ], {1})
    assert support == {2}


def test_sentence_end_can_include_backward_support():
    support, _ = _select([
        _row(1, "Because the gate was locked,"),
        _row(2, "we had to turn back."),
    ], {2})
    assert support == {1}


def test_complete_neighbouring_sentence_is_not_included():
    support, _ = _select([
        _row(1, "I understand."),
        _row(2, "But if you insist,"),
    ], {2})
    assert support == set()


def test_speaker_change_terminates_window():
    support, _ = _select([
        _row(1, "If that is what you think", name="ALICE"),
        _row(2, "then prove it.", name="BOB"),
    ], {1})
    assert support == set()


def test_raw_aliases_for_same_canonical_speaker_continue():
    identities = {
        "LUXION": ("Luxion", None),
        "LUXIN": ("Luxion", None),
    }
    support, _ = _select([
        _row(1, "If you are certain", name="LUXION"),
        _row(2, "we can begin.", name="LUXIN"),
    ], {1}, identities)
    assert support == {2}


def test_protected_neighbour_is_not_support():
    support, _ = _select([
        _row(1, "If you are certain"),
        _row(2, "we can begin.", edited=1),
    ], {1})
    assert support == set()


def test_event_not_passed_from_chunk_cannot_be_selected():
    support, _ = _select([_row(100, "If this is true")], {100})
    assert support == set()


def test_overlapping_primary_windows_are_deduplicated():
    support, associations = _select([
        _row(1, "When the bell rings,"),
        _row(2, "and the door opens,"),
        _row(3, "walk in slowly,"),
        _row(4, "then wait."),
    ], {2, 3})
    assert support == {1, 4}
    assert associations == {1: {2}, 4: {3}}


def test_no_primary_targets_means_no_support():
    assert _select([_row(1, "If it rains")], set()) == (set(), {})


def test_short_independent_utterance_gets_no_forward_support():
    support, _ = _select([
        _row(1, "Yes."),
        _row(2, "I will go now."),
    ], {1})
    assert support == set()
