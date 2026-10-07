"""file_branding.scale_to_script_playres

Optional per-file scaling of the branding template's position/scale tags to
the subtitle script PlayRes. NOT NULL DEFAULT 0, so existing rows backfill to
false (behaviour unchanged).

Revision ID: f2a3b4c5d6e7
Revises: e1f2a3b4c5d6
Create Date: 2026-10-07
"""
from alembic import op
import sqlalchemy as sa

revision = "f2a3b4c5d6e7"
down_revision = "e1f2a3b4c5d6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("file_branding", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("scale_to_script_playres", sa.Integer(), server_default="0", nullable=False))


def downgrade() -> None:
    with op.batch_alter_table("file_branding", schema=None) as batch_op:
        batch_op.drop_column("scale_to_script_playres")
