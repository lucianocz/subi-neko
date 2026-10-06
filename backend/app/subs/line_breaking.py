r"""Deterministic dialogue reflow: balanced auto line breaks + auto-join.

Runs in review_chunk_final (before the readability check) for ordinary
dialogue chunks only (that handler skips every non-``dialogue`` content type,
so signs/karaoke/songs/effects/comments never reach this module). One pass,
``reflow_dialogue``, decides everything for a line:

1. wrapped line whose joined text fits ``join_under`` (and the row limit) →
   join the break away (one shared join, exactly one visible space);
2. text longer than the row limit → best *balanced, linguistically legal*
   two-row split (never "fill row 1, spill the tail");
3. already-wrapped text with a clearly poor split (e.g. 54/12) → re-split.

The function is idempotent: its output fed back in returns ``None``.

ASS safety: the line is tokenised into override blocks (``{...}``) and visible
words. Breaks are only ever placed at a word separator, never inside a block;
blocks stay glued to the word they sit next to, unchanged and in order. Lines
with soft breaks (``\n``), hard spaces (``\h``), drawings or positioning
tags are left alone. Multi-speaker dash dialogue (``- A\N- B``) is
structural and never touched.

The break point is chosen by a small cost function (see ``_break_cost``)
that weighs row balance against Czech phrase structure, not balance alone.
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

# One-letter prepositions/conjunctions (a i k o s u v z) and the common short
# prepositions are the ugliest thing to strand at the end of a row.
_ONE_LETTER = {"a", "i", "k", "o", "s", "u", "v", "z"}
_SHORT_PREPOSITIONS = {"do", "na", "od", "po", "za", "ze", "ke", "ve"}
_STRONG_DANGLING = _ONE_LETTER | _SHORT_PREPOSITIONS

_CLAUSE_BREAK_BONUS = 20
_DANGLING_WORD_PENALTY = 40    # preposition/conjunction left at the end of row 1
_STRONG_DANGLING_PENALTY = 60  # one-letter / short preposition left there
_CLITIC_PENALTY = 30           # enclitic pushed to the start of row 2
_ORPHAN_PENALTY = 25           # a row that is one short word
_PUNCT_BREAK_PENALTY = 1000    # break right before , . ! ? : ; …
# An existing 2-row split is only replaced when the best alternative is at
# least this much cheaper (keeps reflow idempotent and avoids churn).
_REBALANCE_MARGIN = 15

_LEADING_PUNCT_RE = re.compile(r"^[,.!?:;…]")
_WORD_CLEAN_RE = re.compile(r"^\W+|\W+$", re.UNICODE)


def _bare(word: str) -> str:
    """A word stripped of surrounding punctuation, for set lookups."""
    return _WORD_CLEAN_RE.sub("", word).casefold()


def _break_cost(row1: str, row2: str, last_word: str, next_word: str) -> int:
    """Lower is better. Row balance, adjusted for what the break lands on."""
    cost = abs(len(row1) - len(row2))

    if _LEADING_PUNCT_RE.match(next_word):
        cost += _PUNCT_BREAK_PENALTY

    if _CLAUSE_END_RE.search(row1):
        cost -= _CLAUSE_BREAK_BONUS

    tail = _bare(last_word)
    if tail in _STRONG_DANGLING:
        cost += _STRONG_DANGLING_PENALTY
    elif tail in _PREPOSITIONS or tail in _CONJUNCTIONS:
        cost += _DANGLING_WORD_PENALTY

    if _bare(next_word) in _CLITICS:
        cost += _CLITIC_PENALTY

    for row in (row1, row2):
        if " " not in row.strip() and len(_bare(row)) < 5:
            cost += _ORPHAN_PENALTY

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


# ---------------------------------------------------------------------------
# Tag-aware reflow
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(
    r"(?P<tag>\{[^}]*\})"
    r"|(?P<brk>[ \t]*\\N[ \t]*)"
    r"|(?P<sp>[ \t]+)"
    r"|(?P<txt>(?:(?!\\N)[^\s{])+)"
)
# Drawings and positioning tie the line structure to geometry: never reflow.
_UNSAFE_TAG_RE = re.compile(r"\\(?:p\d|pos|move|org|i?clip)")
_DASH_START_RE = re.compile(r"^[-–—]")


class _Parsed:
    """A line as visible words (raw text incl. glued override blocks) and the
    separators between them (``"sp"`` space / ``"brk"`` hard ``\\N``)."""

    def __init__(self, raws: list[str], viss: list[str], seps: list[str]) -> None:
        self.raws, self.viss, self.seps = raws, viss, seps

    def row_ranges(self) -> list[tuple[int, int]]:
        """[start, end) word index per existing row."""
        out, start = [], 0
        for i, sep in enumerate(self.seps):
            if sep == "brk":
                out.append((start, i + 1))
                start = i + 1
        out.append((start, len(self.raws)))
        return out

    def vis_row(self, lo: int, hi: int) -> str:
        return " ".join(self.viss[lo:hi])

    def render(self, break_after: int | None) -> str:
        """Rebuild the line: single spaces between words and one hard break
        after word index ``break_after`` (None = no break)."""
        out = [self.raws[0]]
        for i in range(1, len(self.raws)):
            out.append("\\N" if break_after == i - 1 else " ")
            out.append(self.raws[i])
        return "".join(out)


def _parse(text: str) -> _Parsed | None:
    """Tokenise ``text`` or return None when it is not safe to reflow."""
    if not text:
        return None
    raws: list[str] = []
    viss: list[str] = []
    seps: list[str] = []
    cur_raw = cur_vis = ""
    pending: str | None = None  # separator seen after the last finished word
    pos = 0

    for m in _TOKEN_RE.finditer(text):
        if m.start() != pos:
            return None  # stray "{" or other unmatched content
        pos = m.end()
        kind = m.lastgroup
        tok = m.group()
        if kind == "tag":
            if _UNSAFE_TAG_RE.search(tok):
                return None
            cur_raw += tok
        elif kind == "txt":
            if "\\n" in tok or "\\h" in tok:
                return None
            cur_raw += tok
            cur_vis += tok
        else:  # separator ("sp" or "brk")
            sep = "brk" if kind == "brk" else "sp"
            if cur_vis:
                if raws:
                    seps.append(pending or "sp")
                raws.append(cur_raw)
                viss.append(cur_vis)
                cur_raw = cur_vis = ""
                pending = sep
            else:
                if cur_raw and raws:
                    # Standalone block(s) between separators: glue to the
                    # previous word so the separators collapse into one.
                    raws[-1] += cur_raw
                    cur_raw = ""
                if sep == "brk":
                    if not raws:
                        return None  # leading empty row
                    pending = "brk"
                elif raws and pending is None:
                    pending = "sp"
    if pos != len(text):
        return None
    if cur_vis:
        if raws:
            seps.append(pending or "sp")
        raws.append(cur_raw)
        viss.append(cur_vis)
    else:
        if cur_raw and raws:
            raws[-1] += cur_raw
        if pending == "brk":
            return None  # trailing empty row: the empty-hard-break repair owns it
    if not raws:
        return None
    return _Parsed(raws, viss, seps)


def _split_costs(p: _Parsed, max_row_chars: int) -> list[tuple[int, int]]:
    """(cost, break_after_index) for every legal two-row split, best first."""
    out = []
    n = len(p.raws)
    for i in range(1, n):
        row1, row2 = p.vis_row(0, i), p.vis_row(i, n)
        if len(row1) <= max_row_chars and len(row2) <= max_row_chars:
            out.append((_break_cost(row1, row2, p.viss[i - 1], p.viss[i]), i - 1))
    out.sort()
    return out


def reflow_dialogue(
    text: str,
    max_row_chars: int,
    *,
    join_under: int = 0,
    auto_join: bool = False,
    auto_break: bool = True,
) -> str | None:
    """Return the reflowed ``text`` or None when it should stay as it is.

    Only ever called for ordinary dialogue. ``join_under`` is capped at the
    row limit (a joined line must still fit one row), which is also what makes
    join and wrap agree: after a join the text fits, after a wrap it doesn't.
    """
    p = _parse(text)
    if p is None:
        return None
    ranges = p.row_ranges()
    # Multi-speaker dash dialogue: the break is the structure.
    if sum(1 for lo, _ in ranges if _DASH_START_RE.match(p.viss[lo])) > 1:
        return None

    total = len(" ".join(p.viss))
    n_rows = len(ranges)
    result: str | None = None

    if n_rows > 1 and auto_join and total <= min(join_under, max_row_chars):
        result = p.render(None)
    elif auto_break and total > max_row_chars:
        splits = _split_costs(p, max_row_chars)
        if splits:
            best_cost, best_at = splits[0]
            if n_rows != 2 or any(len(p.vis_row(lo, hi)) > max_row_chars for lo, hi in ranges):
                result = p.render(best_at)
            else:
                cur_at = ranges[0][1] - 1
                cur_cost = next((c for c, at in splits if at == cur_at), None)
                if cur_cost is None or cur_cost - best_cost >= _REBALANCE_MARGIN:
                    result = p.render(best_at)
    return result if result is not None and result != text else None


def rebalance_rows(text: str, max_row_chars: int) -> str | None:
    """Balanced wrap only (no join) — see ``reflow_dialogue``."""
    return reflow_dialogue(text, max_row_chars, auto_break=True, auto_join=False)
