"""Pure reading-speed (CPS) helpers shared by QA checks, prompt budgets and
the subtitle editor, so every consumer measures the same thing.

Semantics (unchanged from the original inline implementations):
- visible text = translated text split on literal ``\\N`` rows, each row run
  through ``plain_text`` (ASS ``{...}`` blocks removed, ``\\N``/``\\n``/``\\h``
  → space, trimmed), empty rows dropped, rows joined with a single space;
- punctuation and interior spaces count;
- duration = ``end_ms - start_ms``; duration <= 0 or empty visible text → no CPS;
- comparisons are strict ``>`` and nothing is rounded before comparing.

QA additionally never flags a line that isn't longer than the English source
needed in the same slot (``SOURCE_FLOOR_RATIO``) — a timing quirk, not fat to
cut. That floor is part of the budget, not of the raw CPS value.
"""
from __future__ import annotations

import re

from app.subs.tag_masking import plain_text

# A line isn't flagged (and a prompt budget never shrinks below) this share of
# the English source's own visible length.
SOURCE_FLOOR_RATIO = 0.9

_ROW_BREAK_RE = re.compile(r"\\N")


def visible_rows(text: str | None) -> list[str]:
    """Per-row visible text (empty rows kept, so row counts stay faithful)."""
    return [plain_text(part) for part in _ROW_BREAK_RE.split(text or "")]


def visible_text(text: str | None) -> str:
    return " ".join(row for row in visible_rows(text) if row)


def visible_len(text: str | None) -> int:
    return len(visible_text(text))


def compute_cps(text: str | None, start_ms: int, end_ms: int) -> float | None:
    """Raw characters per second, or None when it is meaningless."""
    duration_ms = end_ms - start_ms
    if duration_ms <= 0:
        return None
    length = visible_len(text)
    if length == 0:
        return None
    return length / (duration_ms / 1000.0)


def char_budget(
    start_ms: int, end_ms: int, cps_limit: float, source_text: str | None = None,
) -> int | None:
    """Characters that fit in the slot at the CPS limit (floored at the
    source's own visible length times SOURCE_FLOOR_RATIO), or None when the
    duration is not positive."""
    duration_ms = end_ms - start_ms
    if duration_ms <= 0:
        return None
    budget = int(cps_limit * duration_ms / 1000.0)
    if source_text:
        source_floor = int(len(plain_text(source_text)) * SOURCE_FLOOR_RATIO)
        budget = max(budget, source_floor)
    return budget


def exceeds_cps(
    text: str | None,
    start_ms: int,
    end_ms: int,
    cps_limit: float,
    source_text: str | None = None,
) -> bool:
    """True when QA would raise ``high_cps``: the line is over the CPS limit
    AND longer than the protected character budget."""
    cps = compute_cps(text, start_ms, end_ms)
    if cps is None or not cps > cps_limit:
        return False
    budget = char_budget(start_ms, end_ms, cps_limit, source_text)
    return budget is not None and visible_len(text) > budget
