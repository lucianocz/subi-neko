"""Project-level canonical subtitle styles: normalization, hashing, find-or-create.

A style's identity is ``(project_id, source_style_hash)``. The hash covers the
style name plus every ORIGINAL ASS property as it would be persisted — never the
replacement font/size, font-check status, ids or timestamps — and is immutable.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable, Mapping

from sqlalchemy import delete, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.db.models import SubtitleStyle, file_subtitle_styles

# Source properties that define an imported style (and therefore the hash).
# Order is irrelevant (the JSON is key-sorted) but the set is part of the
# hash contract — changing it re-keys every future import.
_TEXT_FIELDS = ("style_name", "font_name", "primary_colour", "secondary_colour", "outline_colour", "back_colour")
_INT_FIELDS = (
    "bold", "italic", "underline", "strikeout", "border_style",
    "alignment", "margin_l", "margin_r", "margin_v", "encoding",
)
_FLOAT_FIELDS = ("font_size", "scale_x", "scale_y", "spacing", "angle", "outline", "shadow")
SOURCE_STYLE_FIELDS: tuple[str, ...] = _TEXT_FIELDS + _INT_FIELDS + _FLOAT_FIELDS


def normalize_source_style(values: Mapping[str, Any]) -> dict[str, Any]:
    """Canonical representation of a style's source definition (None-preserving)."""
    out: dict[str, Any] = {}
    for key in _TEXT_FIELDS:
        v = values.get(key)
        out[key] = None if v is None else str(v)
    for key in _INT_FIELDS:
        v = values.get(key)
        out[key] = None if v is None else int(v)
    for key in _FLOAT_FIELDS:
        v = values.get(key)
        # ``+ 0.0`` folds -0.0 into 0.0 so the JSON text is stable.
        out[key] = None if v is None else round(float(v), 4) + 0.0
    return out


def compute_source_style_hash(values: Mapping[str, Any]) -> str:
    payload = json.dumps(normalize_source_style(values), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def find_or_create_style(session: Session, project_id: int, row: Mapping[str, Any]) -> int:
    """Return the canonical style id for ``row``, inserting it when new.

    ``row`` carries the ASS properties (plus optional created_at/updated_at).
    Race-safe: the insert is ``ON CONFLICT DO NOTHING`` on the
    ``(project_id, source_style_hash)`` unique key, followed by a lookup, so two
    concurrent importers converge on one record.
    """
    normalized = normalize_source_style(row)
    style_hash = compute_source_style_hash(normalized)

    existing = session.scalar(
        select(SubtitleStyle.id).where(
            SubtitleStyle.project_id == project_id,
            SubtitleStyle.source_style_hash == style_hash,
        )
    )
    if existing is not None:
        return existing

    values = dict(normalized)
    values.update(project_id=project_id, source_style_hash=style_hash)
    for extra in ("created_at", "updated_at"):
        if extra in row:
            values[extra] = row[extra]
    session.execute(
        sqlite_insert(SubtitleStyle).values(**values).on_conflict_do_nothing(
            index_elements=["project_id", "source_style_hash"]
        )
    )
    return session.scalar(
        select(SubtitleStyle.id).where(
            SubtitleStyle.project_id == project_id,
            SubtitleStyle.source_style_hash == style_hash,
        )
    )


def link_file_styles(session: Session, file_id: int, project_id: int, rows: Iterable[Mapping[str, Any]]) -> list[int]:
    """Replace ``file_id``'s style associations with canonical styles for ``rows``."""
    session.execute(delete(file_subtitle_styles).where(file_subtitle_styles.c.file_id == file_id))
    style_ids: list[int] = []
    for row in rows:
        style_id = find_or_create_style(session, project_id, row)
        style_ids.append(style_id)
        session.execute(
            sqlite_insert(file_subtitle_styles)
            .values(file_id=file_id, subtitle_style_id=style_id)
            .on_conflict_do_nothing()
        )
    return style_ids


def prune_orphan_styles(session: Session, project_id: int) -> int:
    """Delete the project's canonical styles that no file references any more.

    Called after a file's styles are re-linked (re-extraction). Running it after
    the relink means an unchanged style keeps its replacement settings.
    """
    referenced = select(file_subtitle_styles.c.subtitle_style_id)
    result = session.execute(
        delete(SubtitleStyle).where(
            SubtitleStyle.project_id == project_id,
            SubtitleStyle.id.not_in(referenced),
        )
    )
    return result.rowcount or 0
