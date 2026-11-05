"""Project-level TMDB import mapping: expected episode count, publish intent and
TMDB identity columns on project_episodes.

Revision ID: 8bb829bd511f
Revises: f3a4b5c6d7e8
"""

import sqlalchemy as sa
from alembic import op

revision = "8bb829bd511f"
down_revision = "f3a4b5c6d7e8"
branch_labels = None
depends_on = None

_EPISODE_TEXT_COLUMNS = ("tmdb_episode_id", "tmdb_name", "tmdb_air_date", "tmdb_overview")


def upgrade():
    # Existing projects keep NULLs: no TMDB migration is required.
    op.add_column("projects", sa.Column("web_publish_intent", sa.Text(), nullable=True))
    op.add_column("projects", sa.Column("expected_episode_count", sa.Integer(), nullable=True))
    for name in _EPISODE_TEXT_COLUMNS:
        op.add_column("project_episodes", sa.Column(name, sa.Text(), nullable=True))
    op.add_column("project_episodes", sa.Column("tmdb_runtime", sa.Integer(), nullable=True))
    op.create_index(
        "uq_project_episodes_tmdb", "project_episodes",
        ["project_id", "tmdb_episode_id"], unique=True,
    )


def downgrade():
    op.drop_index("uq_project_episodes_tmdb", table_name="project_episodes")
    op.drop_column("project_episodes", "tmdb_runtime")
    for name in reversed(_EPISODE_TEXT_COLUMNS):
        op.drop_column("project_episodes", name)
    op.drop_column("projects", "expected_episode_count")
    op.drop_column("projects", "web_publish_intent")
