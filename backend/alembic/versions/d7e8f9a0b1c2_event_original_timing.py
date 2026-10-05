"""subtitle_events.original_start_ms / original_end_ms

Immutable source timing, separate from the editable (QC) start_ms/end_ms so a
QC timing correction affects only the translated ASS.

Backfill: original = current for every existing row. Manual QC events have no
source counterpart; they get original = current as a structural fallback (they
are excluded from the source ASS via is_manual).

Revision ID: d7e8f9a0b1c2
Revises: b2c3d4e5f6a7
Create Date: 2026-10-05
"""
from alembic import op
import sqlalchemy as sa

revision = "d7e8f9a0b1c2"
down_revision = "b2c3d4e5f6a7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("subtitle_events", schema=None) as batch_op:
        batch_op.add_column(sa.Column("original_start_ms", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("original_end_ms", sa.Integer(), nullable=True))
    op.execute("UPDATE subtitle_events SET original_start_ms = start_ms, original_end_ms = end_ms")
    with op.batch_alter_table("subtitle_events", schema=None) as batch_op:
        batch_op.alter_column("original_start_ms", existing_type=sa.Integer(), nullable=False)
        batch_op.alter_column("original_end_ms", existing_type=sa.Integer(), nullable=False)


def downgrade() -> None:
    with op.batch_alter_table("subtitle_events", schema=None) as batch_op:
        batch_op.drop_column("original_end_ms")
        batch_op.drop_column("original_start_ms")
