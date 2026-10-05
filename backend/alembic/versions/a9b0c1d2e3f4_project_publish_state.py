"""explicit project publishing: output revision + publish state

Replaces the automatic render/mux lifecycle (files flipped to MUXING, then
COMPLETED once every file was accepted) with a project-level, explicit Publish.

New projects columns:
- output_revision       NOT NULL DEFAULT 0   revision of data feeding the output
- published_revision    NULL                 revision the last successful publish captured
- publish_state         NULL                 publishing | published | failed (latest run)
- publish_error         NULL
- published_at          NULL
- publish_attempt       NOT NULL DEFAULT 0   id of the latest Publish click (job dedupe/fence)
- publish_target_revision NULL               revision the latest run captured

Backfill policy (deterministic, evaluated in this order):

1. A project is migrated to PUBLISHED iff it has at least one file and *every*
   file is `completed` — the old lifecycle's definition of "output exists for the
   whole project" (project.status alone is not trusted: the old orchestrator also
   set `completed` for projects with failed/paused files that were never muxed).
   It gets publish_state='published', published_revision = output_revision = 0 and
   published_at = the latest file completed_at (falling back to the project's
   updated_at).
2. Every other project keeps publish_state NULL / published_revision NULL, so it is
   never falsely PUBLISHED; once all its files are accepted it derives to READY.
3. Retired file statuses are converted so acceptance and publishing stay separate:
   files `muxing` and `completed` -> `accepted` (for ALL projects, including
   partially output ones: those files really were accepted). A `waiting` file with
   the legacy blocking_reason `mux_failed` -> `accepted`, reason cleared.
4. Queued/running legacy render_output_ass / mux_output_mkv jobs are cancelled —
   their handlers no longer exist and their file-level purpose is gone.

Downgrade restores the old shape as far as it can: files of a project whose output
is current become `completed`; files of a fully-accepted project whose output is not
current become `muxing` (the old code re-released them for muxing); then the columns
are dropped. Publish history/errors are not representable in the old schema.

Revision ID: a9b0c1d2e3f4
Revises: e7f8a9b0c1d2
Create Date: 2026-10-05
"""
from alembic import op
import sqlalchemy as sa

revision = 'a9b0c1d2e3f4'
down_revision = 'e7f8a9b0c1d2'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('projects') as batch:
        batch.add_column(sa.Column('output_revision', sa.Integer(), nullable=False, server_default='0'))
        batch.add_column(sa.Column('published_revision', sa.Integer(), nullable=True))
        batch.add_column(sa.Column('publish_state', sa.Text(), nullable=True))
        batch.add_column(sa.Column('publish_error', sa.Text(), nullable=True))
        batch.add_column(sa.Column('published_at', sa.Text(), nullable=True))
        batch.add_column(sa.Column('publish_attempt', sa.Integer(), nullable=False, server_default='0'))
        batch.add_column(sa.Column('publish_target_revision', sa.Integer(), nullable=True))

    # 1. Fully muxed projects -> PUBLISHED at revision 0 (before file statuses change).
    op.execute(
        """
        UPDATE projects
        SET publish_state = 'published',
            published_revision = 0,
            publish_target_revision = 0,
            published_at = COALESCE(
                (SELECT MAX(f.completed_at) FROM files f WHERE f.project_id = projects.id),
                projects.updated_at
            )
        WHERE EXISTS (SELECT 1 FROM files f WHERE f.project_id = projects.id)
          AND NOT EXISTS (
              SELECT 1 FROM files f
              WHERE f.project_id = projects.id AND f.status != 'completed'
          )
        """
    )

    # 3. Retire MUXING / COMPLETED file statuses.
    op.execute("UPDATE files SET status = 'accepted' WHERE status IN ('muxing', 'completed')")
    op.execute(
        "UPDATE files SET status = 'accepted', blocking_reason = NULL "
        "WHERE status = 'waiting' AND blocking_reason = 'mux_failed'"
    )

    # 4. Legacy per-file output jobs can no longer run.
    op.execute(
        "UPDATE jobs SET status = 'cancelled' "
        "WHERE job_type IN ('render_output_ass', 'mux_output_mkv') "
        "  AND status IN ('queued', 'running')"
    )


def downgrade() -> None:
    # Published (current) output -> files completed.
    op.execute(
        """
        UPDATE files SET status = 'completed'
        WHERE status = 'accepted'
          AND project_id IN (
              SELECT id FROM projects
              WHERE publish_state = 'published' AND published_revision = output_revision
          )
        """
    )
    # Fully accepted but not (currently) published -> the old code released
    # these for muxing.
    op.execute(
        """
        UPDATE files SET status = 'muxing'
        WHERE status = 'accepted'
          AND project_id IN (
              SELECT p.id FROM projects p
              WHERE NOT EXISTS (
                  SELECT 1 FROM files f WHERE f.project_id = p.id AND f.status != 'accepted'
              )
          )
        """
    )

    with op.batch_alter_table('projects') as batch:
        batch.drop_column('publish_target_revision')
        batch.drop_column('publish_attempt')
        batch.drop_column('published_at')
        batch.drop_column('publish_error')
        batch.drop_column('publish_state')
        batch.drop_column('published_revision')
        batch.drop_column('output_revision')
