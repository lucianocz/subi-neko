"""project-level canonical subtitle styles (M:N with files)

Revision ID: e7f8a9b0c1d2
Revises: b8c9d0e1f2a3
Create Date: 2026-10-02

``subtitle_styles`` used to be owned by one file (``file_id``). It becomes a
project-level canonical record identified by ``(project_id, source_style_hash)``
and files reference it through the pure join table ``file_subtitle_styles``.

Data migration: each legacy row gets ``project_id`` (from its file) and the
immutable ``source_style_hash``; rows that are equivalent within a project are
merged into the lowest-id one (adopting an already font-checked row's
replacement settings if the kept row is still unchecked) and every legacy
row's file link is preserved.

The hash helpers below deliberately duplicate ``app.subs.style_canonical`` so the
migration stays frozen; ``tests/test_style_canonical.py`` asserts they agree.
"""
from __future__ import annotations

import hashlib
import json

import sqlalchemy as sa
from alembic import op

revision = "e7f8a9b0c1d2"
down_revision = "b8c9d0e1f2a3"
branch_labels = None
depends_on = None

_TEXT = ("style_name", "font_name", "primary_colour", "secondary_colour", "outline_colour", "back_colour")
_INT = ("bold", "italic", "underline", "strikeout", "border_style",
        "alignment", "margin_l", "margin_r", "margin_v", "encoding")
_FLOAT = ("font_size", "scale_x", "scale_y", "spacing", "angle", "outline", "shadow")
_ASS_COLUMNS = _TEXT + _INT + _FLOAT
_META = ("replacement_font_name", "replacement_font_size", "font_check_status", "created_at", "updated_at")


