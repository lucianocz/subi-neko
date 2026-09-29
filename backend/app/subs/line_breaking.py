"""Deterministic row rebalancing for over-long dialogue rows.

Runs in review_chunk_final (before the readability check) when
AUTO_LINE_BREAK is on: a dialogue line whose row(s) exceed the row-length
limit is re-broken at a word boundary into two balanced rows. The long_row
check then only flags lines that even a balanced split cannot fit — real
condensing work for the reviewer instead of mechanical rewrapping.

Deliberately conservative: lines with inline override tags, soft breaks
(\\n) or hard spaces (\\h) are left untouched, because break placement may
interact with positioning/typesetting.

The break point itself is chosen by a small cost function (see
``_break_cost``) that weighs row balance against Czech phrase structure,
rather than by balance alone.
"""
from __future__ import annotations

import re

_LEADING_OVERRIDE_RE = re.compile(r"^(?:\{[^}]*\})+")
_OVERRIDE_RE = re.compile(r"\{\\[^}]*\}")
_HARD_BREAK_RE = re.compile(r"\s*\\N\s*")

# ---------------------------------------------------------------------------
# Where to break
#
# Balance alone is not a good objective: the most even split routinely lands
# between a preposition and its noun ("na / stole"), strands a conjunction at
# the end of the first row, or pushes an enclitic to the start of the second
# ("se", "jsem", "by") — all of which read as broken Czech even when the
# translation is right. Subtitle practice is to break at the highest syntactic
# boundary available, so balance becomes one term in a cost function rather
# than the whole of it.
# ---------------------------------------------------------------------------

# Breaking right after clause-final punctuation is the best break there is.
_CLAUSE_END_RE = re.compile(r"[,.!?…:;–—-]$")

# Prepositions bind to the noun phrase that follows them; leaving one at the
# end of a row splits the phrase across the break.
#
# "se" and "si" are deliberately NOT here. They are overwhelmingly the
# reflexive enclitics rather than the vocalized preposition, and an enclitic
# leans on the word *before* it — so ending a row with "se" is correct, and
# only opening a row with it is wrong (see _CLITICS).
_PREPOSITIONS = {
    "k", "s", "v", "z", "o", "u", "do", "na", "po", "za", "od", "ze", "ke",
    "ve", "pro", "při", "bez", "nad", "pod", "před", "mezi",
    "přes", "kolem", "podle", "kvůli", "vedle", "proti", "mimo", "skrz",
    "okolo", "během", "místo", "díky", "oproti", "beze", "pode", "nade",
    "přede", "ku", "ob",
}

# A conjunction introduces what comes next, so it belongs with the second row.
_CONJUNCTIONS = {
    "a", "i", "ale", "nebo", "že", "aby", "když", "protože", "jestli", "až",
    "ani", "však", "neboť", "či", "takže", "pokud", "zatímco", "jakmile",
    "dokud", "jelikož", "anebo", "abych", "abys", "aby's", "kdyby", "jako",
}

# Second-position enclitics: they lean on the preceding word and cannot open
# a row. Deliberately limited to forms that are unambiguously enclitic —
# "to" and "je" are excluded because they open sentences perfectly normally.
_CLITICS = {
    "se", "si", "jsem", "jsi", "jsme", "jste", "by", "bych", "bys",
    "bychom", "byste", "mi", "ti", "mu", "ho", "ji", "jí", "mě", "tě",
}

_CLAUSE_BREAK_BONUS = 20
_DANGLING_WORD_PENALTY = 40   # preposition/conjunction left at the end of row 1
_CLITIC_PENALTY = 30          # enclitic pushed to the start of row 2

_WORD_CLEAN_RE = re.compile(r"^\W+|\W+$", re.UNICODE)


def _bare(word: str) -> str:
    """A word stripped of surrounding punctuation, for set lookups."""
    return _WORD_CLEAN_RE.sub("", word).casefold()


def _break_cost(row1: str, row2: str, last_word: str, next_word: str) -> int:
    """Lower is better. Row balance, adjusted for what the break lands on."""
    cost = abs(len(row1) - len(row2))

    if _CLAUSE_END_RE.search(row1):
        cost -= _CLAUSE_BREAK_BONUS

    tail = _bare(last_word)
    if tail in _PREPOSITIONS or tail in _CONJUNCTIONS:
        cost += _DANGLING_WORD_PENALTY

    if _bare(next_word) in _CLITICS:
        cost += _CLITIC_PENALTY

    return cost


def empty_hard_break_edges(text: str) -> set[str]:
    """Return visible edges occupied by ``\\N`` (``leading``/``trailing``).

    Override blocks and surrounding whitespace are not visible subtitle
    content, so they do not make an otherwise empty rendered row non-empty.
    """
    visible = _OVERRIDE_RE.sub("", text or "").strip()
    edges: set[str] = set()
    if visible.startswith(r"\N"):
        edges.add("leading")
    if visible.endswith(r"\N"):
        edges.add("trailing")
    return edges


def _best_two_row_split(words: list[str], max_row_chars: int) -> tuple[str, str] | None:
    best: tuple[int, str, str] | None = None
    for i in range(1, len(words)):
        row1 = " ".join(words[:i])
        row2 = " ".join(words[i:])
        if len(row1) <= max_row_chars and len(row2) <= max_row_chars:
            cost = _break_cost(row1, row2, words[i - 1], words[i])
            if best is None or cost < best[0]:
                best = (cost, row1, row2)
    return None if best is None else (best[1], best[2])


def rebalance_empty_hard_break(text: str, max_row_chars: int) -> str | None:
    """Move one edge ``\\N`` to a safe word boundary, preserving its count.

    This intentionally shares the ordinary dialogue rebalancer's safety
    boundary: only an optional leading override run is allowed. More complex
    inline typesetting, soft breaks and hard spaces are left for QA review.
    """
    if not empty_hard_break_edges(text) or text.count(r"\N") != 1:
        return None

    prefix, body = "", text
    match = _LEADING_OVERRIDE_RE.match(text)
    if match:
        prefix, body = text[:match.end()], text[match.end():]

    if "{" in body or r"\n" in body or r"\h" in body:
        return None

    words = " ".join(_HARD_BREAK_RE.split(body.strip())).split()
    if len(words) < 2:
        return None

    split = _best_two_row_split(words, max_row_chars)
    if split is None:
        return None
    fixed = f"{prefix}{split[0]}\\N{split[1]}"
    return fixed if fixed != text else None


def rebalance_rows(text: str, max_row_chars: int) -> str | None:
    """Return `text` re-broken so no row exceeds `max_row_chars`, or None
    when the line already fits, can't be fixed with two rows, or isn't safe
    to touch."""
    if not text:
        return None

    prefix, body = "", text
    m = _LEADING_OVERRIDE_RE.match(text)
    if m:
        prefix, body = text[: m.end()], text[m.end():]

    if "{" in body or "\\n" in body or "\\h" in body:
        return None

    rows = _HARD_BREAK_RE.split(body.strip())
    if all(len(row) <= max_row_chars for row in rows):
        return None

    words = " ".join(rows).split()
    if not words:
        return None

    full = " ".join(words)
    if len(full) <= max_row_chars:
        fixed = prefix + full
        return fixed if fixed != text else None

    best = _best_two_row_split(words, max_row_chars)
    if best is None:
        return None  # no two-row split fits — surfaces as long_row

    fixed = f"{prefix}{best[0]}\\N{best[1]}"
    return fixed if fixed != text else None
