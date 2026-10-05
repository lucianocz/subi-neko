"""Watched-word matching, mirroring the Subtitle Editor's row-level logic.

A watched word matches a text when the lower-cased text contains the
lower-cased word (plain substring, no word boundaries). Each watched-word
*definition* counts at most once per text — the row badges render one badge
per matching definition, not one per repeated occurrence — so the editor's
header total is the number of (event, matching definition) pairs.

``original`` words are matched against the event's source text, ``translated``
words against its current translated text.
"""
from __future__ import annotations

from typing import Iterable, Sequence


def matching_watched_words(text: str | None, words: Sequence[str]) -> list[str]:
    haystack = (text or "").lower()
    if not haystack:
        return []
    return [word for word in words if word and word.lower() in haystack]


def count_watched_matches(
    events: Iterable[tuple[str | None, str | None]],
    original_words: Sequence[str],
    translated_words: Sequence[str],
) -> int:
    """Total matches over ``(source_text, translated_text)`` pairs."""
    total = 0
    for source_text, translated_text in events:
        total += len(matching_watched_words(source_text, original_words))
        total += len(matching_watched_words(translated_text, translated_words))
    return total
