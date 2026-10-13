"""project archive state and English title

Revision ID: c6d7e8f9a0b1
Revises: b5c6d7e8f9a0
Create Date: 2026-10-13

Both columns are nullable with no backfill: every existing project stays
active (archived_at NULL) and gets its English title on the next metadata
refresh.
"""
from alembic import op
import sqlalchemy as sa

revision = 'c6d7e8f9a0b1'
down_revision = 'b5c6d7e8f9a0'
branch_labels = None
depends_on = None


# Plain ADD/DROP COLUMN (not batch mode): batch would rebuild the referenced
# `projects` table, which is needless risk for cascading foreign keys.
def upgrade() -> None:
    op.add_column('projects', sa.Column('archived_at', sa.Text(), nullable=True))
    op.add_column('projects', sa.Column('name_english', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('projects', 'name_english')
    op.drop_column('projects', 'archived_at')
