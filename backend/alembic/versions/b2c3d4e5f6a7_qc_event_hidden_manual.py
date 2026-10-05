"""final QC: subtitle_events.is_hidden / is_manual

* is_hidden — event excluded from the translated ASS (restorable); the source
  ASS is unaffected.
* is_manual — event created in Final QC; present only in the translated ASS.

Both NOT NULL DEFAULT 0, so every existing row backfills to false.

Revision ID: b2c3d4e5f6a7
Revises: a9b0c1d2e3f4
Create Date: 2026-10-05
"""
from alembic import op
import sqlalchemy as sa

revision = "b2c3d4e5f6a7"
down_revision = "a9b0c1d2e3f4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("subtitle_events", schema=None) as batch_op:
        batch_op.add_column(sa.Column("is_hidden", sa.Integer(), server_default="0", nullable=False))
        batch_op.add_column(sa.Column("is_manual", sa.Integer(), server_default="0", nullable=False))


def downgrade() -> None:
    # Manual events cannot exist in the old schema (they would leak into the
    # source ASS); drop them before the marker column goes away.
    op.execute("DELETE FROM subtitle_events WHERE is_manual = 1")
    with op.batch_alter_table("subtitle_events", schema=None) as batch_op:
        batch_op.drop_column("is_manual")
        batch_op.drop_column("is_hidden")
