"""Output revision tracking and the derived project output state.

A project's *output* (the translated ASS files and the muxed MKVs) is produced
only by an explicit Publish. Whether the published output is still current is
decided by two counters on ``Project``:

* ``output_revision``    — bumped by :func:`touch_output` whenever data that
                           feeds the final ASS/MKV changes;
* ``published_revision`` — the ``output_revision`` the last successful publish
                           captured when it started.

The user-facing state is *derived* from those plus the accepted-file count
(:func:`derive_output_state`); nothing stores a redundant READY flag.

How invalidation works (one mechanism, :func:`touch_output_sync`):

* ORM changes to the rendered tables (``SubtitleEvent``, ``SubtitleStyle``,
  ``Subtitle``, ``FileBranding``) are caught by a ``before_flush`` listener below, so every code
  path that edits them through a session — endpoints and pipeline handlers
  alike — invalidates the output without having to remember to.
* Core/bulk statements and options bypass the ORM, so those flows call
  :func:`touch_output` explicitly (re-extraction, full retranslate, the
  output-affecting options in ``app.db.options``).

Both routes run in the *same transaction* as the change and are deduplicated per
transaction, so one logical edit bumps the revision once. Only the monotonic
"it changed" signal matters, not the exact number.
"""
from __future__ import annotations

from enum import Enum
from typing import Iterable

from sqlalchemy import event, inspect, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session


class OutputState(str, Enum):
    NOT_READY = "not_ready"
    READY = "ready"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    FAILED = "failed"


class PublishState(str, Enum):
    """Persisted state of the *latest publish run* (Project.publish_state)."""
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    FAILED = "failed"


# FileStatus values that count as "accepted" for the output gate. MUXING and
# COMPLETED are legacy values of the retired automatic-output lifecycle; the
# migration converts them, but counting them keeps any stray row consistent.
ACCEPTED_FOR_OUTPUT = ("accepted", "muxing", "completed")


def derive_output_state(
    *,
    accepted_files: int,
    total_files: int,
    publish_state: str | None,
    output_revision: int,
    published_revision: int | None,
    publish_target_revision: int | None,
) -> OutputState:
    """The single place the user-facing output state is decided.

    Order matters:
    * a project with unaccepted files is NOT_READY regardless of old output;
    * an active run is PUBLISHING (the run publishes the revision it captured —
      a concurrent edit is surfaced as READY only once it finishes);
    * a failure only counts for the revision it targeted — after a later edit
      the obsolete failure must not mask the new READY state;
    * PUBLISHED only while the published revision is the current one.
    """
    if total_files <= 0 or accepted_files < total_files:
        return OutputState.NOT_READY
    if publish_state == PublishState.PUBLISHING.value:
        return OutputState.PUBLISHING
    if (
        publish_state == PublishState.FAILED.value
        and publish_target_revision == output_revision
    ):
        return OutputState.FAILED
    if published_revision is not None and published_revision == output_revision:
        return OutputState.PUBLISHED
    return OutputState.READY


# ---------------------------------------------------------------------------
# Revision bump
# ---------------------------------------------------------------------------

_TOUCHED_KEY = "_output_touched_projects"
_FILE_PROJECT_KEY = "_output_file_projects"


def touch_output_sync(session: Session, project_id: int) -> None:
    """Bump ``Project.output_revision`` (once per transaction per project).

    A bulk UPDATE in the caller's transaction: atomic with the change it
    describes. In-session ``Project`` instances are deliberately not
    synchronized (an async session could not lazy-reload them); callers that
    need the new value re-query.
    """
    from app.db.models import Project

    touched: set[int] = session.info.setdefault(_TOUCHED_KEY, set())
    if project_id in touched:
        return
    touched.add(project_id)
    session.execute(
        update(Project)
        .where(Project.id == project_id)
        .values(output_revision=Project.output_revision + 1)
        .execution_options(synchronize_session=False)
    )


async def touch_output(session: AsyncSession, project_id: int) -> None:
    """Async twin of :func:`touch_output_sync`; same transaction semantics."""
    await session.run_sync(lambda s: touch_output_sync(s, project_id))


