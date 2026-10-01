import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import case, delete, func, select
from sqlalchemy.orm import selectinload

from app.core.database import AsyncSessionLocal
from app.db import options as options_store
from app.db.models import (
    File,
    FileAnalysis,
    FileBlockingReason,
    FileQualityMetric,
    FileStatus,
    JobRecord,
    JobStatus,
    Project,
    ProjectAddressPair,
    ProjectCharacter,
    ProjectCharacterStyle,
    ProjectGlossaryTerm,
    ProjectSpeaker,
    ProjectStyleBible,
    ProjectWatchedWord,
    ProjectStatus,
    QaItem,
    SubtitleChunk,
    Subtitle,
    SubtitleEvent,
    SubtitleStyle,
    TranslationMemoryEntry,
    WatchedWordType,
)
from app.jobs.manager import job_manager
from app.metadata.base import CharacterGender
from app.orchestrator.context_status import compute_context_status
from app.orchestrator.file_orchestrator import finalize_accepted_file, orchestrate_file
from app.orchestrator.project_orchestrator import (
    orchestrate_project,
    pick_style_bible_sample_file_id,
)
from app.subs.ass_rendering import build_ass
from app.ws.connection_manager import connection_manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/projects", tags=["projects"])


class ProjectOut(BaseModel):
    id: int
    name: str
    source_directory: str
    anime_provider: str
    anime_external_id: str
    speaker_mapping_status: str
    status: str
    is_paused: bool
    context_approved_at: str | None = None
    created_at: str
    updated_at: str

    model_config = {"from_attributes": True}


class FileOut(BaseModel):
    id: int
    project_id: int
    filename: str
    relative_path: str
    status: str
    blocking_reason: str | None
    translation_requested_at: str | None = None
    detected_subtitle_format: str | None
    subtitle_track_index: int | None
    retry_count: int
    last_error_code: str | None
    last_error_message: str | None
    created_at: str
    updated_at: str
    completed_at: str | None
    chunks_total: int | None = None
    chunks_done: int | None = None
    qa_issues: int = 0
    qa_errors: int = 0
    qa_warnings: int = 0

    model_config = {"from_attributes": True}


class CharacterOut(BaseModel):
    id: int
    project_id: int
    external_id: str | None
    name: str
    role: str | None
    gender: CharacterGender | None
    social_position: str | None
    aliases: str | None
    note: str | None
    created_at: str
    updated_at: str
    speaker_ids: list[int]

    model_config = {"from_attributes": True}


class CharacterUpdateIn(BaseModel):
    gender: CharacterGender | None = None
    social_position: str | None = None
    note: str | None = None
    speaker_ids: list[int] = []


class SpeakerOut(BaseModel):
    id: int
    project_id: int
    name: str
    gender: CharacterGender | None
    character_id: int | None
    character_name: str | None
    match_confidence: float | None
    match_origin: str | None
    match_rationale: str | None
    line_count: int
    sample_lines: list[str]
    is_extra: bool
    content_tag: Literal["sign", "karaoke", "song"] | None
    created_at: str
    updated_at: str

    model_config = {"from_attributes": True}


class SpeakerUpdateIn(BaseModel):
    gender: CharacterGender | None = None
    character_id: int | None = None
    is_extra: bool | None = None
    content_tag: Literal["sign", "karaoke", "song"] | None = None


class SpeakerUpdateOut(BaseModel):
    speaker: SpeakerOut
    affected_chunk_count: int


class WatchedWordOut(BaseModel):
    id: int
    project_id: int
    word: str
    word_type: Literal["original", "translated"]
    created_at: str
    updated_at: str

    model_config = {"from_attributes": True}


class WatchedWordCreateIn(BaseModel):
    word: str = Field(min_length=1, max_length=200)
    word_type: Literal["original", "translated"]


class QaIssueOut(BaseModel):
    id: int
    severity: str
    qa_type: str
    message: str
    details_json: str | None
    is_resolved: bool
    resolution_note: str | None
    created_at: str

    model_config = {"from_attributes": True}


class QaIssueSummaryOut(BaseModel):
    qa_type: str
    severity: str
    count: int


class SubtitleEventEditorOut(BaseModel):
    id: int
    file_id: int
    line_index: int
    event_type: str
    source_text: str
    translated_text: str | None
    original_ai_translated_text: str | None
    speaker_name: str | None
    speaker_gender: CharacterGender | None
    character_name: str | None
    character_gender: CharacterGender | None
    is_user_edited: bool
    is_locked: bool
    is_approved: bool
    issues: list[QaIssueOut]

    model_config = {"from_attributes": True}


class SubtitleEventUpdateIn(BaseModel):
    translated_text: str | None = None


@router.get("", response_model=list[ProjectOut])
async def list_projects():
    async with AsyncSessionLocal() as session:
        rows = await session.scalars(select(Project).order_by(Project.name))
        return list(rows.all())


