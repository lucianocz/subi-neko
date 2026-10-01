"""Style bible jobs.

generate_style_bible — one-time per project (before the first file
translates): builds the tone/register/honorific guidance, initial glossary,
character voices and address pairs from the character roster plus a sample
of the first ready file's dialogue. Character names are seeded into the
glossary deterministically (origin=metadata) regardless of the LLM output.

update_style_bible — per accepted episode: additive-only pass that captures
NEW terms, voices and address-pair changes from the finished translation.
"""
from __future__ import annotations

import logging

from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.core.database import SyncSessionLocal
from app.db.models import (
    File,
    ProjectCharacterStyle,
    ProjectCharacter,
    ProjectGlossaryTerm,
    ProjectSpeaker,
    ProjectStyleBible,
    ProjectWatchedWord,
    SubtitleEvent,
)
from app.jobs.context import JobContext, JobResult, ProgressFn
from app.jobs.handlers.character_context import (
    allocate_character_descriptions,
    build_preparation_character_block,
)
from app.jobs.handlers.prompt_context import load_prompt_characters
from app.jobs.handlers.style_store import (
    canonical_address_pairs,
    insert_new_glossary_terms,
    upsert_address_pairs,
    upsert_character_voices,
)
from app.jobs.registry import register_job_handler
from app.llm import client as llm_client
from app.llm.schemas import GlossaryTermOut, StyleBibleResponse, StyleBibleUpdateResponse
from app.subs.tag_masking import plain_text

logger = logging.getLogger(__name__)

_SAMPLE_SIZE = 300
_STYLE_BIBLE_COMPLETION_BASE = 4096
_STYLE_BIBLE_COMPLETION_PER_CHARACTER = 128
_STYLE_BIBLE_COMPLETION_PER_SPEAKER = 96
_STYLE_BIBLE_COMPLETION_CAP = 16000


def style_bible_completion_budget(
    character_count: int, dialogue_speaker_count: int, configured_max: int,
) -> int:
    """Bound initial output space by mandatory names and likely voice/pair rows."""
    requested = (
        _STYLE_BIBLE_COMPLETION_BASE
        + max(0, character_count) * _STYLE_BIBLE_COMPLETION_PER_CHARACTER
        + max(0, dialogue_speaker_count) * _STYLE_BIBLE_COMPLETION_PER_SPEAKER
    )
    return min(max(1, configured_max), _STYLE_BIBLE_COMPLETION_CAP, requested)


def _sample_dialogue(
    session, file_id: int, with_translation: bool,
    excluded_speaker_names: set[str] | None = None,
) -> list[str]:
    """Up to _SAMPLE_SIZE dialogue lines, evenly spread across the file."""
    events = list(session.scalars(
        select(SubtitleEvent)
        .where(SubtitleEvent.file_id == file_id)
        .where(SubtitleEvent.event_type == "dialogue")
        .where(SubtitleEvent.content_type == "dialogue")
        .order_by(SubtitleEvent.line_index)
    ).all())

    excluded = excluded_speaker_names or set()
    events = [
        event for event in events
        if not event.name or event.name.casefold() not in excluded
    ]

    if len(events) > _SAMPLE_SIZE:
        step = len(events) / _SAMPLE_SIZE
        events = [events[int(i * step)] for i in range(_SAMPLE_SIZE)]

    lines = []
    for e in events:
        source = plain_text(e.source_text)
        if not source:
            continue
        speaker = " ".join((e.name or "").split()) or "(unknown)"
        fields = [
            f"[LINE {e.line_index}]",
            f"speaker: {speaker}",
            f"EN: {source}",
        ]
        if with_translation:
            translation = plain_text(e.translated_text) if e.translated_text else ""
            fields.append(f"CS: {translation or '(missing)'}")
        fields.append("[/LINE]")
        lines.append("\n".join(fields))
    return lines


def _seed_character_glossary(session, project_id: int, now: str) -> int:
    """Deterministic glossary seed: every speaking character's name, kept
    as-is, with gender attached (origin=metadata)."""
    characters = load_prompt_characters(session, project_id)
    seeds = [
        GlossaryTermOut(source=c.name, target=c.name, category="name",
                        gender=c.gender, vocative=None, note=None)
        for c in characters if c.name and c.name.strip()
    ]
    return insert_new_glossary_terms(session, project_id, seeds, "metadata", now)