def touch_all_outputs_sync(session: Session) -> None:
    """Invalidate every project (global output-affecting options)."""
    from app.db.models import Project

    session.execute(
        update(Project)
        .values(output_revision=Project.output_revision + 1)
        .execution_options(synchronize_session=False)
    )


# ---------------------------------------------------------------------------
# ORM listener
# ---------------------------------------------------------------------------

# Columns that feed build_ass(). Anything else (QA flags, translation_status,
# is_user_edited, confidence, timestamps…) does not change the output.
_EVENT_OUTPUT_COLUMNS = (
    "translated_text", "source_text", "start_ms", "end_ms", "layer", "style",
    "name", "margin_l", "margin_r", "margin_v", "effect", "event_type", "is_hidden",
)
_STYLE_OUTPUT_COLUMNS = (
    "style_name", "font_name", "font_size", "replacement_font_name",
    "replacement_font_size", "primary_colour", "secondary_colour",
    "outline_colour", "back_colour", "bold", "italic", "underline", "strikeout",
    "scale_x", "scale_y", "spacing", "angle", "border_style", "outline",
    "shadow", "alignment", "margin_l", "margin_r", "margin_v", "encoding",
)
_BRANDING_OUTPUT_COLUMNS = ("enabled", "template_filename", "start_offset_ms")
_SUBTITLE_OUTPUT_COLUMNS = (
    "script_type", "wrap_style", "play_res_x", "play_res_y",
    "scaled_border_and_shadow", "layout_res_x", "layout_res_y", "ycbcr_matrix",
    "kerning", "extra_script_info_json",
)


def _has_changes(obj: object, columns: Iterable[str]) -> bool:
    attrs = inspect(obj).attrs
    return any(attrs[c].history.has_changes() for c in columns)


def _projects_for_files(session: Session, file_ids: set[int]) -> set[int]:
    from app.db.models import File

    cache: dict[int, int | None] = session.info.setdefault(_FILE_PROJECT_KEY, {})
    missing = [fid for fid in file_ids if fid not in cache]
    if missing:
        for fid, pid in session.execute(
            select(File.id, File.project_id).where(File.id.in_(missing))
        ):
            cache[fid] = pid
        for fid in missing:
            cache.setdefault(fid, None)
    return {cache[fid] for fid in file_ids if cache.get(fid) is not None}


def _track_output_changes(session: Session, flush_context, instances) -> None:
    from app.db.models import FileBranding, Subtitle, SubtitleEvent, SubtitleStyle

    file_ids: set[int] = set()
    project_ids: set[int] = set()

    def consider(obj: object, *, is_new_or_deleted: bool) -> None:
        if isinstance(obj, SubtitleEvent):
            if is_new_or_deleted or _has_changes(obj, _EVENT_OUTPUT_COLUMNS):
                if obj.file_id is not None:
                    file_ids.add(obj.file_id)
        elif isinstance(obj, Subtitle):
            if is_new_or_deleted or _has_changes(obj, _SUBTITLE_OUTPUT_COLUMNS):
                if obj.file_id is not None:
                    file_ids.add(obj.file_id)
        elif isinstance(obj, FileBranding):
            if is_new_or_deleted or _has_changes(obj, _BRANDING_OUTPUT_COLUMNS):
                if obj.file_id is not None:
                    file_ids.add(obj.file_id)
        elif isinstance(obj, SubtitleStyle):
            if is_new_or_deleted or _has_changes(obj, _STYLE_OUTPUT_COLUMNS):
                if obj.project_id is not None:
                    project_ids.add(obj.project_id)

    for obj in session.new:
        consider(obj, is_new_or_deleted=True)
    for obj in session.deleted:
        consider(obj, is_new_or_deleted=True)
    for obj in session.dirty:
        consider(obj, is_new_or_deleted=False)

    if file_ids:
        project_ids |= _projects_for_files(session, file_ids)
    for pid in project_ids:
        touch_output_sync(session, pid)


def _reset_transaction_state(session: Session, transaction) -> None:
    if transaction.parent is None:  # root transaction ended (commit or rollback)
        session.info.pop(_TOUCHED_KEY, None)
        session.info.pop(_FILE_PROJECT_KEY, None)


event.listen(Session, "before_flush", _track_output_changes)
event.listen(Session, "after_transaction_end", _reset_transaction_state)
