"""per-file branding overlay: file_branding

One row per file (UNIQUE file_id, FK ON DELETE CASCADE) holding only the
template filename and a global start offset. The overlay is merged into the
translated ASS at render time; nothing is imported into subtitle_events.

Revision ID: e1f2a3b4c5d6
Revises: d7e8f9a0b1c2
Create Date: 2026-10-07
"""
from alembic import op
import sqlalchemy as sa

revision = "e1f2a3b4c5d6"
down_revision = "d7e8f9a0b1c2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "file_branding",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("file_id", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Integer(), server_default="0", nullable=False),
        sa.Column("template_filename", sa.Text(), nullable=True),
        sa.Column("start_offset_ms", sa.Integer(), server_default="0", nullable=False),
        sa.ForeignKeyConstraint(["file_id"], ["files.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("file_id", name="uq_file_branding_file"),
    )


def downgrade() -> None:
    op.drop_table("file_branding")
