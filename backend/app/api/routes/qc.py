"""Final QC API: whole-file event list, single-event detail/edit, hide/restore,
manual events and the authoritative translated-ASS preview.

Contract (see also ``app.db.qc_state`` / ``app.subs.ass_rendering.build_ass``):

* Every endpoint requires ``qc_available`` (no incomplete chunk) and answers
  **409** otherwise. QC never depends on, nor changes, ``FileStatus``.
* Timing is stored as integer ms and quantized to 10 ms (the precision the
  ASS serializer ships) when written.
* Any real QC edit locks the event (``is_locked=1``) so translate/repair/polish/
  review can't overwrite it; only a full retranslate resets the lock.
* ``restore-ai`` mirrors the legacy revert but also locks the event (see its
  docstring). Resolving a QA item is NOT done here: the editor's existing
  ``POST .../qa-issues/{id}/resolve`` is reused (review metadata only — it
  never touches text/timing, so it never bumps ``output_revision``).
* Output invalidation is *not* done here: the ``before_flush`` listener in
  ``app.db.output_state`` bumps ``Project.output_revision`` for every
  ``SubtitleEvent`` insert / output-column change made through the session
  (translated_text, start/end, is_hidden, new manual rows), once per
  transaction. A no-op request changes nothing and therefore bumps nothing.
* QA items are NOT re-run after an edit: ``issue_count`` /
  ``max_issue_severity`` / detail issues describe the pipeline's last review
  and may be stale for edited lines (accepted for v1).
* ``qc/fonts`` (+ ``qc/fonts/{id}``) is QC-gated like everything here: it only
  makes sense for the translated ASS of a file that is in the QC workflow.
  Font problems never fail the manifest: it returns what resolved plus
  ``missing_families`` and, when MKV attachments could not be read,
  ``attachment_error`` (configured fonts are still returned).
* Speaker identity is only the textual ASS actor name (``SubtitleEvent.name``);
  the detail endpoint resolves character/gender by that name.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from app.api.routes.projects import (
    QaIssueOut,
    _broadcast_project_updated,
    _severity_rank,
    _speaker_character_map,
    _sync_tm_after_edit,
)
from app.api.routes.media import font_response
from app.core.database import AsyncSessionLocal
from app.core.media_paths import SourcePathError, resolve_source_path
from app.db import options as options_store
from app.db.models import (
    File,
    Project,
    ProjectWatchedWord,
    QaItem,
    Subtitle,
    SubtitleEvent,
    SubtitleStyle,
    WatchedWordType,
    file_subtitle_styles,
)
from app.db.qc_state import is_qc_available
from app.metadata.base import CharacterGender
from app.subs.ass_rendering import HEADER_NOTICE, build_ass
from app.subs.font_attachments import FontAttachmentError, get_attachment_registry
from app.subs.font_registry import FontFace, FontRegistry, get_configured_registry
from app.subs.qc_fonts import collect_required_families, resolve_manifest
from app.subs.readability import compute_cps
from app.subs.watched_words import matching_watched_words

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/projects", tags=["qc"])

TIMING_QUANTUM_MS = 10


def quantize_ms(value: int) -> int:
    """Round to the nearest 10 ms (half up) — what the centisecond ASS ships."""
    return (value + TIMING_QUANTUM_MS // 2) // TIMING_QUANTUM_MS * TIMING_QUANTUM_MS


# ---------------------------------------------------------------------------
# DTOs
# ---------------------------------------------------------------------------

class QcEventOut(BaseModel):
    """Compact row: the QC page keeps thousands of these in memory."""
    id: int
    line_index: int
    start_ms: int
    end_ms: int
    style: str
    speaker: str | None
    content_type: str
    event_type: str
    translated_text: str | None
    source_text: str
    cps: float | None
    is_hidden: bool
    is_manual: bool
    is_user_edited: bool
    is_locked: bool
    # Unresolved QA items attached to the event (as of the last pipeline review).
    issue_count: int
    max_issue_severity: str | None
    # (watched-word definition, text) matches; detail endpoint says which words.
    watched_count: int


class QcEventListOut(BaseModel):
    file_id: int
    filename: str
    qc_available: bool
    # Bumps on every output-affecting mutation: compare to detect a stale preview.
    output_revision: int
    event_count: int          # rows in this response
    total_count: int          # rows in the file, hidden included
    hidden_count: int
    cps_limit: float          # HARD limit (red; QA high_cps)
    soft_cps_limit: float     # SOFT limit (orange; display only, never QA)
    # Style names linked to the file (for the manual-event style selector).
    styles: list[str]
    events: list[QcEventOut]


class QcWatchedMatchOut(BaseModel):
    word: str
    word_type: Literal["original", "translated"]


class QcEventDetailOut(QcEventOut):
    original_ai_translated_text: str | None
    is_approved: bool
    # Immutable source timing (reference only; start_ms/end_ms are the QC timing).
    original_start_ms: int
    original_end_ms: int
    layer: int
    name: str | None
    character_name: str | None
    character_gender: CharacterGender | None
    speaker_gender: CharacterGender | None
    issues: list[QaIssueOut]
    watched_matches: list[QcWatchedMatchOut]


class QcEventPatchIn(BaseModel):
    """Only these fields are editable; anything else is a 422."""
    model_config = ConfigDict(extra="forbid")

    translated_text: str | None = None
    start_ms: int | None = Field(default=None, ge=0)
    end_ms: int | None = Field(default=None, ge=0)


class QcEventCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)
    translated_text: str = Field(min_length=1)
    style: str = Field(min_length=1)
    speaker: str | None = Field(default=None, max_length=200)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _qc_file(session, project_id: int, file_id: int) -> File:
    file = await session.get(File, file_id)
    if file is None or file.project_id != project_id:
        raise HTTPException(status_code=404, detail="File not found")
    if not await is_qc_available(session, file_id):
        raise HTTPException(
            status_code=409,
            detail="Final QC is not available: the file still has incomplete chunks",
        )
    return file


async def _watched_words(session, project_id: int) -> tuple[list[str], list[str]]:
    rows = (await session.execute(
        select(ProjectWatchedWord.word, ProjectWatchedWord.word_type)
        .where(ProjectWatchedWord.project_id == project_id)
    )).all()
    return (
        [w for w, t in rows if t == WatchedWordType.ORIGINAL.value],
        [w for w, t in rows if t == WatchedWordType.TRANSLATED.value],
    )


def _qc_out(
    row,
    issue: tuple[int, str | None] = (0, None),
    watched: tuple[list[str], list[str]] = ([], []),
) -> QcEventOut:
    original_words, translated_words = watched
    watched_count = 0
    if original_words:
        watched_count += len(matching_watched_words(row.source_text, original_words))
    if translated_words:
        watched_count += len(matching_watched_words(row.translated_text, translated_words))
    speaker = row.name.strip() if row.name and row.name.strip() else None
    return QcEventOut(
        id=row.id,
        line_index=row.line_index,
        start_ms=row.start_ms,
        end_ms=row.end_ms,
        style=row.style,
        speaker=speaker,
        content_type=row.content_type,
        event_type=row.event_type,
        translated_text=row.translated_text,
        source_text=row.source_text,
        cps=compute_cps(row.translated_text, row.start_ms, row.end_ms),
        is_hidden=bool(row.is_hidden),
        is_manual=bool(row.is_manual),
        is_user_edited=bool(row.is_user_edited),
        is_locked=bool(row.is_locked),
        issue_count=issue[0],
        max_issue_severity=issue[1],
        watched_count=watched_count,
    )


def _issue_summary(rows) -> dict[int, tuple[int, str | None]]:
    """(event_id, severity, count) rows of UNRESOLVED items -> per-event
    (count, most severe severity)."""
    best: dict[int, tuple[int, int, str]] = {}  # id -> (count, rank, severity)
    for event_id, severity, count in rows:
        rank = _severity_rank(severity)
        prev = best.get(event_id)
        if prev is None:
            best[event_id] = (count, rank, severity)
        elif rank < prev[1]:
            best[event_id] = (prev[0] + count, rank, severity)
        else:
            best[event_id] = (prev[0] + count, prev[1], prev[2])
    return {eid: (count, severity) for eid, (count, _rank, severity) in best.items()}


async def _event_out(session, project_id: int, event_id: int) -> QcEventOut:
    row = await session.get(SubtitleEvent, event_id)
    await session.refresh(row)
    issues = (await session.execute(
        select(QaItem.subtitle_event_id, QaItem.severity, func.count())
        .where(QaItem.subtitle_event_id == event_id, QaItem.is_resolved == 0)
        .group_by(QaItem.subtitle_event_id, QaItem.severity)
    )).all()
    watched = await _watched_words(session, project_id)
    return _qc_out(row, _issue_summary(issues).get(event_id, (0, None)), watched)


async def _stamp_revision(session, project_id: int, response: Response) -> None:
    """``X-Output-Revision`` on mutation responses: lets the QC page tell its own
    edit (revision advanced by exactly one) from an external one."""
    revision = await session.scalar(
        select(Project.output_revision).where(Project.id == project_id)) or 0
    response.headers["X-Output-Revision"] = str(revision)


async def _load_event(session, file_id: int, event_id: int) -> SubtitleEvent:
    event = await session.get(SubtitleEvent, event_id)
    if event is None or event.file_id != file_id:
        raise HTTPException(status_code=404, detail="Subtitle event not found")
    return event


def _validated_timing(start_ms: int, end_ms: int) -> tuple[int, int]:
    if not 0 <= start_ms < end_ms:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid timing: need 0 <= start_ms < end_ms (got {start_ms}..{end_ms}"
                   f" after {TIMING_QUANTUM_MS} ms quantization)",
        )
    return start_ms, end_ms


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/{project_id}/files/{file_id}/qc/events", response_model=QcEventListOut)
async def list_qc_events(project_id: int, file_id: int, show_hidden: bool = False):
    """All events of the file (no pagination), ordered by (start_ms, line_index).

    Hidden events are excluded unless ``show_hidden=true``. One event query +
    one grouped QA query + one watched-word query: no per-row lookups, and no
    QA detail / full entities in the payload.
    """
    async with AsyncSessionLocal() as session:
        file = await _qc_file(session, project_id, file_id)
        revision = await session.scalar(
            select(Project.output_revision).where(Project.id == project_id)) or 0

        cols = (
            SubtitleEvent.id, SubtitleEvent.line_index, SubtitleEvent.start_ms,
            SubtitleEvent.end_ms, SubtitleEvent.style, SubtitleEvent.name,
            SubtitleEvent.content_type, SubtitleEvent.event_type,
            SubtitleEvent.translated_text, SubtitleEvent.source_text,
            SubtitleEvent.is_hidden, SubtitleEvent.is_manual,
            SubtitleEvent.is_user_edited, SubtitleEvent.is_locked,
        )
        stmt = select(*cols).where(SubtitleEvent.file_id == file_id)
        if not show_hidden:
            stmt = stmt.where(SubtitleEvent.is_hidden == 0)
        rows = (await session.execute(
            stmt.order_by(SubtitleEvent.start_ms, SubtitleEvent.line_index))).all()

        counts = (await session.execute(
            select(func.count(), func.coalesce(func.sum(SubtitleEvent.is_hidden), 0))
            .where(SubtitleEvent.file_id == file_id)
        )).one()
        issue_rows = (await session.execute(
            select(QaItem.subtitle_event_id, QaItem.severity, func.count())
            .where(QaItem.file_id == file_id, QaItem.subtitle_event_id.is_not(None),
                   QaItem.is_resolved == 0)
            .group_by(QaItem.subtitle_event_id, QaItem.severity)
        )).all()
        issues = _issue_summary(issue_rows)
        watched = await _watched_words(session, project_id)
        opts = await options_store.asnapshot()
        styles = list((await session.scalars(
            select(SubtitleStyle.style_name)
            .join(file_subtitle_styles, file_subtitle_styles.c.subtitle_style_id == SubtitleStyle.id)
            .where(file_subtitle_styles.c.file_id == file_id)
            .order_by(SubtitleStyle.id)
        )).all())

        events = [_qc_out(r, issues.get(r.id, (0, None)), watched) for r in rows]
        return QcEventListOut(
            file_id=file_id,
            filename=file.filename,
            qc_available=True,
            output_revision=revision,
            event_count=len(events),
            total_count=int(counts[0]),
            hidden_count=int(counts[1]),
            cps_limit=opts.cps_limit,
            soft_cps_limit=opts.soft_cps_limit,
            styles=list(dict.fromkeys(styles)),
            events=events,
        )


@router.get("/{project_id}/files/{file_id}/qc/events/{event_id}", response_model=QcEventDetailOut)
async def get_qc_event(project_id: int, file_id: int, event_id: int):
    """Selected-event detail: full QA items, watched-word matches, speaker info."""
    async with AsyncSessionLocal() as session:
        await _qc_file(session, project_id, file_id)
        event = await session.get(
            SubtitleEvent, event_id, options=[selectinload(SubtitleEvent.qa_items)])
        if event is None or event.file_id != file_id:
            raise HTTPException(status_code=404, detail="Subtitle event not found")

        items = sorted(
            event.qa_items,
            key=lambda i: (bool(i.is_resolved), _severity_rank(i.severity), i.created_at, i.id),
        )
        unresolved = [i for i in items if not i.is_resolved]
        summary = (
            len(unresolved),
            min((i.severity for i in unresolved), key=_severity_rank) if unresolved else None,
        )
        original_words, translated_words = await _watched_words(session, project_id)
        matches = [
            QcWatchedMatchOut(word=w, word_type="original")
            for w in matching_watched_words(event.source_text, original_words)
        ] + [
            QcWatchedMatchOut(word=w, word_type="translated")
            for w in matching_watched_words(event.translated_text, translated_words)
        ]

        speaker = event.name.strip() if event.name and event.name.strip() else None
        character_name = character_gender = speaker_gender = None
        if speaker:
            mapped = (await _speaker_character_map(session, project_id)).get(speaker.lower())
            if mapped:
                character_name, character_gender, speaker_gender = mapped

        base = _qc_out(event, summary, (original_words, translated_words))
        return QcEventDetailOut(
            **base.model_dump(),
            original_ai_translated_text=event.original_ai_translated_text,
            is_approved=bool(event.is_approved),
            original_start_ms=event.original_start_ms,
            original_end_ms=event.original_end_ms,
            layer=event.layer,
            name=event.name,
            character_name=character_name,
            character_gender=character_gender,
            speaker_gender=speaker_gender,
            issues=[QaIssueOut.model_validate(i) for i in items],
            watched_matches=matches,
        )


@router.patch("/{project_id}/files/{file_id}/qc/events/{event_id}", response_model=QcEventOut)
async def patch_qc_event(
    project_id: int, file_id: int, event_id: int, body: QcEventPatchIn, response: Response,
):
    """Edit translated text and/or timing (blur-save friendly).

    Omitted fields are preserved exactly. Supplied timing is quantized to 10 ms
    and the resulting ``0 <= start < end`` is enforced. A request that changes
    nothing is a no-op: no lock, no revision bump. Does not touch file/project
    status and does not re-run QA.
    """
    fields = body.model_fields_set
    if not fields:
        raise HTTPException(status_code=422, detail="No editable fields supplied")
    if any(getattr(body, name) is None for name in fields):
        raise HTTPException(status_code=422, detail="Supplied fields cannot be null")

    async with AsyncSessionLocal() as session:
        file = await _qc_file(session, project_id, file_id)
        event = await _load_event(session, file_id, event_id)

        start, end = _validated_timing(
            quantize_ms(body.start_ms) if "start_ms" in fields else event.start_ms,
            quantize_ms(body.end_ms) if "end_ms" in fields else event.end_ms,
        )
        text_changed = "translated_text" in fields and body.translated_text != event.translated_text
        changed = text_changed or start != event.start_ms or end != event.end_ms
        is_manual = bool(event.is_manual)
        file_status = file.status
        if changed:
            if text_changed:
                event.translated_text = body.translated_text
                event.is_user_edited = 0 if body.translated_text == event.original_ai_translated_text else 1
            event.start_ms = start
            event.end_ms = end
            event.is_locked = 1
            event.updated_at = datetime.utcnow().isoformat()
            await session.commit()
        out = await _event_out(session, project_id, event_id)
        await _stamp_revision(session, project_id, response)

    if changed:
        if text_changed and not is_manual:
            await _sync_tm_after_edit(project_id, event_id, file_status)
        await _broadcast_project_updated(project_id)
    return out


@router.post("/{project_id}/files/{file_id}/qc/events/{event_id}/restore-ai", response_model=QcEventOut)
async def restore_ai_qc_event(project_id: int, file_id: int, event_id: int, response: Response):
    """Put the original AI translation back (the legacy editor's *revert*).

    Same data semantics as ``POST .../subtitle-events/{id}/revert`` —
    ``translated_text`` := ``original_ai_translated_text``, ``is_user_edited``
    cleared, 409 when there is no baseline — with ONE deliberate QC difference:
    the event ends up ``is_locked=1``. An explicit QC interaction stays
    protected, so a later translate/polish/review run can never overwrite the
    restored line (the legacy revert leaves the lock untouched and relies on
    ``is_user_edited``, which restore clears). Timing is left as it is. Output
    invalidation is the usual ``before_flush`` listener: the revision bumps
    only when ``translated_text`` actually changed.
    """
    async with AsyncSessionLocal() as session:
        file = await _qc_file(session, project_id, file_id)
        event = await _load_event(session, file_id, event_id)
        if event.original_ai_translated_text is None:
            raise HTTPException(status_code=409, detail="No original AI translation stored")
        text_changed = event.translated_text != event.original_ai_translated_text
        is_manual = bool(event.is_manual)
        file_status = file.status
        event.translated_text = event.original_ai_translated_text
        event.is_user_edited = 0
        event.is_locked = 1
        event.updated_at = datetime.utcnow().isoformat()
        await session.commit()
        out = await _event_out(session, project_id, event_id)
        await _stamp_revision(session, project_id, response)

    if text_changed and not is_manual:
        await _sync_tm_after_edit(project_id, event_id, file_status)
    if text_changed:
        await _broadcast_project_updated(project_id)
    return out


async def _set_hidden(
    project_id: int, file_id: int, event_id: int, hidden: bool, response: Response,
) -> QcEventOut:
    async with AsyncSessionLocal() as session:
        await _qc_file(session, project_id, file_id)
        event = await _load_event(session, file_id, event_id)
        changed = bool(event.is_hidden) != hidden
        if changed:
            event.is_hidden = int(hidden)
            event.updated_at = datetime.utcnow().isoformat()
            await session.commit()
        out = await _event_out(session, project_id, event_id)
        await _stamp_revision(session, project_id, response)
    if changed:
        await _broadcast_project_updated(project_id)
    return out


@router.post("/{project_id}/files/{file_id}/qc/events/{event_id}/hide", response_model=QcEventOut)
async def hide_qc_event(project_id: int, file_id: int, event_id: int, response: Response):
    """Exclude the event from the translated ASS (idempotent; the row is kept and
    the source ASS still contains it). Does not lock the event."""
    return await _set_hidden(project_id, file_id, event_id, True, response)


@router.post("/{project_id}/files/{file_id}/qc/events/{event_id}/restore", response_model=QcEventOut)
async def restore_qc_event(project_id: int, file_id: int, event_id: int, response: Response):
    return await _set_hidden(project_id, file_id, event_id, False, response)


@router.post("/{project_id}/files/{file_id}/qc/events", response_model=QcEventOut, status_code=201)
async def create_qc_event(project_id: int, file_id: int, body: QcEventCreateIn, response: Response):
    """Create a manual (QC-only) event, e.g. a fansub sign.

    The frontend keeps blank drafts locally and POSTs only complete rows, so no
    incomplete row is ever persisted. ``line_index`` is ``max + 1`` (internal
    provenance/stable id only; the QC list orders by time).
    """
    start, end = _validated_timing(quantize_ms(body.start_ms), quantize_ms(body.end_ms))
    now = datetime.utcnow().isoformat()
    async with AsyncSessionLocal() as session:
        await _qc_file(session, project_id, file_id)
        has_subtitle = await session.scalar(
            select(Subtitle.id).where(Subtitle.file_id == file_id))
        if has_subtitle is None:
            raise HTTPException(status_code=409, detail="Subtitles have not been extracted yet")
        style_ok = await session.scalar(
            select(SubtitleStyle.id)
            .join(file_subtitle_styles, file_subtitle_styles.c.subtitle_style_id == SubtitleStyle.id)
            .where(file_subtitle_styles.c.file_id == file_id,
                   SubtitleStyle.style_name == body.style)
            .limit(1)
        )
        if style_ok is None:
            raise HTTPException(status_code=422, detail=f"Unknown style '{body.style}' for this file")

        max_index = await session.scalar(
            select(func.max(SubtitleEvent.line_index)).where(SubtitleEvent.file_id == file_id))
        speaker = body.speaker.strip() if body.speaker and body.speaker.strip() else None
        event = SubtitleEvent(
            file_id=file_id,
            line_index=(max_index if max_index is not None else -1) + 1,
            event_type="dialogue",
            content_type="sign",
            content_type_reason="manual_qc",
            layer=0,
            start_ms=start,
            end_ms=end,
            original_start_ms=start,
            original_end_ms=end,
            style=body.style,
            name=speaker,
            source_text="",
            translated_text=body.translated_text,
            translation_status="translated",
            is_locked=1,
            is_manual=1,
            is_hidden=0,
            created_at=now,
            updated_at=now,
        )
        session.add(event)
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            raise HTTPException(status_code=409, detail="Concurrent event creation; retry")
        out = await _event_out(session, project_id, event.id)
        await _stamp_revision(session, project_id, response)

    await _broadcast_project_updated(project_id)
    return out


@router.get("/{project_id}/files/{file_id}/qc/preview.ass")
async def qc_preview_ass(project_id: int, file_id: int, request: Request):
    """Translated ASS for the current DB state, built by the SAME ``build_ass``
    call as the final output / translated download (hidden excluded, manual
    included, current styles and font-replacement option).

    Caching: ``ETag`` is derived from ``Project.output_revision`` (which every
    output-affecting mutation, including styles and the font option, bumps) and
    ``Cache-Control: no-cache`` forces revalidation, so a save is never served
    stale yet an unchanged preview answers 304. The revision is read *before*
    the events, so a concurrent edit can only make the ETag older than the body
    (a harmless extra refetch), never newer.
    """
    async with AsyncSessionLocal() as session:
        await _qc_file(session, project_id, file_id)
        revision = await session.scalar(
            select(Project.output_revision).where(Project.id == project_id)) or 0
        etag = f'"qc-{project_id}-{file_id}-{revision}"'
        headers = {
            "ETag": etag,
            "Cache-Control": "no-cache, must-revalidate",
            "X-Output-Revision": str(revision),
        }
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers=headers)

        subtitle = await session.scalar(select(Subtitle).where(Subtitle.file_id == file_id))
        if subtitle is None:
            raise HTTPException(status_code=409, detail="Subtitles have not been extracted yet")
        styles = list((await session.scalars(
            select(SubtitleStyle)
            .join(file_subtitle_styles, file_subtitle_styles.c.subtitle_style_id == SubtitleStyle.id)
            .where(file_subtitle_styles.c.file_id == file_id)
            .order_by(SubtitleStyle.id)
        )).all())
        events = list((await session.scalars(
            select(SubtitleEvent)
            .where(SubtitleEvent.file_id == file_id)
            .order_by(SubtitleEvent.line_index)
        )).all())
        opts = await options_store.asnapshot()
        subs = build_ass(
            subtitle, styles, events,
            text_variant="translated",
            title=opts.target_lang_name or "",
            use_font_replacements=opts.replace_incompatible_fonts,
        )

    return Response(
        content=subs.to_string("ass", header_notice=HEADER_NOTICE).encode("utf-8"),
        media_type="text/x-ssa",
        headers=headers,
    )


# ---------------------------------------------------------------------------
# Fonts for the (future) JASSUB preview
# ---------------------------------------------------------------------------

class QcFontOut(BaseModel):
    id: str
    family_names: list[str]
    url: str
    source: Literal["attachment", "configured"]
    filename: str
    media_type: str
    bold: bool
    italic: bool


class QcFontsOut(BaseModel):
    file_id: int
    replace_incompatible_fonts: bool
    # Families the translated ASS requests (style fonts + explicit \fn overrides).
    required_families: list[str]
    fonts: list[QcFontOut]
    missing_families: list[str]
    attachment_error: str | None = None


async def _load_attachment_registry(
    project: Project, file: File,
) -> tuple[FontRegistry | None, str | None]:
    try:
        source = resolve_source_path(project, file)
    except SourcePathError:
        return None, "Source media file not found"
    try:
        return await asyncio.to_thread(
            get_attachment_registry, source, project_id=project.id, file_id=file.id), None
    except FontAttachmentError as exc:
        logger.warning("Font attachments unavailable for file id=%s: %s", file.id, exc)
        return None, str(exc)
    except Exception:
        logger.exception("Unexpected font attachment failure for file id=%s", file.id)
        return None, "Could not read font attachments"


def _font_out(face: FontFace, base_url: str, api_prefix: str) -> QcFontOut:
    url = (f"{base_url}/{face.id}" if face.source == "attachment"
           else f"{api_prefix}/fonts/configured/{face.id}")
    return QcFontOut(
        id=face.id, family_names=list(face.families), url=url, source=face.source,
        filename=face.filename, media_type=face.media_type, bold=face.bold, italic=face.italic)


@router.get("/{project_id}/files/{file_id}/qc/fonts", response_model=QcFontsOut)
async def get_qc_fonts(project_id: int, file_id: int, request: Request):
    """Fonts JASSUB needs for this file: all MKV font attachments plus configured
    fonts for the families the translated ASS requests (same effective-font rule
    as ``build_ass``, honouring ``REPLACE_INCOMPATIBLE_FONTS``). URLs are
    relative to the current API prefix; no server paths are exposed."""
    async with AsyncSessionLocal() as session:
        file = await _qc_file(session, project_id, file_id)
        project = await session.get(Project, project_id)
        styles = list((await session.scalars(
            select(SubtitleStyle)
            .join(file_subtitle_styles, file_subtitle_styles.c.subtitle_style_id == SubtitleStyle.id)
            .where(file_subtitle_styles.c.file_id == file_id)
        )).all())
        event_rows = [
            (r.style, r.translated_text, bool(r.is_hidden)) for r in (await session.execute(
                select(SubtitleEvent.style, SubtitleEvent.translated_text, SubtitleEvent.is_hidden)
                .where(SubtitleEvent.file_id == file_id)
            )).all()
        ]
        replace = (await options_store.asnapshot()).replace_incompatible_fonts

    required = collect_required_families(styles, event_rows, use_font_replacements=replace)
    attachments, error = await _load_attachment_registry(project, file)
    resolved = resolve_manifest(required, attachments, get_configured_registry())

    path = request.url.path.rstrip("/")
    api_prefix = path.split("/projects/", 1)[0]
    return QcFontsOut(
        file_id=file_id,
        replace_incompatible_fonts=replace,
        required_families=required,
        fonts=[_font_out(f, path, api_prefix) for f in resolved.fonts],
        missing_families=resolved.missing_families,
        attachment_error=error,
    )


@router.api_route(
    "/{project_id}/files/{file_id}/qc/fonts/{font_id}", methods=["GET", "HEAD"], response_model=None)
async def get_qc_attachment_font(project_id: int, file_id: int, font_id: str) -> FileResponse:
    """Bytes of one font attached to this file's MKV (id from the manifest)."""
    async with AsyncSessionLocal() as session:
        file = await _qc_file(session, project_id, file_id)
        project = await session.get(Project, project_id)
    registry, error = await _load_attachment_registry(project, file)
    if registry is None:
        raise HTTPException(status_code=502, detail=error or "Font attachments unavailable")
    face = registry.get(font_id)
    if face is None or not face.path.is_file():
        raise HTTPException(status_code=404, detail="Font not found")
    return font_response(face)
