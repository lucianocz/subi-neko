from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest

from app.subs.tag_masking import (
    force_unmask,
    mask_line,
    plain_text,
    unmask_line,
)


# ---------------------------------------------------------------------------
# Round-trip property: unmasking the untouched masked text must reproduce the
# original line exactly, for a corpus of real-world ASS shapes.
# ---------------------------------------------------------------------------

ROUND_TRIP_CORPUS = [
    "Plain dialogue line.",
    r"{\an8}Leading tag only",
    r"{\an8}{\i1}Two leading tags",
    r"I {\i1}really{\i0} mean it",
    r"{\pos(576,223.2)}{\an7}EPISODE 6",
    r"{\an8}Episode 11\NEmergency Quest: Save Enya!",
    r"Line with\Nnewline and\hhard space",
    r"{\fscx102\fscy118\frz9.139\pos(312.737,190.153)}[Evergelion]\NProduct details",
    r"wa{\i1}ta{\i0}shi mid-word italics",
    r"{\k20}ka{\k30}ra{\k25}o{\k40}ke",
    r"Trailing tag{\i0}",
    r"  leading spaces preserved",
    "",
]


@pytest.mark.parametrize("source", ROUND_TRIP_CORPUS)
def test_mask_unmask_round_trip_identity(source):
    masked = mask_line(source)
    result, errors = unmask_line(masked.text, masked)
    assert errors == []
    assert result == source


def test_leading_override_run_is_hidden_from_llm():
    masked = mask_line(r"{\an8}{\i1}Hello there")
    assert masked.prefix == r"{\an8}{\i1}"
    assert masked.text == "Hello there"
    assert masked.tokens == {}


def test_inline_tags_become_indexed_markers():
    masked = mask_line(r"I {\i1}really{\i0} mean it")
    assert masked.text == "I ⟦1⟧really⟦2⟧ mean it"
    assert masked.tokens == {1: r"{\i1}", 2: r"{\i0}"}


def test_escapes_become_single_characters():
    masked = mask_line(r"One\NTwo\nThree\hFour")
    assert "\\" not in masked.text
    assert masked.escape_counts == {"⏎": 1, "␤": 1, "␣": 1}


def test_unmask_allows_marker_movement():
    source = r"I {\i1}really{\i0} mean it"
    masked = mask_line(source)
    result, errors = unmask_line("Myslím to ⟦1⟧vážně⟦2⟧", masked)
    assert errors == []
    assert result == r"Myslím to {\i1}vážně{\i0}"


def test_unmask_reattaches_hidden_prefix():
    source = r"{\an8}Hello"
    masked = mask_line(source)
    result, errors = unmask_line("Ahoj", masked)
    assert errors == []
    assert result == r"{\an8}Ahoj"


def test_unmask_detects_dropped_marker():
    masked = mask_line(r"A {\i1}b{\i0} c")
    result, errors = unmask_line("A b c", masked)
    assert result is None
    assert any(e.startswith("marker_count:1") for e in errors)
    assert any(e.startswith("marker_count:2") for e in errors)


def test_unmask_detects_duplicated_marker():
    masked = mask_line(r"A {\i1}b c")
    result, errors = unmask_line("A ⟦1⟧b ⟦1⟧c", masked)
    assert result is None
    assert any(e.startswith("marker_count:1=2") for e in errors)


def test_unmask_detects_invented_marker():
    masked = mask_line("Plain line")
    result, errors = unmask_line("Plain ⟦1⟧line", masked)
    assert result is None
    assert "unknown_marker:1" in errors


def test_unmask_detects_escape_count_mismatch():
    masked = mask_line(r"One\NTwo")
    result, errors = unmask_line("OneTwo", masked)
    assert result is None
    assert any(e.startswith("escape_count:") for e in errors)


def test_unmask_restores_escapes():
    masked = mask_line(r"One\NTwo\hThree")
    result, errors = unmask_line("Jedna⏎Dva␣Tři", masked)
    assert errors == []
    assert result == r"Jedna\NDva\hTři"


def test_unmask_allows_hard_space_count_change():
    # \h is alignment padding — translations may legitimately use a
    # different number of them (e.g. column-aligned signs).
    masked = mask_line(r"Name\h\h\h\hdies.\NOther\h\h\h\h\h\hlives.")
    result, errors = unmask_line("Jméno␣␣umírá.⏎Jiný␣␣␣přežívá.", masked)
    assert errors == []
    assert result == r"Jméno\h\humírá.\NJiný\h\h\hpřežívá."


def test_unmask_still_rejects_line_break_count_change():
    masked = mask_line(r"One\NTwo\h\hThree")
    result, errors = unmask_line("Jedna Dva␣Tři", masked)
    assert result is None
    assert any(e.startswith("escape_count:\\N") for e in errors)


def test_force_unmask_appends_missing_markers():
    source = r"A {\i1}b{\i0} c"
    masked = mask_line(source)
    result = force_unmask("A b c", masked)
    # No block is ever lost — missing ones land at the end.
    assert result.count(r"{\i1}") == 1
    assert result.count(r"{\i0}") == 1


def test_force_unmask_collapses_duplicate_markers():
    masked = mask_line(r"A {\i1}b c")
    result = force_unmask("A ⟦1⟧b ⟦1⟧c", masked)
    assert result.count(r"{\i1}") == 1


def test_plain_text_strips_tags_and_escapes():
    assert plain_text(r"{\an8}Hello\NWorld") == "Hello World"
    assert plain_text(r"{{\shad0}Item/Skin") == "Item/Skin"


@pytest.mark.parametrize("source,expected", [
    ("before {unfinished", "before {unfinished"),
    ("before {a{b} after", "before  after"),
    ("a{}{}b", "ab"),
    ("a}b{c}d{", "a}bd{"),
    ("{a\nb}text", "text"),
])
def test_plain_text_preserves_malformed_brace_behavior(source, expected):
    assert plain_text(source) == expected


def test_plain_text_handles_large_unclosed_brace_run():
    # Bound the old quadratic behavior without allowing a regression to hang CI.
    backend = Path(__file__).resolve().parents[1]
    subprocess.run(
        [sys.executable, "-c", (
            "from app.subs.tag_masking import plain_text; "
            "text = '{' * 300_000 + 'keep me'; "
            "assert plain_text(text) == text"
        )],
        cwd=backend,
        env={**os.environ, "PYTHONPATH": str(backend)},
        check=True,
        capture_output=True,
        timeout=10,
    )
