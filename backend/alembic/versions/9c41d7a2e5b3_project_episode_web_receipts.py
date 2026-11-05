"""Durable website receipts on project_episodes (remote episode id, lock, confirmation).

Revision ID: 9c41d7a2e5b3
Revises: 8bb829bd511f
"""

import sqlalchemy as sa

from alembic import op

revision = "9c41d7a2e5b3"
down_revision = "8bb829bd511f"
branch_labels = None
depends_on = None

_COLUMNS = (
    "web_episode_uuid", "web_publisher_key", "web_identity_locked_at", "web_confirmed_at",
    "web_subtitle_sha256", "web_subtitle_published_at",
)


def upgrade():
    # Existing rows keep NULLs: nothing has been confirmed by a website yet.
    for name in _COLUMNS:
        op.add_column("project_episodes", sa.Column(name, sa.Text(), nullable=True))
    op.add_column("project_episodes", sa.Column("web_subtitle_revision", sa.Integer(), nullable=True))
    op.create_index(
        "uq_project_episodes_web_uuid", "project_episodes",
        ["project_id", "web_episode_uuid"], unique=True,
    )
    op.create_index(
        "uq_project_episodes_web_key", "project_episodes",
        ["project_id", "web_publisher_key"], unique=True,
    )
    op.add_column("projects", sa.Column("import_request_id", sa.Text(), nullable=True))
    op.create_index("uq_projects_import_request_id", "projects", ["import_request_id"], unique=True)


def downgrade():
    op.drop_index("uq_projects_import_request_id", table_name="projects")
    op.drop_column("projects", "import_request_id")
    op.drop_index("uq_project_episodes_web_key", table_name="project_episodes")
    op.drop_column("project_episodes", "web_subtitle_revision")
    op.drop_index("uq_project_episodes_web_uuid", table_name="project_episodes")
    for name in reversed(_COLUMNS):
        op.drop_column("project_episodes", name)
