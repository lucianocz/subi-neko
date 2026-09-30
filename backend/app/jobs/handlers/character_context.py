"""Deterministic character-description budgeting for preparation prompts."""
from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

from app.db.models import ProjectCharacter

_SENTENCE_END_RE = re.compile(r"(?<=[.!?])\s+")
_ROLE_RANK = {"MAIN": 0, "SUPPORTING": 1, "BACKGROUND": 2}


def _clean_description(value: str | None) -> str:
    return " ".join((value or "").split())


def truncate_description(value: str | None, limit: int) -> str | None:
    """Trim without inventing text, preferring the last sentence boundary."""
    text = _clean_description(value)
    if not text or limit <= 0:
        return None
    if len(text) <= limit:
        return text
    if limit == 1:
        return "…"

    # Reserve one character for the ellipsis. Avoid returning a tiny first
    # sentence when the boundary is very early in the available fragment.
    body_limit = limit - 1
    fragment = text[:body_limit].rstrip()
    boundaries = [m.start() for m in _SENTENCE_END_RE.finditer(fragment)]
    if boundaries:
        boundary = boundaries[-1]
        if boundary >= max(24, body_limit // 2):
            fragment = fragment[:boundary].rstrip()
    return fragment + "…"


def allocate_character_descriptions(
    characters: Sequence[ProjectCharacter],
    per_character_limit: int,
    aggregate_limit: int,
    relevant_character_ids: Iterable[int] = (),
) -> dict[int, str]:
    """Allocate only description text within two configured budgets.

    Every described character first receives an equal share, preventing a
    large early entry from starving the rest. Remaining capacity is then
    distributed in a stable relevance/role/roster order.
    """
    if per_character_limit <= 0 or aggregate_limit <= 0:
        return {}

    relevant = set(relevant_character_ids)
    entries: list[tuple[int, ProjectCharacter, str]] = []
    for position, character in enumerate(characters):
        text = _clean_description(character.description)
        if text:
            # Keep the full text until rendering so truncate_description can
            # mark an actual cut with an ellipsis instead of silently losing
            # the tail at this stage.
            entries.append((position, character, text))
    if not entries:
        return {}

    entries.sort(key=lambda item: (
        0 if item[1].id in relevant else 1,
        _ROLE_RANK.get((item[1].role or "").upper(), 3),
        item[0],
    ))

    available_lengths = [min(len(text), per_character_limit) for _, _, text in entries]
    total_available = sum(available_lengths)
    budget = min(aggregate_limit, total_available)
    fair_share = budget // len(entries)
    allocations = [min(length, fair_share) for length in available_lengths]
    remaining = budget - sum(allocations)

    # Priority only affects the surplus; every entry already received its
    # fair share above (possibly its whole description).
    for idx, length in enumerate(available_lengths):
        if remaining <= 0:
            break
        extra = min(length - allocations[idx], remaining)
        allocations[idx] += extra
        remaining -= extra

    result: dict[int, str] = {}
    for (_, character, text), allocation in zip(entries, allocations, strict=True):
        rendered = truncate_description(text, allocation)
        if rendered:
            result[character.id] = rendered
    return result


def build_preparation_character_block(
    characters: Sequence[ProjectCharacter], descriptions: dict[int, str],
) -> str:
    """Render complete preparation metadata in explicit, stable records.

    Values are collapsed to one line so provider/user text cannot blur record
    boundaries.  Descriptions have already been independently budgeted by the
    caller; this formatter never applies the translation-stage 200-char cap.
    """
    records: list[str] = []
    for character in characters:
        def field(value: str | None) -> str:
            return " ".join((value or "").split())

        fields = [
            "[CHARACTER]",
            f"external_id: {field(character.external_id) if character.external_id else f'internal:{character.id}'}",
            f"canonical_name: {field(character.name)}",
        ]
        for label, value in (
            ("aliases", character.aliases),
            ("gender", character.gender),
            ("role", character.role),
            ("character_type", character.character_type),
            ("voice_actor", character.voice_actor),
            ("social_position", character.social_position),
            ("notes", character.note),
            ("description", descriptions.get(character.id)),
        ):
            cleaned = field(value)
            if cleaned:
                fields.append(f"{label}: {cleaned}")
        fields.append("[/CHARACTER]")
        records.append("\n".join(fields))
    return "\n\n".join(records)
