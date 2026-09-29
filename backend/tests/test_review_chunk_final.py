"""Unit tests for review_chunk_final's helpers.

_modes_for is the bridge between two different namings of the same person:
address pairs are keyed by the raw speaker labels the script uses, while the
inferred addressee is a glossary name as it appears in the translation.
"""
from __future__ import annotations

from app.jobs.handlers.review_chunk_final import _modes_for, _rebalance_dialogue_text
from app.subs.line_breaking import empty_hard_break_edges

# Speaker label → addressee label, both casefolded, as stored by analyze_script.
PAIRS = {
    ("bob", "aria"): {"vykani"},
    ("bob", "tomas"): {"tykani"},
    ("aria", "bob"): {"tykani"},
}

# Canonical glossary name → every alias a pair might use.
ALIASES = {
    "Aria": {"aria", "ario"},
    "Tomáš": {"tomas", "tomáš", "tomáši"},
}


def test_exact_pair_wins_over_speaker_uniform_mode():
    # Bob vykes Aria but tyká Tomáš — a uniform-mode check could say nothing
    # about either; the pair says exactly which applies to this line.
    assert _modes_for("Bob", "Aria", PAIRS, ALIASES) == {"vykani"}
    assert _modes_for("Bob", "Tomáš", PAIRS, ALIASES) == {"tykani"}


def test_addressee_matched_through_its_aliases():
    """The translated name ("Tomáš") and the script's speaker label ("tomas")
    are different strings for the same person."""
    assert _modes_for("Bob", "Tomáš", PAIRS, ALIASES) == {"tykani"}


def test_speaker_match_is_case_insensitive():
    assert _modes_for("BOB", "Aria", PAIRS, ALIASES) == {"vykani"}


def test_none_when_no_pair_is_stored():
    assert _modes_for("Bob", "Nobody", PAIRS, ALIASES) is None
    assert _modes_for("Stranger", "Aria", PAIRS, ALIASES) is None


def test_none_without_both_ends():
    assert _modes_for(None, "Aria", PAIRS, ALIASES) is None
    assert _modes_for("Bob", None, PAIRS, ALIASES) is None
    assert _modes_for("", "", PAIRS, ALIASES) is None


def test_falls_back_to_the_bare_name_when_no_aliases_known():
    pairs = {("bob", "carol"): {"tykani"}}
    assert _modes_for("Bob", "Carol", pairs, {}) == {"tykani"}


def test_final_review_repairs_new_empty_row_and_preserves_break_count():
    translated = r"Ve hře ale byla záporačka. \N"
    fixed = _rebalance_dialogue_text(
        r"But in the game, she was a villain.\NIt was different.",
        translated,
        42,
    )

    assert fixed is not None
    assert fixed.count(r"\N") == translated.count(r"\N") == 1
    assert not empty_hard_break_edges(fixed)


def test_final_review_respects_intentional_source_edge_break():
    source = r"\NPreviously on..."
    translated = r"\NMinule jste viděli..."
    assert _rebalance_dialogue_text(source, translated, 42) is None


def test_final_review_does_not_repair_complex_typesetting():
    assert _rebalance_dialogue_text(
        r"Plain\Nsource",
        r"Text{\i0}\N",
        42,
    ) is None
