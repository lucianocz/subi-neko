"""Independent WEB configuration, mappings and lightweight receipts.

Revision ID: f3a4b5c6d7e8
Revises: c6d7e8f9a0b1
"""

import sqlalchemy as sa
from alembic import op

revision = "f3a4b5c6d7e8"
down_revision = "c6d7e8f9a0b1"
branch_labels = None
depends_on = None


def upgrade():
    for name in ("web_config_json", "web_publish_state", "web_identity_locked_at"):
        op.add_column("projects", sa.Column(name, sa.Text(), nullable=True))
    for name in ("web_config_version", "web_publish_attempt"):
        op.add_column(
            "projects",
            sa.Column(name, sa.Integer(), nullable=False, server_default="0"),
        )
    for name in (
        "web_tmdb_episode_id",
        "web_publisher_key",
        "web_episode_uuid",
        "web_mapping_json",
        "web_identity_locked_at",
        "web_published_sha256",
        "web_published_at",
    ):
        op.add_column("files", sa.Column(name, sa.Text(), nullable=True))
    for name, column in (
        ("tmdb", "web_tmdb_episode_id"),
        ("key", "web_publisher_key"),
        ("uuid", "web_episode_uuid"),
    ):
        op.create_index(
            f"uq_file_web_{name}", "files", ["project_id", column], unique=True
        )


def downgrade():
    for name in ("tmdb", "key", "uuid"):
        op.drop_index(f"uq_file_web_{name}", table_name="files")
    for name in (
        "web_tmdb_episode_id",
        "web_publisher_key",
        "web_episode_uuid",
        "web_mapping_json",
        "web_identity_locked_at",
        "web_published_sha256",
        "web_published_at",
    ):
        op.drop_column("files", name)
    for name in (
        "web_config_json",
        "web_publish_state",
        "web_identity_locked_at",
        "web_config_version",
        "web_publish_attempt",
    ):
        op.drop_column("projects", name)