def _episode_speaker_mappings(session, project_id: int, file_id: int) -> list[tuple[str, str]]:
    """Mapped raw speaker labels that actually occur in this episode."""
    present = {
        name.casefold()
        for name in session.scalars(
            select(SubtitleEvent.name)
            .where(SubtitleEvent.file_id == file_id)
            .where(SubtitleEvent.event_type == "dialogue")
            .where(SubtitleEvent.content_type == "dialogue")
            .where(SubtitleEvent.name.is_not(None))
        ).all()
        if name and name.strip()
    }
    speakers = session.scalars(
        select(ProjectSpeaker)
        .where(ProjectSpeaker.project_id == project_id)
        .options(selectinload(ProjectSpeaker.character))
        .order_by(ProjectSpeaker.name)
    ).all()
    return [
        (speaker.name, speaker.character.name)
        for speaker in speakers
        if speaker.name.casefold() in present and speaker.character is not None
    ]


def _tagged_speaker_names(session, project_id: int) -> set[str]:
    """Raw labels already classified as sign/song/karaoke by the authoritative pass."""
    return {
        name.casefold()
        for name in session.scalars(
            select(ProjectSpeaker.name)
            .where(ProjectSpeaker.project_id == project_id)
            .where(ProjectSpeaker.content_tag.is_not(None))
        ).all()
        if name and name.strip()
    }


def _episode_dialogue_speaker_count(
    session, file_id: int, excluded_speaker_names: set[str] | None = None,
) -> int:
    """Distinct non-empty raw labels represented in initial dialogue evidence."""
    names = session.scalars(
        select(SubtitleEvent.name)
        .where(SubtitleEvent.file_id == file_id)
        .where(SubtitleEvent.event_type == "dialogue")
        .where(SubtitleEvent.content_type == "dialogue")
        .where(SubtitleEvent.name.is_not(None))
    ).all()
    excluded = excluded_speaker_names or set()
    return len({
        name.casefold() for name in names
        if name and name.strip() and name.casefold() not in excluded
    })


def _episode_character_ids(session, project_id: int, file_id: int) -> set[int]:
    """Canonical characters tied to labels actually present in the sample."""
    present = {
        name.casefold()
        for name in session.scalars(
            select(SubtitleEvent.name)
            .where(SubtitleEvent.file_id == file_id)
            .where(SubtitleEvent.event_type == "dialogue")
            .where(SubtitleEvent.content_type == "dialogue")
            .where(SubtitleEvent.name.is_not(None))
        ).all()
        if name and name.strip()
    }
    return {
        speaker.character_id
        for speaker in session.scalars(
            select(ProjectSpeaker).where(ProjectSpeaker.project_id == project_id)
        ).all()
        if speaker.character_id is not None and speaker.name.casefold() in present
    }


