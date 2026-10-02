"""file_quality_metrics: source_fingerprint

Metrics are now refreshed progressively while a file is still being
processed; the fingerprint of the inputs a snapshot was computed from lets the
orchestrator tell a current snapshot from a stale one without recomputing.

Revision ID: b8c9d0e1f2a3
Revises: a7b8c9d0e1f2
Create Date: 2026-10-02
"""
from alembic import op
import sqlalchemy as sa

revision = 'b8c9d0e1f2a3'
down_revision = 'a7b8c9d0e1f2'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('file_quality_metrics') as batch:
        batch.add_column(sa.Column('source_fingerprint', sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('file_quality_metrics') as batch:
        batch.drop_column('source_fingerprint')
