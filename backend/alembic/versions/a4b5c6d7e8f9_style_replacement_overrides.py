"""subtitle_styles: optional translated-output style overrides

Adds nullable replacement_bold/italic/outline/shadow and the three replacement
colours. All NULL = inherit the source value, so existing rows keep rendering
exactly as before. The immutable source_style_hash is untouched.

Revision ID: a4b5c6d7e8f9
Revises: f2a3b4c5d6e7
Create Date: 2026-10-09
"""
from alembic import op
import sqlalchemy as sa

revision = "a4b5c6d7e8f9"
down_revision = "f2a3b4c5d6e7"
branch_labels = None
depends_on = None

_COLUMNS = (
    ("replacement_bold", sa.Integer()),
    ("replacement_italic", sa.Integer()),
    ("replacement_outline", sa.Float()),
    ("replacement_shadow", sa.Float()),
    ("replacement_primary_colour", sa.Text()),
    ("replacement_outline_colour", sa.Text()),
    ("replacement_back_colour", sa.Text()),
)


def upgrade() -> None:
    with op.batch_alter_table("subtitle_styles", schema=None) as batch_op:
        for name, type_ in _COLUMNS:
            batch_op.add_column(sa.Column(name, type_, nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("subtitle_styles", schema=None) as batch_op:
        for name, _ in reversed(_COLUMNS):
            batch_op.drop_column(name)
