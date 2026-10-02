"""Deterministic ASS tag masking around LLM calls.

The LLM never sees raw ASS markup. Before a line is sent:
- a leading override run ("{\\an8}{\\i1}Text") is stripped off entirely and
  kept as an invisible prefix,
- every remaining inline override block is replaced with an indexed marker
  ⟦1⟧, ⟦2⟧, … that the model must keep exactly once,
- the escapes \\N / \\n / \\h become the single characters ⏎ / ␤ / ␣ so no
  backslash ever enters or leaves the model.

After the response, unmask_line() verifies marker/escape integrity and
reassembles the exact original markup. A verification failure is reported to
the caller (which retries correctively); force_unmask() provides a
best-effort assembly that guarantees no tag block is ever lost.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

TOKEN_RE = re.compile(r"⟦(\d+)⟧")          # ⟦123⟧
_STRAY_TOKEN_CHAR_RE = re.compile(r"[⟦⟧]")  # lone ⟦ or ⟧
_LEADING_OVERRIDE_RUN_RE = re.compile(r"^(?:\{\\[^}]*\})+")

# ASS escape → single-character stand-in (and back)
_ESCAPE_TO_CHAR = {"\\N": "⏎", "\\n": "␤", "\\h": "␣"}  # ⏎ ␤ ␣
_CHAR_TO_ESCAPE = {v: k for k, v in _ESCAPE_TO_CHAR.items()}
_ESCAPE_RE = re.compile(r"\\[Nnh]")
_ESCAPE_CHAR_RE = re.compile("[⏎␤␣]")

_TAG_BLOCK_RE = re.compile(r"\{\\[^}]*\}")
_ANY_BRACE_BLOCK_RE = re.compile(r"\{[^}]*\}")


@dataclass
class MaskedLine:
    text: str                                  # what the LLM sees
    prefix: str = ""                           # hidden leading override run
    tokens: dict[int, str] = field(default_factory=dict)   # marker id → original block
    escape_counts: dict[str, int] = field(default_factory=dict)  # stand-in char → count

    @property
    def has_markup(self) -> bool:
        return bool(self.prefix or self.tokens or any(self.escape_counts.values()))


def mask_line(source_text: str) -> MaskedLine:
    text = source_text or ""

    prefix_match = _LEADING_OVERRIDE_RUN_RE.match(text)
    prefix = prefix_match.group(0) if prefix_match else ""
    rest = text[len(prefix):]

    tokens: dict[int, str] = {}

    def _tokenize(match: re.Match) -> str:
        token_id = len(tokens) + 1
        tokens[token_id] = match.group(0)
        return f"⟦{token_id}⟧"

    rest = _TAG_BLOCK_RE.sub(_tokenize, rest)

    def _escape_char(match: re.Match) -> str:
        return _ESCAPE_TO_CHAR[match.group(0)]

    rest = _ESCAPE_RE.sub(_escape_char, rest)

    escape_counts = {char: rest.count(char) for char in _CHAR_TO_ESCAPE}

    return MaskedLine(text=rest, prefix=prefix, tokens=tokens, escape_counts=escape_counts)


def unmask_line(
    llm_text: str, masked: MaskedLine, *, check_line_breaks: bool = True,
) -> tuple[str | None, list[str]]:
    """Verify marker/escape integrity and reassemble the original markup.

    Returns (final_text, []) on success or (None, errors) when the model
    corrupted markers/escapes — the caller should retry or fall back to
    force_unmask(). With check_line_breaks=False the ⏎/␤ counts are not
    verified (for callers whose line breaks are re-flowed later anyway).
    """
    errors: list[str] = []

    found: dict[int, int] = {}
    for match in TOKEN_RE.finditer(llm_text):
        token_id = int(match.group(1))
        found[token_id] = found.get(token_id, 0) + 1

    for token_id in masked.tokens:
        count = found.get(token_id, 0)
        if count != 1:
            errors.append(f"marker_count:{token_id}={count}")
    for token_id in found:
        if token_id not in masked.tokens:
            errors.append(f"unknown_marker:{token_id}")

    stray = _STRAY_TOKEN_CHAR_RE.sub("", TOKEN_RE.sub("", llm_text))
    if len(stray) != len(TOKEN_RE.sub("", llm_text)):
        errors.append("stray_marker_char")

    for char, expected in masked.escape_counts.items():
        # ␣ (\h) is presentation padding — its count legitimately changes
        # with translated word widths (e.g. column alignment), so it is not
        # verified. ⏎/␤ (line breaks) must be preserved exactly.
        if char == "␣" or not check_line_breaks:
            continue
        actual = llm_text.count(char)
        if actual != expected:
            escape = _CHAR_TO_ESCAPE[char]
            errors.append(f"escape_count:{escape}={actual}!={expected}")

    if errors:
        return None, errors

    return _assemble(llm_text, masked), []


def force_unmask(llm_text: str, masked: MaskedLine) -> str:
    """Best-effort assembly that never loses a tag block: duplicate markers
    are collapsed to their first occurrence, missing markers are appended at
    the end of the line. Escape-count mismatches are left for validation."""
    seen: set[int] = set()
    parts: list[str] = []
    last = 0

    # Clean stray ⟦/⟧ only in the segments BETWEEN valid tokens, so kept
    # tokens survive intact.
    for match in TOKEN_RE.finditer(llm_text):
        parts.append(_STRAY_TOKEN_CHAR_RE.sub("", llm_text[last:match.start()]))
        token_id = int(match.group(1))
        if token_id in masked.tokens and token_id not in seen:
            seen.add(token_id)
            parts.append(match.group(0))
        last = match.end()
    parts.append(_STRAY_TOKEN_CHAR_RE.sub("", llm_text[last:]))

    text = "".join(parts)
    for token_id in masked.tokens:
        if token_id not in seen:
            text += f"⟦{token_id}⟧"

    return _assemble(text, masked)


def _assemble(text: str, masked: MaskedLine) -> str:
    def _restore_token(match: re.Match) -> str:
        return masked.tokens.get(int(match.group(1)), "")

    result = TOKEN_RE.sub(_restore_token, text)

    def _restore_escape(match: re.Match) -> str:
        return _CHAR_TO_ESCAPE[match.group(0)]

    result = _ESCAPE_CHAR_RE.sub(_restore_escape, result)
    return masked.prefix + result


def plain_text(text: str) -> str:
    """Strip all ASS markup for display/context/heuristics: remove {...}
    blocks and turn escapes into spaces."""
    cleaned = _ANY_BRACE_BLOCK_RE.sub("", text or "")
    cleaned = _ESCAPE_RE.sub(" ", cleaned)
    return cleaned.strip()
