"""glossary terms + address pairs: is_new review marker

Adds a non-null boolean ``is_new`` (server default TRUE, so automatic writers
that omit it mark their rows). Every row that exists at migration time is
backfilled to FALSE — nothing already in the database counts as "new".

Revision ID: b5c6d7e8f9a0
Revises: a4b5c6d7e8f9
Create Date: 2026-10-13
"""
from alembic import op
import sqlalchemy as sa

revision = "b5c6d7e8f9a0"
down_revision = "a4b5c6d7e8f9"
branch_labels = None
depends_on = None

_TABLES = ("project_glossary_terms", "project_address_pairs")


def upgrade() -> None:
    for table in _TABLES:
        # Add with a FALSE default so existing rows backfill to FALSE, then
        # flip the default to TRUE for rows inserted from now on.
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.add_column(sa.Column(
                "is_new", sa.Boolean(), nullable=False, server_default=sa.false()))
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.alter_column(
                "is_new", existing_type=sa.Boolean(), existing_nullable=False,
                server_default=sa.true())


def downgrade() -> None:
    for table in reversed(_TABLES):
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.drop_column("is_new")
