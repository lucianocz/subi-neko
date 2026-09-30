"""Heuristic content-type classification for subtitle events.

Tag-based signals are checked before style-name signals because many
fansub scripts leave styles unnamed/generic ("Default") or reuse one style
for both dialogue and signs — tag presence is unambiguous ASS semantics,
style naming is not. Classification never excludes a line from
translation; it only changes which prompt/validation path is used.

Used by extract_subtitles (initial per-event classification) and by
plan_translation_chunks (re-classification when a speaker content tag is
removed).
"""
from __future__ import annotations

import re

_KARAOKE_TAG_RE = re.compile(r"\\k[fo]?\d", re.IGNORECASE)
_POSITIONING_TAG_RE = re.compile(r"\\(?:pos|move|org|clip|iclip|t\()", re.IGNORECASE)
# Word-boundary search: real releases compose lyric style names ("Romaji
# Main", "ED Eng Main", "Copy of ED Romanji Secondary"). A bare "English"
# style is deliberately NOT matched — some releases use it for dialogue.
_SONG_STYLE_RE = re.compile(
    r"\b(op|ed|oped|song|insert|kara|karaoke|romaji|romanji|lyrics?)\d*\b",
    re.IGNORECASE,
)
_SIGN_STYLE_RE = re.compile(r"sign|title|caption|note|typeset|credit|logo", re.IGNORECASE)
_TAG_BLOCK_RE = re.compile(r"\{[^}]*\}")
_SPEAKER_LABEL_SEPARATOR_RE = re.compile(r"[-_]+")
_SIGN_SPEAKER_RE = re.compile(
    r"^(?:"
    r"signs?(?: (?:center|centre|left|right|top|bottom|middle|"
    r"top left|top right|bottom left|bottom right|"
    r"upper left|upper right|lower left|lower right))?"
    r"|ep ?title|episode title|title|typeset|typesetting"
    r")$",
    re.IGNORECASE,
)


def classify_speaker_content_tag(name: str) -> str | None:
    """Return a deterministic content tag for clear non-character labels.

    Separators and whitespace are normalized, but matching remains anchored
    to the complete label so character names containing words such as
    ``sign`` are not caught accidentally.
    """
    normalized = _SPEAKER_LABEL_SEPARATOR_RE.sub(" ", (name or "").strip())
    normalized = " ".join(normalized.split())
    if _SIGN_SPEAKER_RE.fullmatch(normalized):
        return "sign"
    return None


def classify_content_type(event_type: str, style: str, text: str) -> tuple[str, str | None]:
    if event_type == "comment":
        return "other", "ass_comment"

    if _KARAOKE_TAG_RE.search(text):
        return "karaoke", "k_tag"

    if _POSITIONING_TAG_RE.search(text):
        return "sign", "positioning_tag"

    if style and _SONG_STYLE_RE.search(style):
        return "song", "style_name"

    if style and _SIGN_STYLE_RE.search(style):
        return "sign", "style_name"

    tag_chars = sum(len(m) for m in _TAG_BLOCK_RE.findall(text))
    if len(text) > 0 and (tag_chars / len(text)) > 0.3:
        return "sign", "tag_density"

    return "dialogue", None