def _source_hash(values: dict) -> str:
    out = {}
    for k in _TEXT:
        out[k] = None if values[k] is None else str(values[k])
    for k in _INT:
        out[k] = None if values[k] is None else int(values[k])
    for k in _FLOAT:
        out[k] = None if values[k] is None else round(float(values[k]), 4) + 0.0
    payload = json.dumps(out, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _columns(with_file: bool):
    cols = [sa.Column("id", sa.Integer(), autoincrement=True, nullable=False)]
    if with_file:
        cols.append(sa.Column("file_id", sa.Integer(), nullable=False))
    else:
        cols += [sa.Column("project_id", sa.Integer(), nullable=False),
                 sa.Column("source_style_hash", sa.Text(), nullable=False)]
    cols += [
        sa.Column("style_name", sa.Text(), nullable=False),
        sa.Column("font_name", sa.Text(), nullable=False),
        sa.Column("font_size", sa.Float(), nullable=False),
        sa.Column("primary_colour", sa.Text(), nullable=True),
        sa.Column("secondary_colour", sa.Text(), nullable=True),
        sa.Column("outline_colour", sa.Text(), nullable=True),
        sa.Column("back_colour", sa.Text(), nullable=True),
        sa.Column("bold", sa.Integer(), nullable=True),
        sa.Column("italic", sa.Integer(), nullable=True),
        sa.Column("underline", sa.Integer(), nullable=True),
        sa.Column("strikeout", sa.Integer(), nullable=True),
        sa.Column("scale_x", sa.Float(), nullable=True),
        sa.Column("scale_y", sa.Float(), nullable=True),
        sa.Column("spacing", sa.Float(), nullable=True),
        sa.Column("angle", sa.Float(), nullable=True),
        sa.Column("border_style", sa.Integer(), nullable=True),
        sa.Column("outline", sa.Float(), nullable=True),
        sa.Column("shadow", sa.Float(), nullable=True),
        sa.Column("alignment", sa.Integer(), nullable=True),
        sa.Column("margin_l", sa.Integer(), nullable=True),
        sa.Column("margin_r", sa.Integer(), nullable=True),
        sa.Column("margin_v", sa.Integer(), nullable=True),
        sa.Column("encoding", sa.Integer(), nullable=True),
        sa.Column("replacement_font_name", sa.Text(), nullable=True),
        sa.Column("replacement_font_size", sa.Float(), nullable=True),
        sa.Column("font_check_status", sa.Text(), server_default="unchecked", nullable=False),
        sa.Column("created_at", sa.Text(), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.Column("updated_at", sa.Text(), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
    ]
    return cols


def upgrade() -> None:
    bind = op.get_bind()

    op.create_table(
        "subtitle_styles_new",
        *_columns(with_file=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "source_style_hash"),
    )

    select_cols = ", ".join(f"s.{c}" for c in _ASS_COLUMNS + _META)
    legacy = bind.execute(sa.text(
        f"SELECT s.id AS old_id, s.file_id, f.project_id, {select_cols} "
        "FROM subtitle_styles s JOIN files f ON f.id = s.file_id ORDER BY s.id"
    )).mappings().all()

    keeper_by_key: dict[tuple[int, str], int] = {}
    keeper_row: dict[int, dict] = {}
    links: set[tuple[int, int]] = set()
    for row in legacy:
        row = dict(row)
        key = (row["project_id"], _source_hash(row))
        new_id = keeper_by_key.get(key)
        if new_id is None:
            new_id = row["old_id"]
            keeper_by_key[key] = new_id
            keeper_row[new_id] = {**row, "source_style_hash": key[1]}
        else:
            kept = keeper_row[new_id]
            if kept["font_check_status"] == "unchecked" and row["font_check_status"] != "unchecked":
                for col in ("replacement_font_name", "replacement_font_size", "font_check_status"):
                    kept[col] = row[col]
        links.add((row["file_id"], new_id))

    new_cols = ["id", "project_id", "source_style_hash"] + list(_ASS_COLUMNS + _META)
    insert_sql = sa.text(
        f"INSERT INTO subtitle_styles_new ({', '.join(new_cols)}) "
        f"VALUES ({', '.join(':' + c for c in new_cols)})"
    )
    for new_id, row in keeper_row.items():
        bind.execute(insert_sql, {**{c: row[c] for c in new_cols if c != "id"}, "id": new_id})

    # Old table (and its index) goes first, then the replacement takes its name
    # before the join table is created so the join FK references the final name.
    op.drop_table("subtitle_styles")
    op.rename_table("subtitle_styles_new", "subtitle_styles")
    op.create_table(
        "file_subtitle_styles",
        sa.Column("file_id", sa.Integer(), nullable=False),
        sa.Column("subtitle_style_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["file_id"], ["files.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["subtitle_style_id"], ["subtitle_styles.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("file_id", "subtitle_style_id"),
    )
    for file_id, style_id in sorted(links):
        bind.execute(
            sa.text("INSERT INTO file_subtitle_styles (file_id, subtitle_style_id) VALUES (:f, :s)"),
            {"f": file_id, "s": style_id},
        )
    op.create_index("idx_subtitle_styles_project_font_check_status", "subtitle_styles",
                    ["project_id", "font_check_status"])
    op.create_index("idx_file_subtitle_styles_style", "file_subtitle_styles", ["subtitle_style_id"])


def downgrade() -> None:
    """Re-expand to one row per (file, style); shared replacement settings are copied."""
    bind = op.get_bind()
    op.create_table(
        "subtitle_styles_old",
        *_columns(with_file=True),
        sa.ForeignKeyConstraint(["file_id"], ["files.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("file_id", "style_name"),
    )
    cols = ", ".join(_ASS_COLUMNS + _META)
    bind.execute(sa.text(
        f"INSERT INTO subtitle_styles_old (file_id, {cols}) "
        f"SELECT l.file_id, {', '.join('s.' + c for c in _ASS_COLUMNS + _META)} "
        "FROM file_subtitle_styles l JOIN subtitle_styles s ON s.id = l.subtitle_style_id "
        "ORDER BY l.file_id, s.id"
    ))
    op.drop_table("file_subtitle_styles")
    op.drop_table("subtitle_styles")
    op.rename_table("subtitle_styles_old", "subtitle_styles")
    op.create_index("idx_subtitle_styles_file_font_check_status", "subtitle_styles",
                    ["file_id", "font_check_status"])