@router.get("/{project_id}", response_model=ProjectOut)
async def get_project(project_id: int):
    async with AsyncSessionLocal() as session:
        project = await session.get(Project, project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found")
        return project


@router.post("/{project_id}/pause", response_model=ProjectOut)
async def pause_project(project_id: int):
    async with AsyncSessionLocal() as session:
        project = await session.get(Project, project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found")
        if project.status in (ProjectStatus.COMPLETED.value, ProjectStatus.FAILED.value):
            raise HTTPException(status_code=409, detail=f"Cannot pause a project with status '{project.status}'")
        if not project.is_paused:
            project.is_paused = True
            project.updated_at = datetime.utcnow().isoformat()
            await session.commit()
        await session.refresh(project)

    await job_manager.cancel_queued_project_jobs(project_id)
    return project


@router.post("/{project_id}/resume", response_model=ProjectOut)
async def resume_project(project_id: int):
    async with AsyncSessionLocal() as session:
        project = await session.get(Project, project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found")
        if not project.is_paused:
            return project
        project.is_paused = False
        project.updated_at = datetime.utcnow().isoformat()
        await session.commit()
        await session.refresh(project)

    await orchestrate_project(project_id, job_manager.enqueue)
    return project


async def _broadcast_project_updated(project_id: int) -> None:
    try:
        await connection_manager.broadcast("project_updated", {"project_id": project_id})
    except Exception:
        logger.exception("project_updated broadcast failed for project_id=%d", project_id)


@router.get("/{project_id}/context-status")
async def get_context_status(project_id: int):
    """Derived readiness of the translation context (gate 1): per-component
    progress/failures, speakers needing attention, and overall confidence."""
    status = await compute_context_status(project_id)
    if status is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return status


@router.post("/{project_id}/approve-context", response_model=ProjectOut)
async def approve_context(project_id: int):
    """Explicit user approval of the translation context. Unblocks the
    per-file Translate actions; idempotent when already approved."""
    status = await compute_context_status(project_id)
    if status is None:
        raise HTTPException(status_code=404, detail="Project not found")

    if status["state"] != "approved":
        if status["state"] != "ready_for_review":
            failed = [
                c["key"] for c in status["components"] if c["status"] == "failed"
            ]
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "Translation context is not ready for approval",
                    "state": status["state"],
                    "failed_components": failed,
                },
            )
        now = datetime.utcnow().isoformat()
        async with AsyncSessionLocal() as session:
            project = await session.get(Project, project_id)
            if project is None:
                raise HTTPException(status_code=404, detail="Project not found")
            project.context_approved_at = now
            project.status = ProjectStatus.PROCESSING.value
            project.updated_at = now
            await session.commit()
        await _broadcast_project_updated(project_id)
        await orchestrate_project(project_id, job_manager.enqueue)

    async with AsyncSessionLocal() as session:
        project = await session.get(Project, project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found")
        return ProjectOut.model_validate(project)


_CONTEXT_COMPONENT_JOBS = {
    "speaker_aggregation": "aggregate_speakers",
    "character_mapping": "infer_character_mapping",
    "style_bible": "generate_style_bible",
}


class ContextRetryIn(BaseModel):
    component: Literal["speaker_aggregation", "character_mapping", "style_bible"]


@router.post("/{project_id}/context/retry")
async def retry_context_component(project_id: int, body: ContextRetryIn):
    """Re-run a permanently failed context-generation job. Re-enqueueing with
    the canonical dedupe key resets the FAILED JobRecord (manager stale-reset),
    so no extra retry machinery is needed."""
    job_type = _CONTEXT_COMPONENT_JOBS[body.component]
    dedupe_key = f"{job_type}:{project_id}"

    async with AsyncSessionLocal() as session:
        project = await session.get(Project, project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found")
        record = await session.scalar(
            select(JobRecord).where(JobRecord.dedupe_key == dedupe_key)
        )

    if record is None or record.status != JobStatus.FAILED.value:
        raise HTTPException(
            status_code=409,
            detail=f"Context component '{body.component}' has no failed job to retry",
        )

    payload: dict = {"project_id": project_id}
    if job_type == "generate_style_bible":
        sample_file_id = await pick_style_bible_sample_file_id(project_id)
        if sample_file_id is None:
            raise HTTPException(
                status_code=409,
                detail="No file with extracted subtitles to sample the style bible from",
            )
        payload["sample_file_id"] = sample_file_id

    await job_manager.enqueue(
        job_type=job_type,
        project_id=project_id,
        payload=payload,
        dedupe_key=dedupe_key,
    )
    await _broadcast_project_updated(project_id)
    return {"status": "requeued", "component": body.component}


@router.post("/{project_id}/files/{file_id}/translate", response_model=FileOut)
async def translate_file(project_id: int, file_id: int):
    """Per-file Translate action (gate 2): starts the file's automatic
    pipeline, or retries a failed script analysis. Idempotent for files
    already started."""
    now = datetime.utcnow().isoformat()

    async with AsyncSessionLocal() as session:
        file = await session.get(File, file_id)
        if file is None or file.project_id != project_id:
            raise HTTPException(status_code=404, detail="File not found")
        project = await session.get(Project, project_id)
        if project is None or project.context_approved_at is None:
            raise HTTPException(
                status_code=409,
                detail="Approve the translation context before translating files",
            )

        retry_analysis = (
            file.status == FileStatus.WAITING.value
            and file.blocking_reason == FileBlockingReason.ANALYSIS_FAILED.value
        )
        if file.status == FileStatus.READY.value or retry_analysis:
            if file.translation_requested_at is None:
                file.translation_requested_at = now
            if retry_analysis:
                file.status = FileStatus.READY.value
                file.blocking_reason = None
                file.last_error_code = None
                file.last_error_message = None
            file.updated_at = now
            await session.commit()
            await session.refresh(file)
        elif file.translation_requested_at is None:
            raise HTTPException(
                status_code=409,
                detail=f"File cannot be translated from status '{file.status}'",
            )
        # else: already started — idempotent no-op

        out = FileOut.model_validate(file)

    if retry_analysis:
        # Enqueue with the canonical key so the manager's stale-reset clears
        # the FAILED JobRecord before the orchestrator's perma-fail guard
        # would park the file again.
        await job_manager.enqueue(
            job_type="analyze_script",
            project_id=project_id,
            payload={"file_id": file_id},
            file_id=file_id,
            dedupe_key=f"analyze_script:{file_id}",
        )

    await _broadcast_project_updated(project_id)
    await orchestrate_file(file_id, job_manager.enqueue)
    return out


_ORIGINAL_DOWNLOAD_STATUSES = {
    FileStatus.READY.value,
    FileStatus.PROCESSING.value,
    FileStatus.WAITING.value,
    FileStatus.REVIEW_REQUIRED.value,
    FileStatus.ACCEPTED.value,
    FileStatus.MUXING.value,
    FileStatus.COMPLETED.value,
}
_TRANSLATION_DOWNLOAD_STATUSES = {
    FileStatus.REVIEW_REQUIRED.value,
    FileStatus.ACCEPTED.value,
    FileStatus.MUXING.value,
    FileStatus.COMPLETED.value,
}
_RETRANSLATABLE_STATUSES = {
    FileStatus.REVIEW_REQUIRED.value,
    FileStatus.ACCEPTED.value,
    FileStatus.COMPLETED.value,
}
_RETRANSLATION_CHUNK_JOB_TYPES = {
    "translate_chunk",
    "validate_chunk",
    "repair_chunk",
    "polish_chunk",
    "review_chunk_final",
    "audit_chunk_final",
}


@router.get("/{project_id}/files/{file_id}/subtitles/{variant}")
async def download_file_subtitles(
    project_id: int,
    file_id: int,
    variant: Literal["original", "translated"],
):
    """Download the extracted source or the reviewed translation as ASS."""
    async with AsyncSessionLocal() as session:
        file = await session.get(File, file_id)
        if file is None or file.project_id != project_id:
            raise HTTPException(status_code=404, detail="File not found")

        allowed_statuses = (
            _ORIGINAL_DOWNLOAD_STATUSES
            if variant == "original"
            else _TRANSLATION_DOWNLOAD_STATUSES
        )
        if file.status not in allowed_statuses:
            availability = "awaiting translation" if variant == "original" else "review required"
            raise HTTPException(
                status_code=409,
                detail=f"{variant.capitalize()} subtitles are available from '{availability}' onwards",
            )

        subtitle = await session.scalar(select(Subtitle).where(Subtitle.file_id == file_id))
        if subtitle is None:
            raise HTTPException(status_code=409, detail="Subtitles have not been extracted yet")
        styles = list((await session.scalars(
            select(SubtitleStyle).where(SubtitleStyle.file_id == file_id)
        )).all())
        events = list((await session.scalars(
            select(SubtitleEvent)
            .where(SubtitleEvent.file_id == file_id)
            .order_by(SubtitleEvent.line_index)
        )).all())

        title = None
        if variant == "translated":
            title = (await options_store.asnapshot()).target_lang_name or ""
        subs = build_ass(
            subtitle,
            styles,
            events,
            text_variant=variant,
            title=title,
        )

    suffix = "original" if variant == "original" else "translated"
    download_name = f"{Path(file.filename).stem}.{suffix}.ass"
    encoded_name = quote(download_name)
    return Response(
        content=subs.to_string("ass").encode("utf-8"),
        media_type="text/x-ssa",
        headers={
            "Content-Disposition": (
                f"attachment; filename=\"subtitles.{suffix}.ass\"; "
                f"filename*=UTF-8''{encoded_name}"
            ),
        },
    )


@router.post("/{project_id}/files/{file_id}/retranslate", response_model=FileOut)
async def retranslate_file(project_id: int, file_id: int):
    """Clear a finished translation and restart its complete chunk pipeline.

    LLM call rows are intentionally retained, so cost/token metrics remain
    cumulative across translation attempts. The quality snapshot is removed
    and recomputed after the new output completes from all retained calls.
    """
    now = datetime.utcnow().isoformat()
    async with AsyncSessionLocal() as session:
        file = await session.get(File, file_id)
        if file is None or file.project_id != project_id:
            raise HTTPException(status_code=404, detail="File not found")
        if file.status not in _RETRANSLATABLE_STATUSES:
            raise HTTPException(
                status_code=409,
                detail=f"File cannot be retranslated from status '{file.status}'",
            )

        events = list((await session.scalars(
            select(SubtitleEvent).where(SubtitleEvent.file_id == file_id)
        )).all())
        chunk_count = await session.scalar(
            select(func.count())
            .select_from(SubtitleChunk)
            .where(SubtitleChunk.file_id == file_id)
        )
        if not chunk_count:
            raise HTTPException(status_code=409, detail="File has no translation chunks")

        for event in events:
            event.translated_text = None
            event.original_ai_translated_text = None
            event.translation_status = "skipped" if event.content_type == "other" else "pending"
            event.translation_confidence = None
            event.is_user_edited = 0
            event.is_locked = 0
            event.is_approved = 0
            event.updated_at = now

        # Re-plan from scratch rather than reusing the old chunk boundaries.
        # The READY orchestrator sees zero chunks and reruns
        # plan_translation_chunks before any translation job is scheduled.
        await session.execute(delete(SubtitleChunk).where(SubtitleChunk.file_id == file_id))
        await session.execute(delete(FileAnalysis).where(FileAnalysis.file_id == file_id))
        await session.execute(delete(QaItem).where(QaItem.file_id == file_id))
        await session.execute(delete(FileQualityMetric).where(FileQualityMetric.file_id == file_id))
        await session.execute(
            delete(TranslationMemoryEntry).where(
                TranslationMemoryEntry.project_id == project_id,
                TranslationMemoryEntry.src_file_id == file_id,
            )
        )

        # These jobs describe artifacts/snapshots invalidated by the reset.
        # Marking them cancelled lets the manager reuse their canonical keys.
        invalidated_keys = {
            f"plan_translation_chunks:{file_id}",
            f"analyze_script:{file_id}",
            f"render_output_ass:{file_id}",
            f"mux_output_mkv:{file_id}",
            f"compute_file_metrics:{file_id}",
            f"update_style_bible:{project_id}:{file_id}",
        }
        records = list((await session.scalars(
            select(JobRecord).where(
                JobRecord.file_id == file_id,
                (
                    JobRecord.dedupe_key.in_(invalidated_keys)
                    | JobRecord.job_type.in_(_RETRANSLATION_CHUNK_JOB_TYPES)
                ),
            )
        )).all())
        for record in records:
            record.status = JobStatus.CANCELLED.value
            record.finished_at = now
            record.updated_at = now

        file.status = FileStatus.READY.value
        file.translation_requested_at = now
        file.blocking_reason = None
        file.retry_count = 0
        file.last_error_code = None
        file.last_error_message = None
        file.completed_at = None
        file.updated_at = now

        project = await session.get(Project, project_id)
        if project is not None:
            project.status = ProjectStatus.PROCESSING.value
            project.updated_at = now

        await session.commit()
        await session.refresh(file)
        out = FileOut.model_validate(file)

    await _broadcast_project_updated(project_id)
    await orchestrate_file(file_id, job_manager.enqueue)
    return out


@router.delete("/{project_id}", status_code=204)
async def delete_project(project_id: int):
    async with AsyncSessionLocal() as session:
        project = await session.get(Project, project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found")

    # Cancel all active jobs before deletion so workers don't race against CASCADE
    await job_manager.cancel_project_jobs(project_id)

    async with AsyncSessionLocal() as session:
        project = await session.get(Project, project_id)
        if project is not None:
            await session.delete(project)
            await session.commit()


@router.get("/{project_id}/watched-words", response_model=list[WatchedWordOut])
async def list_project_watched_words(project_id: int):
    async with AsyncSessionLocal() as session:
        project = await session.get(Project, project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found")
        rows = await session.scalars(
            select(ProjectWatchedWord)
            .where(ProjectWatchedWord.project_id == project_id)
            .order_by(ProjectWatchedWord.word_type, func.lower(ProjectWatchedWord.word))
        )
        return list(rows.all())


@router.post("/{project_id}/watched-words", response_model=WatchedWordOut, status_code=201)
async def create_project_watched_word(project_id: int, body: WatchedWordCreateIn):
    word = body.word.strip()
    if not word:
        raise HTTPException(status_code=422, detail="Word must not be blank")
    if body.word_type not in {WatchedWordType.ORIGINAL.value, WatchedWordType.TRANSLATED.value}:
        raise HTTPException(status_code=422, detail="Invalid watched word type")

    async with AsyncSessionLocal() as session:
        project = await session.get(Project, project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found")

        duplicate = await session.scalar(
            select(ProjectWatchedWord)
            .where(ProjectWatchedWord.project_id == project_id)
            .where(ProjectWatchedWord.word_type == body.word_type)
            .where(func.lower(ProjectWatchedWord.word) == word.lower())
        )
        if duplicate is not None:
            raise HTTPException(status_code=409, detail="Watched word already exists for this type")

        watched_word = ProjectWatchedWord(
            project_id=project_id,
            word=word,
            word_type=body.word_type,
        )
        session.add(watched_word)
        await session.commit()
        await session.refresh(watched_word)
        return watched_word


@router.delete("/{project_id}/watched-words/{watched_word_id}", status_code=204)
async def delete_project_watched_word(project_id: int, watched_word_id: int):
    async with AsyncSessionLocal() as session:
        watched_word = await session.get(ProjectWatchedWord, watched_word_id)
        if watched_word is None or watched_word.project_id != project_id:
            raise HTTPException(status_code=404, detail="Watched word not found")
        await session.delete(watched_word)
        await session.commit()


@router.get("/{project_id}/files", response_model=list[FileOut])
async def list_project_files(project_id: int):
    async with AsyncSessionLocal() as session:
        project = await session.get(Project, project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found")
        rows = await session.scalars(
            select(File)
            .where(File.project_id == project_id)
            .order_by(File.filename)
        )
        files = list(rows.all())

        # Fetch chunk counts for all files in one query
        file_ids = [f.id for f in files]
        chunk_rows = (await session.execute(
            select(
                SubtitleChunk.file_id,
                func.count().label("total"),
                func.sum(case((SubtitleChunk.status != "pending", 1), else_=0)).label("done"),
            )
            .where(SubtitleChunk.file_id.in_(file_ids))
            .group_by(SubtitleChunk.file_id)
        )).all() if file_ids else []

        chunk_map = {row.file_id: (int(row.done or 0), int(row.total)) for row in chunk_rows}

        qa_rows = (await session.execute(
            select(
                QaItem.file_id,
                QaItem.severity,
                func.count().label("cnt"),
            )
            .where(QaItem.file_id.in_(file_ids))
            .where(QaItem.is_resolved == 0)
            .group_by(QaItem.file_id, QaItem.severity)
        )).all() if file_ids else []
        qa_map: dict[int, dict[str, int]] = {}
        for row in qa_rows:
            bucket = "errors" if _severity_rank(row.severity) == 0 else "warnings"
            qa_map.setdefault(row.file_id, {"errors": 0, "warnings": 0})[bucket] += int(row.cnt or 0)

        result = []
        for f in files:
            out = FileOut.model_validate(f)
            if f.id in chunk_map:
                out.chunks_done, out.chunks_total = chunk_map[f.id]
            qa_counts = qa_map.get(f.id, {"errors": 0, "warnings": 0})
            out.qa_errors = qa_counts["errors"]
            out.qa_warnings = qa_counts["warnings"]
            out.qa_issues = out.qa_errors + out.qa_warnings
            result.append(out)
        return result


# ---------------------------------------------------------------------------
# Glossary & style guide
# ---------------------------------------------------------------------------

class GlossaryTermApiOut(BaseModel):
    id: int
    source_term: str
    target_term: str
    category: str
    gender: str | None
    vocative: str | None
    note: str | None
    origin: str
    locked: bool
    is_active: bool

    model_config = {"from_attributes": True}


class GlossaryTermCreateIn(BaseModel):
    source_term: str
    target_term: str
    category: str = "other"
    gender: str | None = None
    vocative: str | None = None
    note: str | None = None


class GlossaryTermUpdateIn(BaseModel):
    target_term: str | None = None
    category: str | None = None
    gender: str | None = None
    vocative: str | None = None
    note: str | None = None
    is_active: bool | None = None


class CharacterVoiceApiOut(BaseModel):
    id: int
    character_id: int
    character_name: str
    voice_note: str | None
    register: str | None
    origin: str
    locked: bool


class CharacterVoiceUpdateIn(BaseModel):
    voice_note: str | None = None
    register: str | None = None


class AddressPairApiOut(BaseModel):
    id: int
    speaker_name: str
    addressee_name: str
    mode: str
    origin: str
    locked: bool

    model_config = {"from_attributes": True}


class AddressPairUpdateIn(BaseModel):
    mode: Literal["tykani", "vykani", "mixed"]


class StyleGuideOut(BaseModel):
    version: int | None
    tone_summary: str | None
    register_notes: str | None
    honorific_policy: str | None
    character_voices: list[CharacterVoiceApiOut]
    address_pairs: list[AddressPairApiOut]


class StyleBibleUpdateIn(BaseModel):
    tone_summary: str | None = None
    register_notes: str | None = None
    honorific_policy: str | None = None


_GLOSSARY_CATEGORIES = {"name", "place", "technique", "item", "honorific", "catchphrase", "other"}


async def _get_project_or_404(session, project_id: int) -> Project:
    project = await session.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


@router.get("/{project_id}/glossary", response_model=list[GlossaryTermApiOut])
async def list_glossary_terms(project_id: int):
    async with AsyncSessionLocal() as session:
        await _get_project_or_404(session, project_id)
        rows = await session.scalars(
            select(ProjectGlossaryTerm)
            .where(ProjectGlossaryTerm.project_id == project_id)
            .order_by(ProjectGlossaryTerm.category, ProjectGlossaryTerm.source_term)
        )
        return list(rows.all())


@router.post("/{project_id}/glossary", response_model=GlossaryTermApiOut, status_code=201)
async def create_glossary_term(project_id: int, body: GlossaryTermCreateIn):
    source = body.source_term.strip()
    target = body.target_term.strip()
    if not source or not target:
        raise HTTPException(status_code=422, detail="source_term and target_term are required")
    category = body.category if body.category in _GLOSSARY_CATEGORIES else "other"
    now = datetime.utcnow().isoformat()

    async with AsyncSessionLocal() as session:
        await _get_project_or_404(session, project_id)
        existing = await session.scalar(
            select(ProjectGlossaryTerm)
            .where(ProjectGlossaryTerm.project_id == project_id)
            .where(func.lower(ProjectGlossaryTerm.source_term) == source.lower())
            .where(ProjectGlossaryTerm.category == category)
        )
        if existing is not None:
            raise HTTPException(status_code=409, detail="Term already exists in this category")
        term = ProjectGlossaryTerm(
            project_id=project_id,
            source_term=source,
            target_term=target,
            category=category,
            gender=body.gender,
            vocative=body.vocative,
            note=body.note,
            origin="manual",
            locked=1,
            is_active=1,
            created_at=now,
            updated_at=now,
        )
        session.add(term)
        await session.commit()
        await session.refresh(term)
        return term


@router.put("/{project_id}/glossary/{term_id}", response_model=GlossaryTermApiOut)
async def update_glossary_term(project_id: int, term_id: int, body: GlossaryTermUpdateIn):
    now = datetime.utcnow().isoformat()
    async with AsyncSessionLocal() as session:
        term = await session.get(ProjectGlossaryTerm, term_id)
        if term is None or term.project_id != project_id:
            raise HTTPException(status_code=404, detail="Glossary term not found")
        if body.target_term is not None:
            if not body.target_term.strip():
                raise HTTPException(status_code=422, detail="target_term cannot be empty")
            term.target_term = body.target_term.strip()
        if body.category is not None and body.category in _GLOSSARY_CATEGORIES:
            term.category = body.category
        if body.gender is not None:
            term.gender = body.gender or None
        if body.vocative is not None:
            term.vocative = body.vocative or None
        if body.note is not None:
            term.note = body.note or None
        if body.is_active is not None:
            term.is_active = 1 if body.is_active else 0
        term.origin = "manual"
        term.locked = 1
        term.updated_at = now
        await session.commit()
        await session.refresh(term)
        return term


@router.delete("/{project_id}/glossary/{term_id}", status_code=204)
async def delete_glossary_term(project_id: int, term_id: int):
    async with AsyncSessionLocal() as session:
        term = await session.get(ProjectGlossaryTerm, term_id)
        if term is None or term.project_id != project_id:
            raise HTTPException(status_code=404, detail="Glossary term not found")
        await session.delete(term)
        await session.commit()


@router.get("/{project_id}/style-guide", response_model=StyleGuideOut)
async def get_style_guide(project_id: int):
    async with AsyncSessionLocal() as session:
        await _get_project_or_404(session, project_id)

        bible = await session.scalar(
            select(ProjectStyleBible)
            .where(ProjectStyleBible.project_id == project_id)
            .order_by(ProjectStyleBible.version.desc())
            .limit(1)
        )

        voice_rows = (await session.execute(
            select(ProjectCharacterStyle, ProjectCharacter.name)
            .join(ProjectCharacter, ProjectCharacter.id == ProjectCharacterStyle.project_character_id)
            .where(ProjectCharacter.project_id == project_id)
            .order_by(ProjectCharacter.name)
        )).all()

        pairs = (await session.scalars(
            select(ProjectAddressPair)
            .where(ProjectAddressPair.project_id == project_id)
            .order_by(ProjectAddressPair.speaker_name, ProjectAddressPair.addressee_name)
        )).all()

        return StyleGuideOut(
            version=bible.version if bible else None,
            tone_summary=bible.tone_summary if bible else None,
            register_notes=bible.register_notes if bible else None,
            honorific_policy=bible.honorific_policy if bible else None,
            character_voices=[
                CharacterVoiceApiOut(
                    id=style.id,
                    character_id=style.project_character_id,
                    character_name=name,
                    voice_note=style.voice_note,
                    register=style.register,
                    origin=style.origin,
                    locked=bool(style.locked),
                )
                for style, name in voice_rows
            ],
            address_pairs=[AddressPairApiOut.model_validate(p) for p in pairs],
        )


@router.put("/{project_id}/style-bible", response_model=StyleGuideOut)
async def update_style_bible_text(project_id: int, body: StyleBibleUpdateIn):
    now = datetime.utcnow().isoformat()
    async with AsyncSessionLocal() as session:
        await _get_project_or_404(session, project_id)
        bible = await session.scalar(
            select(ProjectStyleBible)
            .where(ProjectStyleBible.project_id == project_id)
            .order_by(ProjectStyleBible.version.desc())
            .limit(1)
        )
        if bible is None:
            bible = ProjectStyleBible(
                project_id=project_id, version=1,
                created_at=now, updated_at=now,
            )
            session.add(bible)
        if body.tone_summary is not None:
            bible.tone_summary = body.tone_summary or None
        if body.register_notes is not None:
            bible.register_notes = body.register_notes or None
        if body.honorific_policy is not None:
            bible.honorific_policy = body.honorific_policy or None
        bible.is_user_edited = 1
        bible.updated_at = now
        await session.commit()
    return await get_style_guide(project_id)


@router.put("/{project_id}/address-pairs/{pair_id}", response_model=AddressPairApiOut)
async def update_address_pair(project_id: int, pair_id: int, body: AddressPairUpdateIn):
    now = datetime.utcnow().isoformat()
    async with AsyncSessionLocal() as session:
        pair = await session.get(ProjectAddressPair, pair_id)
        if pair is None or pair.project_id != project_id:
            raise HTTPException(status_code=404, detail="Address pair not found")
        pair.mode = body.mode
        pair.origin = "manual"
        pair.locked = 1
        pair.updated_at = now
        await session.commit()
        await session.refresh(pair)
        return pair


@router.put("/{project_id}/character-styles/{style_id}", response_model=CharacterVoiceApiOut)
async def update_character_style(project_id: int, style_id: int, body: CharacterVoiceUpdateIn):
    now = datetime.utcnow().isoformat()
    async with AsyncSessionLocal() as session:
        row = (await session.execute(
            select(ProjectCharacterStyle, ProjectCharacter.name)
            .join(ProjectCharacter, ProjectCharacter.id == ProjectCharacterStyle.project_character_id)
            .where(ProjectCharacterStyle.id == style_id)
            .where(ProjectCharacter.project_id == project_id)
        )).first()
        if row is None:
            raise HTTPException(status_code=404, detail="Character style not found")
        style, name = row
        if body.voice_note is not None:
            style.voice_note = body.voice_note or None
        if body.register is not None:
            style.register = body.register or None
        style.origin = "manual"
        style.locked = 1
        style.updated_at = now
        await session.commit()
        return CharacterVoiceApiOut(
            id=style.id,
            character_id=style.project_character_id,
            character_name=name,
            voice_note=style.voice_note,
            register=style.register,
            origin=style.origin,
            locked=bool(style.locked),
        )


class AcceptReviewIn(BaseModel):
    resolve_warnings: bool = False


@router.post("/{project_id}/files/{file_id}/accept-review", response_model=FileOut)
async def accept_file_review(project_id: int, file_id: int, body: AcceptReviewIn | None = None):
    """Accept a reviewed file. Only unresolved BLOCKER items stand in the
    way; warnings/info ride along and can optionally be bulk-resolved with
    resolve_warnings=true. Output starts once every project file is accepted."""
    now = datetime.utcnow().isoformat()
    resolve_warnings = bool(body and body.resolve_warnings)

    async with AsyncSessionLocal() as session:
        file = await session.get(File, file_id)
        if file is None or file.project_id != project_id:
            raise HTTPException(status_code=404, detail="File not found")
        if file.status != FileStatus.REVIEW_REQUIRED.value:
            raise HTTPException(
                status_code=409,
                detail=f"File is not in review_required state (current: {file.status})",
            )

        unresolved_blockers = await session.scalar(
            select(func.count())
            .select_from(QaItem)
            .where(QaItem.file_id == file_id, QaItem.is_resolved == 0,
                   QaItem.severity == "blocker")
        )
        if unresolved_blockers > 0:
            raise HTTPException(
                status_code=409,
                detail="Cannot accept review while unresolved blocker issues remain",
            )

        if resolve_warnings:
            remaining = (await session.scalars(
                select(QaItem).where(QaItem.file_id == file_id, QaItem.is_resolved == 0)
            )).all()
            for item in remaining:
                item.is_resolved = 1
                item.resolution_note = "accepted_with_file"
                item.resolved_at = now
            await session.commit()

    await finalize_accepted_file(file_id, project_id, job_manager.enqueue)

    async with AsyncSessionLocal() as session:
        file = await session.get(File, file_id)
        out = FileOut.model_validate(file)

    await orchestrate_file(file_id, job_manager.enqueue)
    return out


class BulkResolveIn(BaseModel):
    ids: list[int] | None = None
    severity: str | None = None          # blocker|warning|info
    qa_type: str | None = None
    file_ids: list[int] | None = None
    resolution_note: str | None = None


class BulkResolveOut(BaseModel):
    resolved: int


@router.post("/{project_id}/qa-items/bulk-resolve", response_model=BulkResolveOut)
async def bulk_resolve_qa_items(project_id: int, body: BulkResolveIn):
    if not any([body.ids, body.severity, body.qa_type, body.file_ids]):
        raise HTTPException(status_code=422, detail="At least one filter is required")

    now = datetime.utcnow().isoformat()
    async with AsyncSessionLocal() as session:
        query = (
            select(QaItem)
            .join(File, QaItem.file_id == File.id)
            .where(File.project_id == project_id, QaItem.is_resolved == 0)
        )
        if body.ids:
            query = query.where(QaItem.id.in_(body.ids))
        if body.severity:
            query = query.where(QaItem.severity == body.severity)
        if body.qa_type:
            query = query.where(QaItem.qa_type == body.qa_type)
        if body.file_ids:
            query = query.where(QaItem.file_id.in_(body.file_ids))

        items = (await session.scalars(query)).all()
        for item in items:
            item.is_resolved = 1
            item.resolution_note = body.resolution_note or "bulk_resolved"
            item.resolved_at = now
        await session.commit()

    await orchestrate_project(project_id, job_manager.enqueue)
    return BulkResolveOut(resolved=len(items))


class ReviewQueueItemOut(BaseModel):
    id: int
    file_id: int
    filename: str
    severity: str
    qa_type: str
    message: str
    event_id: int | None
    line_index: int | None
    speaker: str | None
    source_text: str | None
    translated_text: str | None
    translation_confidence: float | None
    is_user_edited: bool
    created_at: str


class ReviewQueueOut(BaseModel):
    total: int
    items: list[ReviewQueueItemOut]


_SEVERITY_ORDER = case(
    (QaItem.severity == "blocker", 0),
    (QaItem.severity == "warning", 1),
    else_=2,
)


@router.get("/{project_id}/review-queue", response_model=ReviewQueueOut)
async def get_review_queue(
    project_id: int,
    severity: str | None = None,
    limit: int = 200,
    offset: int = 0,
):
    """All unresolved QA items across the project, most severe (and least
    confident) first — the flat triage feed for the review queue UI."""
    limit = max(1, min(limit, 500))
    async with AsyncSessionLocal() as session:
        base = (
            select(QaItem)
            .join(File, QaItem.file_id == File.id)
            .where(File.project_id == project_id, QaItem.is_resolved == 0)
        )
        if severity:
            base = base.where(QaItem.severity == severity)

        total = await session.scalar(
            select(func.count()).select_from(base.subquery())
        ) or 0

        rows = (await session.execute(
            base.add_columns(File.filename)
            .outerjoin(SubtitleEvent, QaItem.subtitle_event_id == SubtitleEvent.id)
            .add_columns(
                SubtitleEvent.line_index,
                SubtitleEvent.name,
                SubtitleEvent.source_text,
                SubtitleEvent.translated_text,
                SubtitleEvent.translation_confidence,
                SubtitleEvent.is_user_edited,
            )
            .order_by(
                _SEVERITY_ORDER,
                func.coalesce(SubtitleEvent.translation_confidence, 1.0),
                QaItem.file_id,
                SubtitleEvent.line_index,
                QaItem.id,
            )
            .limit(limit)
            .offset(offset)
        )).all()

        items = [
            ReviewQueueItemOut(
                id=item.id,
                file_id=item.file_id,
                filename=filename,
                severity=item.severity,
                qa_type=item.qa_type,
                message=item.message,
                event_id=item.subtitle_event_id,
                line_index=line_index,
                speaker=name,
                source_text=source_text,
                translated_text=translated_text,
                translation_confidence=confidence,
                is_user_edited=bool(is_user_edited),
                created_at=item.created_at,
            )
            for (item, filename, line_index, name, source_text,
                 translated_text, confidence, is_user_edited) in rows
        ]
        return ReviewQueueOut(total=int(total), items=items)


class ChunkJobOut(BaseModel):
    id: int
    job_type: str
    status: str
    attempt_count: int
    result: dict | None = None
    error_code: str | None
    error_message: str | None
    scheduled_at: str
    started_at: str | None
    finished_at: str | None
    updated_at: str

    model_config = {"from_attributes": True}


class ChunkOut(BaseModel):
    id: int
    chunk_index: int
    translate_from_line: int
    translate_to_line: int
    content_type: str = "dialogue"
    status: str
    model: str | None
    llm_review_needed: bool
    retry_count: int = 0
    repair_attempt_count: int = 0
    last_error_code: str | None = None
    last_error_message: str | None = None
    failed_job_type: str | None = None
    qa_errors: int = 0
    qa_warnings: int = 0
    jobs: dict[str, ChunkJobOut] = Field(default_factory=dict)

    model_config = {"from_attributes": True}


CHUNK_PIPELINE_JOB_TYPES = {
    "translate_chunk",
    "validate_chunk",
    "repair_chunk",
    "polish_chunk",
    "review_chunk_final",
    "audit_chunk_final",
}


def _job_chunk_index(job: JobRecord) -> int | None:
    try:
        payload = json.loads(job.payload_json or "{}")
    except json.JSONDecodeError:
        return None
    value = payload.get("chunk_index")
    return value if isinstance(value, int) else None


def _job_result(job: JobRecord) -> dict | None:
    if not job.result_json:
        return None
    try:
        value = json.loads(job.result_json)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


@router.get("/{project_id}/files/{file_id}/chunks", response_model=list[ChunkOut])
async def list_file_chunks(project_id: int, file_id: int):
    async with AsyncSessionLocal() as session:
        file = await session.get(File, file_id)
        if file is None or file.project_id != project_id:
            raise HTTPException(status_code=404, detail="File not found")

        chunks = list((await session.scalars(
            select(SubtitleChunk)
            .where(SubtitleChunk.file_id == file_id)
            .order_by(SubtitleChunk.chunk_index)
        )).all())

        # Aggregate unresolved QA items by line_index, content_type and
        # severity in one query
        qa_rows = (await session.execute(
            select(
                SubtitleEvent.line_index,
                SubtitleEvent.content_type,
                QaItem.severity,
                func.count().label("cnt"),
            )
            .join(QaItem, QaItem.subtitle_event_id == SubtitleEvent.id)
            .where(SubtitleEvent.file_id == file_id)
            .where(QaItem.is_resolved == 0)
            .group_by(SubtitleEvent.line_index, SubtitleEvent.content_type,
                      QaItem.severity)
        )).all()

        # Build (line_index, content_type) → {severity: count} map. Chunk line
        # ranges of different content types overlap (a sign chunk's from/to
        # spans its sparse member lines), so counts must be matched per type.
        qa_by_line: dict[tuple[int, str], dict[str, int]] = {}
        for row in qa_rows:
            key = (row.line_index, row.content_type or "dialogue")
            qa_by_line.setdefault(key, {})[row.severity] = int(row.cnt)

        job_rows = list((await session.scalars(
            select(JobRecord)
            .where(JobRecord.file_id == file_id)
            .where(JobRecord.job_type.in_(CHUNK_PIPELINE_JOB_TYPES))
            .order_by(JobRecord.updated_at)
        )).all())

        jobs_by_chunk: dict[int, dict[str, ChunkJobOut]] = {}
        for job in job_rows:
            chunk_index = _job_chunk_index(job)
            if chunk_index is None:
                continue
            jobs_by_chunk.setdefault(chunk_index, {})[job.job_type] = ChunkJobOut(
                id=job.id,
                job_type=job.job_type,
                status=job.status,
                attempt_count=job.attempt_count,
                result=_job_result(job),
                error_code=job.error_code,
                error_message=job.error_message,
                scheduled_at=job.scheduled_at,
                started_at=job.started_at,
                finished_at=job.finished_at,
                updated_at=job.updated_at,
            )

        result = []
        for chunk in chunks:
            chunk_content_type = chunk.content_type or "dialogue"
            errors = warnings = 0
            for (line_idx, event_content_type), sevs in qa_by_line.items():
                if event_content_type != chunk_content_type:
                    continue
                if chunk.translate_from_line <= line_idx <= chunk.translate_to_line:
                    errors += sevs.get("blocker", 0)
                    warnings += sevs.get("warning", 0) + sevs.get("info", 0)
            result.append(ChunkOut(
                id=chunk.id,
                chunk_index=chunk.chunk_index,
                translate_from_line=chunk.translate_from_line,
                translate_to_line=chunk.translate_to_line,
                content_type=chunk.content_type or "dialogue",
                status=chunk.status,
                model=chunk.model,
                llm_review_needed=bool(chunk.llm_review_needed),
                retry_count=chunk.retry_count or 0,
                repair_attempt_count=chunk.repair_attempt_count or 0,
                last_error_code=chunk.last_error_code,
                last_error_message=chunk.last_error_message,
                failed_job_type=chunk.failed_job_type,
                qa_errors=errors,
                qa_warnings=warnings,
                jobs=jobs_by_chunk.get(chunk.chunk_index, {}),
            ))
        return result


# Status to restore when retrying a job_failed chunk, keyed by failed_job_type.
_RETRY_STATUS_BY_JOB_TYPE: dict[str, str] = {
    "translate_chunk":     "pending",
    "validate_chunk":      "translated",
    "repair_chunk":        "validate_trans_failed",
    "polish_chunk":        "validated",
    "review_chunk_final":  "polished",
    "audit_chunk_final":   "final_reviewed",
}


@router.post("/{project_id}/files/{file_id}/chunks/{chunk_index}/retry", response_model=ChunkOut)
async def retry_chunk(project_id: int, file_id: int, chunk_index: int):
    """Retry a chunk that is in job_failed or validate_repair_failed state."""
    now = datetime.utcnow().isoformat()

    async with AsyncSessionLocal() as session:
        file = await session.get(File, file_id)
        if file is None or file.project_id != project_id:
            raise HTTPException(status_code=404, detail="File not found")

        chunk = await session.scalar(
            select(SubtitleChunk)
            .where(SubtitleChunk.file_id == file_id)
            .where(SubtitleChunk.chunk_index == chunk_index)
        )
        if chunk is None:
            raise HTTPException(status_code=404, detail="Chunk not found")

        if chunk.status not in ("job_failed", "validate_repair_failed"):
            raise HTTPException(
                status_code=409,
                detail=f"Chunk is not in a retryable state (current: {chunk.status})",
            )

        if chunk.status == "job_failed":
            failed_job_type = chunk.failed_job_type
            restore_status = _RETRY_STATUS_BY_JOB_TYPE.get(failed_job_type or "")
            if restore_status is None:
                raise HTTPException(
                    status_code=409,
                    detail=f"Cannot determine retry status for failed_job_type={failed_job_type!r}",
                )
            chunk.status = restore_status
        else:
            # validate_repair_failed: reset repair count and go back to translated
            chunk.status = "translated"
            chunk.repair_attempt_count = 0

        chunk.retry_count = 0
        chunk.last_error_code = None
        chunk.last_error_message = None
        chunk.failed_job_type = None
        chunk.updated_at = now

        # If file was put into waiting due to this chunk, restore it to processing.
        if file.status == "waiting" and file.blocking_reason in (
            FileBlockingReason.TRANSLATION_FAILED.value,
            FileBlockingReason.VALIDATION_FAILED.value,
        ):
            file.status = FileStatus.PROCESSING.value
            file.blocking_reason = None
            file.updated_at = now

        await session.commit()
        await session.refresh(chunk)

    await orchestrate_file(file_id, job_manager.enqueue)
    return ChunkOut(
        id=chunk.id,
        chunk_index=chunk.chunk_index,
        translate_from_line=chunk.translate_from_line,
        translate_to_line=chunk.translate_to_line,
        content_type=chunk.content_type or "dialogue",
        status=chunk.status,
        model=chunk.model,
        llm_review_needed=bool(chunk.llm_review_needed),
        retry_count=chunk.retry_count or 0,
        repair_attempt_count=chunk.repair_attempt_count or 0,
        last_error_code=chunk.last_error_code,
        last_error_message=chunk.last_error_message,
        failed_job_type=chunk.failed_job_type,
        qa_errors=0,
        qa_warnings=0,
    )


def _severity_rank(severity: str) -> int:
    # One scale: blocker | warning | info (legacy names tolerated read-only).
    return {
        "blocker": 0,
        "critical": 0,
        "error": 0,
        "high": 0,
        "warning": 1,
        "medium": 1,
        "info": 2,
        "low": 2,
    }.get(severity.lower(), 99)


def _subtitle_event_editor_out(
    event: SubtitleEvent,
    speaker_character_map: dict[str, tuple[str | None, CharacterGender | None, CharacterGender | None]] | None = None,
) -> SubtitleEventEditorOut:
    # Resolved issues ride along (flagged is_resolved) so the editor can
    # optionally show what was already handled — e.g. warnings resolved by
    # accepting the file. Unresolved first, then by severity.
    issues = sorted(
        event.qa_items,
        key=lambda item: (bool(item.is_resolved), _severity_rank(item.severity), item.created_at, item.id),
    )
    speaker_name = event.name.strip() if event.name and event.name.strip() else None
    speaker_gender = None
    character_name = None
    character_gender = None
    if speaker_name and speaker_character_map:
        mapped = speaker_character_map.get(speaker_name.lower())
        if mapped:
            character_name, character_gender, speaker_gender = mapped
    return SubtitleEventEditorOut(
        id=event.id,
        file_id=event.file_id,
        line_index=event.line_index,
        event_type=event.event_type,
        source_text=event.source_text,
        translated_text=event.translated_text,
        original_ai_translated_text=event.original_ai_translated_text,
        speaker_name=speaker_name,
        speaker_gender=speaker_gender,
        character_name=character_name,
        character_gender=character_gender,
        is_user_edited=bool(event.is_user_edited),
        is_locked=bool(event.is_locked),
        is_approved=bool(event.is_approved),
        issues=[QaIssueOut.model_validate(item) for item in issues],
    )


async def _speaker_character_map(session, project_id: int) -> dict[str, tuple[str | None, CharacterGender | None, CharacterGender | None]]:
    speakers = list((await session.scalars(
        select(ProjectSpeaker)
        .where(ProjectSpeaker.project_id == project_id)
        .options(selectinload(ProjectSpeaker.character))
    )).all())
    result: dict[str, tuple[str | None, CharacterGender | None, CharacterGender | None]] = {}
    for speaker in speakers:
        character = speaker.character
        result[speaker.name.lower()] = (
            character.name if character is not None else None,
            character.gender if character is not None else None,
            speaker.gender,
        )
    return result


@router.get("/{project_id}/files/{file_id}/subtitle-events", response_model=list[SubtitleEventEditorOut])
async def list_file_subtitle_events(project_id: int, file_id: int):
    async with AsyncSessionLocal() as session:
        file = await session.get(File, file_id)
        if file is None or file.project_id != project_id:
            raise HTTPException(status_code=404, detail="File not found")

        events = list((await session.scalars(
            select(SubtitleEvent)
            .where(SubtitleEvent.file_id == file_id)
            .options(selectinload(SubtitleEvent.qa_items))
            .order_by(SubtitleEvent.line_index)
        )).all())
        speaker_map = await _speaker_character_map(session, project_id)

        return [_subtitle_event_editor_out(event, speaker_map) for event in events]


async def _sync_tm_after_edit(project_id: int, event_id: int, file_status: str) -> None:
    """Post-acceptance editor corrections flow straight into the translation
    memory so the next translated file benefits (forward-only). Edits before
    acceptance are captured wholesale by populate_from_file at accept time."""
    if file_status not in (
        FileStatus.ACCEPTED.value,
        FileStatus.MUXING.value,
        FileStatus.COMPLETED.value,
    ):
        return
    try:
        await asyncio.to_thread(_sync_tm_from_event_sync, project_id, event_id)
    except Exception:
        logger.exception(
            "TM sync failed for event %d in project %d", event_id, project_id,
        )


def _sync_tm_from_event_sync(project_id: int, event_id: int) -> None:
    from app.core.database import SyncSessionLocal
    from app.subs import translation_memory as tm

    with SyncSessionLocal() as session:
        event = session.get(SubtitleEvent, event_id)
        if event is None:
            return
        if event.is_user_edited or event.is_approved:
            tm.upsert_event_entry(session, project_id, event, origin="human")
        else:
            # The user restored the AI text — a human entry owned by this
            # exact line no longer represents a correction.
            tm.downgrade_event_entry(session, project_id, event)
        session.commit()


@router.put("/{project_id}/files/{file_id}/subtitle-events/{event_id}", response_model=SubtitleEventEditorOut)
async def update_file_subtitle_event(
    project_id: int,
    file_id: int,
    event_id: int,
    body: SubtitleEventUpdateIn,
):
    async with AsyncSessionLocal() as session:
        file = await session.get(File, file_id)
        if file is None or file.project_id != project_id:
            raise HTTPException(status_code=404, detail="File not found")

        event = await session.get(
            SubtitleEvent,
            event_id,
            options=[selectinload(SubtitleEvent.qa_items)],
        )
        if event is None or event.file_id != file_id:
            raise HTTPException(status_code=404, detail="Subtitle event not found")

        event.translated_text = body.translated_text
        event.is_user_edited = 0 if body.translated_text == event.original_ai_translated_text else 1
        event.updated_at = datetime.utcnow().isoformat()
        file_status = file.status
        await session.commit()
        await session.refresh(event, ["qa_items"])
        speaker_map = await _speaker_character_map(session, project_id)
        out = _subtitle_event_editor_out(event, speaker_map)

    await _sync_tm_after_edit(project_id, event_id, file_status)
    return out


@router.post("/{project_id}/files/{file_id}/subtitle-events/{event_id}/revert", response_model=SubtitleEventEditorOut)
async def revert_file_subtitle_event(
    project_id: int,
    file_id: int,
    event_id: int,
):
    async with AsyncSessionLocal() as session:
        file = await session.get(File, file_id)
        if file is None or file.project_id != project_id:
            raise HTTPException(status_code=404, detail="File not found")

        event = await session.get(
            SubtitleEvent,
            event_id,
            options=[selectinload(SubtitleEvent.qa_items)],
        )
        if event is None or event.file_id != file_id:
            raise HTTPException(status_code=404, detail="Subtitle event not found")
        if event.original_ai_translated_text is None:
            raise HTTPException(status_code=409, detail="No original AI translation stored")

        event.translated_text = event.original_ai_translated_text
        event.is_user_edited = 0
        event.updated_at = datetime.utcnow().isoformat()
        file_status = file.status
        await session.commit()
        await session.refresh(event, ["qa_items"])
        speaker_map = await _speaker_character_map(session, project_id)
        out = _subtitle_event_editor_out(event, speaker_map)

    await _sync_tm_after_edit(project_id, event_id, file_status)
    return out


@router.post("/{project_id}/files/{file_id}/qa-issues/{issue_id}/resolve", response_model=SubtitleEventEditorOut)
async def resolve_file_qa_issue(project_id: int, file_id: int, issue_id: int):
    async with AsyncSessionLocal() as session:
        file = await session.get(File, file_id)
        if file is None or file.project_id != project_id:
            raise HTTPException(status_code=404, detail="File not found")

        issue = await session.get(QaItem, issue_id)
        if issue is None or issue.file_id != file_id:
            raise HTTPException(status_code=404, detail="QA issue not found")

        issue.is_resolved = 1
        issue.resolved_at = datetime.utcnow().isoformat()
        await session.commit()

        if issue.subtitle_event_id is None:
            raise HTTPException(status_code=404, detail="QA issue has no subtitle event")

        event = await session.get(
            SubtitleEvent,
            issue.subtitle_event_id,
            options=[selectinload(SubtitleEvent.qa_items)],
        )
        if event is None:
            raise HTTPException(status_code=404, detail="Subtitle event not found")
        speaker_map = await _speaker_character_map(session, project_id)
        out = _subtitle_event_editor_out(event, speaker_map)

    await orchestrate_file(file_id, job_manager.enqueue)
    return out


def _speaker_out(speaker: ProjectSpeaker, character_name: str | None) -> SpeakerOut:
    try:
        samples = json.loads(speaker.sample_lines_json or "[]")
    except json.JSONDecodeError:
        samples = []
    return SpeakerOut(
        id=speaker.id,
        project_id=speaker.project_id,
        name=speaker.name,
        gender=speaker.gender,
        character_id=speaker.character_id,
        character_name=character_name,
        match_confidence=speaker.match_confidence,
        match_origin=speaker.match_origin,
        match_rationale=speaker.match_rationale,
        line_count=speaker.line_count or 0,
        sample_lines=[s for s in samples if isinstance(s, str)],
        is_extra=bool(speaker.is_extra),
        content_tag=speaker.content_tag,
        created_at=speaker.created_at,
        updated_at=speaker.updated_at,
    )


async def _affected_chunk_ids(session, project_id: int, speaker_name: str) -> list[int]:
    """Dialogue chunks (of non-terminal files) that already produced a
    translation for at least one line spoken by this speaker."""
    rows = await session.execute(
        select(SubtitleChunk.id)
        .distinct()
        .join(File, SubtitleChunk.file_id == File.id)
        .join(SubtitleEvent, SubtitleEvent.file_id == SubtitleChunk.file_id)
        .where(File.project_id == project_id)
        .where(File.status.notin_([FileStatus.COMPLETED.value, FileStatus.FAILED.value]))
        .where(SubtitleChunk.content_type == "dialogue")
        .where(SubtitleChunk.status != "pending")
        .where(SubtitleEvent.event_type == "dialogue")
        .where(SubtitleEvent.name == speaker_name)
        .where(SubtitleEvent.line_index >= SubtitleChunk.translate_from_line)
        .where(SubtitleEvent.line_index <= SubtitleChunk.translate_to_line)
    )
    return [row[0] for row in rows.all()]


@router.get("/{project_id}/characters", response_model=list[CharacterOut])
async def list_project_characters(project_id: int):
    async with AsyncSessionLocal() as session:
        project = await session.get(Project, project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found")
        rows = await session.scalars(
            select(ProjectCharacter)
            .where(ProjectCharacter.project_id == project_id)
            .options(selectinload(ProjectCharacter.speakers))
            .order_by(ProjectCharacter.name)
        )
        characters = list(rows.all())
        result = []
        for char in characters:
            result.append(CharacterOut(
                **{c: getattr(char, c) for c in [
                    "id", "project_id", "external_id", "name", "role",
                    "gender", "social_position", "aliases", "note",
                    "created_at", "updated_at",
                ]},
                speaker_ids=[s.id for s in char.speakers],
            ))
        return result


@router.get("/{project_id}/speakers", response_model=list[SpeakerOut])
async def list_project_speakers(project_id: int):
    async with AsyncSessionLocal() as session:
        project = await session.get(Project, project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found")
        rows = await session.scalars(
            select(ProjectSpeaker)
            .where(ProjectSpeaker.project_id == project_id)
            .options(selectinload(ProjectSpeaker.character))
            .order_by(ProjectSpeaker.line_count.desc(), ProjectSpeaker.name)
        )
        return [
            _speaker_out(s, s.character.name if s.character else None)
            for s in rows.all()
        ]


@router.put("/{project_id}/speakers/{speaker_id}", response_model=SpeakerUpdateOut)
async def update_project_speaker(project_id: int, speaker_id: int, body: SpeakerUpdateIn):
    """Manual mapping correction — sets origin=manual so the inference job
    never overwrites it, and reports how many already-translated chunks the
    correction affects (retranslation is a separate explicit call)."""
    now = datetime.utcnow().isoformat()
    async with AsyncSessionLocal() as session:
        project = await session.get(Project, project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found")

        speaker = await session.get(ProjectSpeaker, speaker_id)
        if speaker is None or speaker.project_id != project_id:
            raise HTTPException(status_code=404, detail="Speaker not found")

        update_data = body.model_dump(exclude_unset=True)

        if "character_id" in update_data:
            character_id = update_data["character_id"]
            if character_id is not None:
                character = await session.get(ProjectCharacter, character_id)
                if character is None or character.project_id != project_id:
                    raise HTTPException(status_code=422, detail="Character not found in project")
            speaker.character_id = character_id
            speaker.match_origin = "manual"
            speaker.match_confidence = 1.0
            speaker.match_rationale = None
            speaker.is_extra = 0
            if character_id is not None:
                speaker.content_tag = None
        if "gender" in update_data:
            gender = update_data["gender"]
            speaker.gender = gender.value if isinstance(gender, CharacterGender) else gender
        if "is_extra" in update_data and update_data["is_extra"] is not None:
            speaker.is_extra = 1 if update_data["is_extra"] else 0
            if speaker.is_extra:
                speaker.character_id = None
                speaker.match_origin = "manual"
                speaker.match_confidence = 1.0
                speaker.content_tag = None
        if "content_tag" in update_data:
            content_tag = update_data["content_tag"]
            speaker.content_tag = content_tag
            if content_tag is not None:
                # Tagging as sign/karaoke/song is mutually exclusive with a
                # character mapping or the extra flag; manual origin protects
                # the row from the inference job.
                speaker.character_id = None
                speaker.is_extra = 0
                speaker.match_origin = "manual"
                speaker.match_confidence = 1.0
                speaker.match_rationale = None

        speaker.updated_at = now
        await session.commit()
        await session.refresh(speaker, ["character"])

        affected = await _affected_chunk_ids(session, project_id, speaker.name)
        return SpeakerUpdateOut(
            speaker=_speaker_out(speaker, speaker.character.name if speaker.character else None),
            affected_chunk_count=len(affected),
        )


@router.post("/{project_id}/speakers/{speaker_id}/retranslate-affected", response_model=SpeakerUpdateOut)
async def retranslate_affected_chunks(project_id: int, speaker_id: int):
    """Reset the dialogue chunks containing this speaker's lines back to
    'pending' so they re-run through the whole pipeline with the corrected
    identity. User-edited lines survive (translate/polish skip them).
    Completed files are not touched."""
    now = datetime.utcnow().isoformat()
    async with AsyncSessionLocal() as session:
        speaker = await session.get(
            ProjectSpeaker, speaker_id, options=[selectinload(ProjectSpeaker.character)])
        if speaker is None or speaker.project_id != project_id:
            raise HTTPException(status_code=404, detail="Speaker not found")

        chunk_ids = await _affected_chunk_ids(session, project_id, speaker.name)
        if chunk_ids:
            chunks = (await session.scalars(
                select(SubtitleChunk).where(SubtitleChunk.id.in_(chunk_ids))
            )).all()
            file_ids = set()
            for chunk in chunks:
                chunk.status = "pending"
                chunk.repair_attempt_count = 0
                chunk.polish_attempt_count = 0
                chunk.retry_count = 0
                chunk.last_error_code = None
                chunk.last_error_message = None
                chunk.failed_job_type = None
                chunk.updated_at = now
                file_ids.add(chunk.file_id)

            files = (await session.scalars(
                select(File).where(File.id.in_(file_ids))
            )).all()
            for file in files:
                if file.status in (FileStatus.REVIEW_REQUIRED.value, FileStatus.WAITING.value,
                                   FileStatus.ACCEPTED.value, FileStatus.MUXING.value):
                    file.status = FileStatus.PROCESSING.value
                    file.blocking_reason = None
                    file.updated_at = now
            await session.commit()

        out = SpeakerUpdateOut(
            speaker=_speaker_out(speaker, speaker.character.name if speaker.character else None),
            affected_chunk_count=len(chunk_ids),
        )

    await orchestrate_project(project_id, job_manager.enqueue)
    return out


class CharacterCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    gender: CharacterGender | None = None
    role: str | None = None
    social_position: str | None = None
    note: str | None = None


@router.post("/{project_id}/characters", response_model=CharacterOut, status_code=201)
async def create_project_character(project_id: int, body: CharacterCreateIn):
    """Manually add a character the provider roster is missing."""
    now = datetime.utcnow().isoformat()
    async with AsyncSessionLocal() as session:
        await _get_project_or_404(session, project_id)
        existing = await session.scalar(
            select(ProjectCharacter)
            .where(ProjectCharacter.project_id == project_id)
            .where(func.lower(ProjectCharacter.name) == body.name.strip().lower())
        )
        if existing is not None:
            raise HTTPException(status_code=409, detail="Character already exists")

        char = ProjectCharacter(
            project_id=project_id,
            name=body.name.strip(),
            gender=body.gender.value if body.gender else None,
            role=body.role,
            social_position=body.social_position,
            note=body.note,
            created_at=now,
            updated_at=now,
        )
        session.add(char)
        await session.commit()
        await session.refresh(char)
        return CharacterOut(
            **{c: getattr(char, c) for c in [
                "id", "project_id", "external_id", "name", "role",
                "gender", "social_position", "aliases", "note",
                "created_at", "updated_at",
            ]},
            speaker_ids=[],
        )


@router.delete("/{project_id}/characters/{character_id}", status_code=204)
async def delete_project_character(project_id: int, character_id: int):
    """Remove a character; mapped speakers become unmapped (FK SET NULL)."""
    async with AsyncSessionLocal() as session:
        char = await session.get(ProjectCharacter, character_id)
        if char is None or char.project_id != project_id:
            raise HTTPException(status_code=404, detail="Character not found")
        await session.delete(char)
        await session.commit()


class RefreshMetadataOut(BaseModel):
    characters_created: int
    characters_updated: int
    episodes_created: int
    episodes_updated: int


@router.post("/{project_id}/refresh-metadata", response_model=RefreshMetadataOut)
async def refresh_project_metadata(project_id: int):
    """Re-fetch characters and episode metadata from the project's stored
    provider. Provider-sourced fields are upserted; user-owned character
    fields (social position, aliases, notes, a set gender) are preserved."""
    from app.metadata.registry import get_provider
    from app.metadata.sync import upsert_characters, upsert_episodes

    async with AsyncSessionLocal() as session:
        project = await _get_project_or_404(session, project_id)
        provider_name = project.anime_provider
        external_id = project.anime_external_id

    try:
        provider = get_provider(provider_name)
        characters = await provider.get_characters(external_id)
        episodes = await provider.get_episodes(external_id)
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Metadata fetch from {provider_name} failed: {exc}",
        )

    async with AsyncSessionLocal() as session:
        chars_created, chars_updated = await upsert_characters(session, project_id, characters)
        eps_created, eps_updated = await upsert_episodes(session, project_id, episodes)
        await session.commit()

    return RefreshMetadataOut(
        characters_created=chars_created,
        characters_updated=chars_updated,
        episodes_created=eps_created,
        episodes_updated=eps_updated,
    )


@router.put("/{project_id}/characters/{character_id}", response_model=CharacterOut)
async def update_project_character(
    project_id: int, character_id: int, body: CharacterUpdateIn
):
    async with AsyncSessionLocal() as session:
        project = await session.get(Project, project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found")

        char = await session.get(
            ProjectCharacter, character_id,
            options=[selectinload(ProjectCharacter.speakers)],
        )
        if char is None or char.project_id != project_id:
            raise HTTPException(status_code=404, detail="Character not found")

        # Validate all provided speaker IDs belong to this project
        deduped_speaker_ids = list(dict.fromkeys(body.speaker_ids))
        if deduped_speaker_ids:
            valid_speakers = await session.scalars(
                select(ProjectSpeaker)
                .where(
                    ProjectSpeaker.id.in_(deduped_speaker_ids),
                    ProjectSpeaker.project_id == project_id,
                )
            )
            valid_ids = {s.id for s in valid_speakers.all()}
            invalid = set(deduped_speaker_ids) - valid_ids
            if invalid:
                raise HTTPException(
                    status_code=422,
                    detail=f"Speaker IDs not found in project: {sorted(invalid)}",
                )

        # Update scalar fields — only overwrite what was explicitly sent
        update_data = body.model_dump(exclude_unset=True)
        if "gender" in update_data:
            char.gender = update_data["gender"]
        if "social_position" in update_data:
            char.social_position = update_data["social_position"]
        if "note" in update_data:
            char.note = update_data["note"]
        char.updated_at = datetime.utcnow().isoformat()

        # Reassign speakers when explicitly provided (manual decision).
        if "speaker_ids" in update_data:
            now = datetime.utcnow().isoformat()
            currently_assigned = (await session.scalars(
                select(ProjectSpeaker).where(ProjectSpeaker.character_id == character_id)
            )).all()
            for s in currently_assigned:
                if s.id not in deduped_speaker_ids:
                    s.character_id = None
                    s.match_origin = "manual"
                    s.match_confidence = 1.0
                    s.updated_at = now
            if deduped_speaker_ids:
                to_assign = (await session.scalars(
                    select(ProjectSpeaker).where(ProjectSpeaker.id.in_(deduped_speaker_ids))
                )).all()
                for s in to_assign:
                    s.character_id = character_id
                    s.match_origin = "manual"
                    s.match_confidence = 1.0
                    s.is_extra = 0
                    s.updated_at = now

        await session.commit()
        await session.refresh(char, ["speakers"])

        return CharacterOut(
            **{c: getattr(char, c) for c in [
                "id", "project_id", "external_id", "name", "role",
                "gender", "social_position", "aliases", "note",
                "created_at", "updated_at",
            ]},
            speaker_ids=[s.id for s in char.speakers],
        )


class TmEntryOut(BaseModel):
    id: int
    source_text: str
    target_text: str
    content_type: str
    origin: str
    use_count: int
    updated_at: str

    model_config = {"from_attributes": True}


class TmEntryUpdateIn(BaseModel):
    target_text: str = Field(min_length=1)


class TmListOut(BaseModel):
    total: int
    items: list[TmEntryOut]


@router.get("/{project_id}/translation-memory", response_model=TmListOut)
async def list_translation_memory(
    project_id: int, q: str | None = None, limit: int = 200, offset: int = 0,
):
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    async with AsyncSessionLocal() as session:
        await _get_project_or_404(session, project_id)
        filters = [TranslationMemoryEntry.project_id == project_id]
        if q and q.strip():
            pattern = f"%{q.strip()}%"
            filters.append(
                TranslationMemoryEntry.source_text.ilike(pattern)
                | TranslationMemoryEntry.target_text.ilike(pattern)
            )
        total = (await session.execute(
            select(func.count()).select_from(TranslationMemoryEntry).where(*filters)
        )).scalar_one()
        rows = await session.scalars(
            select(TranslationMemoryEntry)
            .where(*filters)
            .order_by(TranslationMemoryEntry.use_count.desc(),
                      TranslationMemoryEntry.updated_at.desc())
            .offset(offset)
            .limit(limit)
        )
        return TmListOut(
            total=int(total),
            items=[TmEntryOut.model_validate(r) for r in rows.all()],
        )


@router.put("/{project_id}/translation-memory/{entry_id}", response_model=TmEntryOut)
async def update_translation_memory_entry(project_id: int, entry_id: int, body: TmEntryUpdateIn):
    """Manual correction — promoted to origin=human so the pipeline never
    downgrades it and auto-applies it on future exact matches."""
    async with AsyncSessionLocal() as session:
        entry = await session.get(TranslationMemoryEntry, entry_id)
        if entry is None or entry.project_id != project_id:
            raise HTTPException(status_code=404, detail="TM entry not found")
        entry.target_text = body.target_text
        entry.origin = "human"
        entry.updated_at = datetime.utcnow().isoformat()
        await session.commit()
        await session.refresh(entry)
        return entry


@router.delete("/{project_id}/translation-memory/{entry_id}", status_code=204)
async def delete_translation_memory_entry(project_id: int, entry_id: int):
    async with AsyncSessionLocal() as session:
        entry = await session.get(TranslationMemoryEntry, entry_id)
        if entry is None or entry.project_id != project_id:
            raise HTTPException(status_code=404, detail="TM entry not found")
        await session.delete(entry)
        await session.commit()


class FileMetricsOut(BaseModel):
    file_id: int
    filename: str
    episode_number: int | None
    prompt_version: str | None
    events_total: int
    events_user_edited: int
    events_approved: int
    edit_distance_norm: float | None
    polish_churn_norm: float | None
    polish_edit_count: int
    qa_blockers: int
    qa_warnings: int
    qa_info: int
    mean_confidence: float | None
    mean_confidence_edited: float | None
    llm_cost_usd: float | None
    prompt_tokens: int
    completion_tokens: int
    created_at: str


@router.get("/{project_id}/metrics", response_model=list[FileMetricsOut])
async def get_project_metrics(project_id: int):
    """Per-file quality metrics (episode order) — edit-distance trend falling
    across episodes means the consistency layer is doing its job."""
    async with AsyncSessionLocal() as session:
        await _get_project_or_404(session, project_id)
        rows = (await session.execute(
            select(FileQualityMetric, File.filename, File.episode_number, File.relative_path)
            .join(File, FileQualityMetric.file_id == File.id)
            .where(FileQualityMetric.project_id == project_id)
        )).all()

    rows = sorted(rows, key=lambda r: (r.episode_number is None, r.episode_number, r.relative_path))
    return [
        FileMetricsOut(
            file_id=m.file_id,
            filename=filename,
            episode_number=episode_number,
            prompt_version=m.prompt_version,
            events_total=m.events_total,
            events_user_edited=m.events_user_edited,
            events_approved=m.events_approved,
            edit_distance_norm=m.edit_distance_norm,
            polish_churn_norm=m.polish_churn_norm,
            polish_edit_count=m.polish_edit_count,
            qa_blockers=m.qa_blockers,
            qa_warnings=m.qa_warnings,
            qa_info=m.qa_info,
            mean_confidence=m.mean_confidence,
            mean_confidence_edited=m.mean_confidence_edited,
            llm_cost_usd=m.llm_cost_usd,
            prompt_tokens=m.prompt_tokens,
            completion_tokens=m.completion_tokens,
            created_at=m.created_at,
        )
        for m, filename, episode_number, _relative_path in rows
    ]


class ProjectStatsOut(BaseModel):
    qa_errors: int
    qa_warnings: int


@router.get("/{project_id}/stats", response_model=ProjectStatsOut)
async def get_project_stats(project_id: int):
    async with AsyncSessionLocal() as session:
        row = (await session.execute(
            select(
                func.sum(case((QaItem.severity == "blocker", 1), else_=0)).label("errors"),
                func.sum(case((QaItem.severity != "blocker", 1), else_=0)).label("warnings"),
            )
            .join(File, QaItem.file_id == File.id)
            .where(File.project_id == project_id, QaItem.is_resolved == 0)
        )).one()
        return ProjectStatsOut(
            qa_errors=int(row.errors or 0),
            qa_warnings=int(row.warnings or 0),
        )


