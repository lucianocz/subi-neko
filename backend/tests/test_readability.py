"""Shared CPS/readability helper — regression tests proving the extraction
from czech_checks / prompt_context preserved the original semantics."""
from __future__ import annotations

from app.jobs.handlers.prompt_context import MIN_CHAR_BUDGET, char_budget as prompt_char_budget
from app.subs.czech_checks import check_readability
from app.subs.readability import (
    char_budget,
    compute_cps,
    exceeds_cps,
    visible_len,
    visible_text,
)

BS = "\\"


def _legacy_visible(text: str) -> str:
    """The pre-extraction inline logic, verbatim."""
    import re

    from app.subs.tag_masking import plain_text

    rows = [plain_text(part) for part in re.split(r"\\N", text or "")]
    return " ".join(row for row in rows if row)


def test_visible_text_matches_legacy_for_tags_spaces_breaks_punctuation():
    samples = [
        "Ahoj, světe!",
        "{\\i1}Kurzíva{\\i0} text",
        f"První řádek{BS}Ndruhý řádek",
        f"  padded  {BS}N  rows  ",
        f"{BS}N{BS}Nonly breaks",
        f"soft{BS}nbreak{BS}hspace",
        "{\\an8}",
        "",
        None,
    ]
    for sample in samples:
        assert visible_text(sample) == _legacy_visible(sample or "")
        assert visible_len(sample) == len(_legacy_visible(sample or ""))


def test_visible_text_specifics():
    assert visible_text(f"Ahoj{BS}Nsvět") == "Ahoj svět"  # break counts as one space
    assert visible_text("{\\b1}Hi{\\b0}, you!") == "Hi, you!"
    assert visible_text(f"  a  {BS}N  b  ") == "a b"  # trimmed rows, interior spaces kept
    assert visible_len("a  b") == 4


def test_compute_cps_raw_unrounded():
    assert compute_cps("a" * 30, 0, 1000) == 30.0
    cps = compute_cps("a" * 13, 0, 600)
    assert cps == 13 / 0.6  # no rounding
    assert cps != round(cps)


def test_compute_cps_null_cases():
    assert compute_cps("Ahoj", 1000, 1000) is None
    assert compute_cps("Ahoj", 1000, 900) is None
    assert compute_cps("", 0, 1000) is None
    assert compute_cps(None, 0, 1000) is None
    assert compute_cps("{\\an8}", 0, 1000) is None
    assert compute_cps(f"{BS}N", 0, 1000) is None


def test_compute_cps_ignores_tags():
    assert compute_cps("{\\i1}abcde{\\i0}", 0, 1000) == 5.0


def test_char_budget_floor_and_none():
    assert char_budget(0, 3000, 20.0) == 60
    assert char_budget(0, 0, 20.0) is None
    assert char_budget(500, 100, 20.0) is None
    # Source floor: 100-char source * 0.9 = 90 beats the 20 cps budget of 20.
    assert char_budget(0, 1000, 20.0, "x" * 100) == 90
    # Floor never lowers a larger CPS budget.
    assert char_budget(0, 10000, 20.0, "x" * 10) == 200


def test_exceeds_cps_strict_comparisons():
    # exactly at the limit: not over
    assert not exceeds_cps("a" * 20, 0, 1000, 20.0)
    assert exceeds_cps("a" * 21, 0, 1000, 20.0)
    assert not exceeds_cps("a" * 21, 0, 0, 20.0)
    assert not exceeds_cps("", 0, 1000, 20.0)


def test_exceeds_cps_uses_source_floor_exactly():
    # 30 chars in 1s at limit 20, 30-char source -> floor 27: flagged (30 > 27).
    assert exceeds_cps("a" * 30, 0, 1000, 20.0, "b" * 30)
    # 30-char source... 40-char source -> floor 36: translation 30 <= 36, protected.
    assert not exceeds_cps("a" * 30, 0, 1000, 20.0, "b" * 40)
    # boundary: len == protected budget is NOT flagged (strict >).
    assert not exceeds_cps("a" * 36, 0, 1000, 20.0, "b" * 40)
    assert exceeds_cps("a" * 37, 0, 1000, 20.0, "b" * 40)
    # Source tags don't count towards the floor (floor stays 36, so 37 flags).
    tagged = "{\\i1}" + "b" * 40 + "{\\i0}"
    assert exceeds_cps("a" * 37, 0, 1000, 20.0, tagged)
    assert not exceeds_cps("a" * 36, 0, 1000, 20.0, tagged)


def test_exceeds_cps_agrees_with_check_readability():
    cases = [
        ("a" * 30, 1000, None),
        ("a" * 30, 1000, "b" * 40),
        ("a" * 37, 1000, "b" * 40),
        (f"{'a' * 15}{BS}N{'b' * 15}", 1000, None),
        ("a" * 20, 1000, None),
        ("a" * 50, 0, None),
    ]
    for text, duration, source in cases:
        flagged = any(
            f[0] == "high_cps"
            for f in check_readability(text, duration, 20.0, 1000, source)
        )
        assert flagged == exceeds_cps(text, 0, duration, 20.0, source), (text, duration, source)


def test_check_readability_message_and_details_unchanged():
    findings = check_readability("a" * 30, 1000, 20.0, 42, "b" * 40)
    assert findings == []
    findings = check_readability("a" * 40, 1000, 20.0, 42)
    (qa_type, message, details), = [f for f in findings if f[0] == "high_cps"]
    assert message == "Reading speed 40.0 CPS exceeds limit 20 (fits in ~20 chars)."
    assert details == {"cps": 40.0, "limit": 20.0, "char_budget": 20}


def test_prompt_char_budget_keeps_minimum_floor_and_source_floor():
    assert prompt_char_budget(0, 3000, 20.0) == 60
    assert prompt_char_budget(0, 40, 20.0) is None
    assert prompt_char_budget(0, 450, 20.0) is None  # 9 chars < MIN_CHAR_BUDGET
    assert MIN_CHAR_BUDGET == 10
    assert prompt_char_budget(0, 500, 20.0) == 10  # exactly at the floor survives
    assert prompt_char_budget(1000, 1000, 20.0) is None
    # Source floor lifts a tiny CPS budget above the minimum.
    assert prompt_char_budget(0, 450, 20.0, "x" * 100) == 90
    # Matches the shared helper whenever the minimum doesn't apply.
    assert prompt_char_budget(0, 2000, 20.0, "x" * 10) == char_budget(0, 2000, 20.0, "x" * 10)