@register_job_handler("generate_style_bible")
def generate_style_bible(
    payload: dict[str, Any],
    ctx: JobContext,
    progress: ProgressFn,
) -> JobResult:
    project_id: int = payload["project_id"]
    sample_file_id: int = payload["sample_file_id"]
    model: str = payload.get("model") or ctx.options.openai_model_better or ctx.options.openai_model_cheap
    now = datetime.utcnow().isoformat()

    progress(0.05, "Loading project data")

    with SyncSessionLocal() as session:
        existing = session.scalar(
            select(func.count()).select_from(ProjectStyleBible)
            .where(ProjectStyleBible.project_id == project_id)
        )
        if existing:
            return JobResult(status="succeeded",
                             result={"skipped": "style bible already exists"},
                             error_code=None, error_message=None)

        seeded = _seed_character_glossary(session, project_id, now)
        session.commit()

        characters = load_prompt_characters(session, project_id)
        descriptions = allocate_character_descriptions(
            characters,
            ctx.options.style_bible_character_description_max,
            ctx.options.style_bible_character_description_budget,
            _episode_character_ids(session, project_id, sample_file_id),
        )
        char_block = build_preparation_character_block(characters, descriptions)
        tagged_speakers = _tagged_speaker_names(session, project_id)
        sample_lines = _sample_dialogue(
            session, sample_file_id, with_translation=False,
            excluded_speaker_names=tagged_speakers,
        )
        dialogue_speaker_count = _episode_dialogue_speaker_count(
            session, sample_file_id, excluded_speaker_names=tagged_speakers)
        watched = [
            w.word for w in session.scalars(
                select(ProjectWatchedWord)
                .where(ProjectWatchedWord.project_id == project_id)
                .where(ProjectWatchedWord.word_type == "original")
            ).all()
        ]

    if not sample_lines:
        return JobResult(status="failed", result=None,
                         error_code="NO_EVENTS",
                         error_message=f"Sample file id={sample_file_id} has no dialogue lines")

    progress(0.25, f"Building style bible prompt ({len(sample_lines)} sample lines)")

    system_prompt = ctx.options.resolved_style_bible_prompt().strip()
    user_parts = []
    if char_block:
        user_parts.append(f"## Characters\n{char_block}")
    if watched:
        user_parts.append("## Watched Terms\nThe user flagged these terms as important:\n"
                          + "\n".join(f"- {w}" for w in watched))
    user_parts.append("## Sample Dialogue\n" + "\n".join(sample_lines))
    user_message = "\n\n".join(user_parts)

    progress(0.4, f"Calling LLM ({model})")

    try:
        response, stats = llm_client.complete(
            task="style_bible",
            model=model,
            system=system_prompt,
            user=user_message,
            schema=StyleBibleResponse,
            options=ctx.options,
            max_completion_tokens=style_bible_completion_budget(
                len(characters), dialogue_speaker_count,
                ctx.options.llm_max_completion_tokens,
            ),
            project_id=project_id,
            file_id=sample_file_id,
        )
    except llm_client.LlmError as exc:
        return JobResult(status="failed", result=None,
                         error_code=exc.code, error_message=exc.message)

    progress(0.8, "Writing style bible")

    with SyncSessionLocal() as session:
        # Re-check in case a concurrent run won the race.
        existing = session.scalar(
            select(func.count()).select_from(ProjectStyleBible)
            .where(ProjectStyleBible.project_id == project_id)
        )
        if not existing:
            session.add(ProjectStyleBible(
                project_id=project_id,
                version=1,
                tone_summary=response.tone_summary,
                register_notes=response.register_notes,
                honorific_policy=response.honorific_policy,
                generated_from_file_id=sample_file_id,
                model=model,
                created_at=now,
                updated_at=now,
            ))
        new_terms = insert_new_glossary_terms(session, project_id, response.terms, "llm", now)
        new_voices = upsert_character_voices(session, project_id, response.character_voices, "llm", now)
        new_pairs = upsert_address_pairs(session, project_id, response.address_pairs, "llm", now)
        session.commit()

    progress(1.0, "Done")
    return JobResult(
        status="succeeded",
        result={
            "seeded_character_terms": seeded,
            "new_glossary_terms": new_terms,
            "character_voices": new_voices,
            "address_pairs": new_pairs,
            "response_mode": stats.response_mode,
        },
        error_code=None,
        error_message=None,
    )


@register_job_handler("update_style_bible")
def update_style_bible(
    payload: dict[str, Any],
    ctx: JobContext,
    progress: ProgressFn,
) -> JobResult:
    project_id: int = payload["project_id"]
    file_id: int = payload["file_id"]
    model: str = payload.get("model") or ctx.options.openai_model_better or ctx.options.openai_model_cheap
    now = datetime.utcnow().isoformat()

    progress(0.05, "Loading current style data")

    with SyncSessionLocal() as session:
        file = session.get(File, file_id)
        if file is None or file.project_id != project_id:
            return JobResult(status="failed", result=None,
                             error_code="FILE_NOT_FOUND",
                             error_message=f"File id={file_id} not found in project {project_id}")
        translation_attempt = file.translation_requested_at

        terms = list(session.scalars(
            select(ProjectGlossaryTerm)
            .where(ProjectGlossaryTerm.project_id == project_id)
            .where(ProjectGlossaryTerm.is_active == 1)
        ).all())
        pairs = canonical_address_pairs(session, project_id)
        voices = list(session.execute(
            select(ProjectCharacter.name, ProjectCharacterStyle.voice_note, ProjectCharacterStyle.register)
            .join(ProjectCharacterStyle, ProjectCharacterStyle.project_character_id == ProjectCharacter.id)
            .where(ProjectCharacter.project_id == project_id)
        ).all())

        current_bible = session.scalar(
            select(ProjectStyleBible)
            .where(ProjectStyleBible.project_id == project_id)
            .order_by(ProjectStyleBible.version.desc())
            .limit(1)
        )

        sample_lines = _sample_dialogue(session, file_id, with_translation=True)
        speaker_mappings = _episode_speaker_mappings(session, project_id, file_id)
        episode_character_ids = _episode_character_ids(session, project_id, file_id)
        episode_characters = [
            character for character in load_prompt_characters(session, project_id)
            if character.id in episode_character_ids
        ]
        descriptions = allocate_character_descriptions(
            episode_characters,
            ctx.options.style_bible_character_description_max,
            ctx.options.style_bible_character_description_budget,
            episode_character_ids,
        )
        character_block = build_preparation_character_block(
            episode_characters, descriptions)

    if not sample_lines:
        return JobResult(status="succeeded", result={"skipped": "no dialogue lines"},
                         error_code=None, error_message=None)

    progress(0.25, "Building update prompt")

    system_prompt = ctx.options.resolved_style_bible_update_prompt().strip()

    glossary_block = "\n".join(
        f"- {t.source_term} => {t.target_term} ({t.category})" for t in terms
    ) or "(empty)"
    pairs_block = "\n".join(
        f"- {speaker} -> {addressee}: {mode}" for speaker, addressee, mode in pairs
    ) or "(none)"
    voices_block = "\n".join(
        f"- {name}: {voice_note or ''} (register: {register or 'unknown'})"
        for name, voice_note, register in voices
    ) or "(none)"
    bible_block = "\n".join([
        f"tone_summary: {current_bible.tone_summary or '(not provided)'}",
        f"register_notes: {current_bible.register_notes or '(not provided)'}",
        f"honorific_policy: {current_bible.honorific_policy or '(not provided)'}",
    ]) if current_bible is not None else "(none)"

    mapping_block = "\n".join(
        f"- {speaker} → {character}" for speaker, character in speaker_mappings
    ) or "(none)"
    user_message = (
        f"## Current Approved Style Bible\n{bible_block}\n\n"
        f"## Current Glossary\n{glossary_block}\n\n"
        f"## Current Address Pairs\n{pairs_block}\n\n"
        f"## Speaker Identity Mapping\n{mapping_block}\n\n"
        f"## Episode Character Metadata\n{character_block or '(none)'}\n\n"
        f"## Current Character Voices\n{voices_block}\n\n"
        "## Update Rules\nExisting address pairs are authoritative. Add only genuinely new "
        "directed relationships; never propose a different mode for an existing relationship. "
        "The Czech translation is generated evidence and may contain T–V mistakes, so do not "
        "infer a project-wide convention or a change of convention from it alone.\n\n"
        f"## New Episode Dialogue (source and generated translation)\n"
        + "\n\n".join(sample_lines)
    )

    progress(0.4, f"Calling LLM ({model})")

    try:
        response, stats = llm_client.complete(
            task="style_bible",
            model=model,
            system=system_prompt,
            user=user_message,
            schema=StyleBibleUpdateResponse,
            options=ctx.options,
            max_completion_tokens=4000,
            project_id=project_id,
            file_id=file_id,
        )
    except llm_client.LlmError as exc:
        return JobResult(status="failed", result=None,
                         error_code=exc.code, error_message=exc.message)

    progress(0.8, "Writing additions")

    with SyncSessionLocal() as session:
        current_file = session.get(File, file_id)
        if (current_file is None
                or current_file.translation_requested_at != translation_attempt):
            return JobResult(
                status="succeeded",
                result={"skipped": "stale translation attempt"},
                error_code=None,
                error_message=None,
            )
        new_terms = insert_new_glossary_terms(session, project_id, response.terms, "llm", now)
        new_voices = upsert_character_voices(session, project_id, response.character_voices, "llm", now)
        new_pairs = upsert_address_pairs(
            session, project_id, response.address_pairs, "llm", now,
            update_existing_mode=False,
        )
        session.commit()

    progress(1.0, "Done")
    return JobResult(
        status="succeeded",
        result={
            "new_glossary_terms": new_terms,
            "character_voices": new_voices,
            "address_pairs": new_pairs,
            "response_mode": stats.response_mode,
        },
        error_code=None,
        error_message=None,
    )
