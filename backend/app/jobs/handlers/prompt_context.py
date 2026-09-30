from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.db.models import (
    File,
    FileAnalysis,
    ProjectCharacter,
    ProjectCharacterStyle,
    ProjectEpisode,
    ProjectGlossaryTerm,
    ProjectSpeaker,
    ProjectStyleBible,
    SubtitleEvent,
)
from app.jobs.handlers.style_store import canonical_address_pairs
from app.subs.tag_masking import plain_text

# AniDB character_type values that denote non-speaking "characters"
# (organizations, vessels/mecha, etc.) — not useful as speaker candidates.
_NON_SPEAKING_CHARACTER_TYPES = {"organization", "vessel"}

_DESCRIPTION_MAX_LEN = 200


def load_preceding_context_events(
    session: Session,
    file_id: int,
    content_type: str,
    before_line: int,
    limit: int,
) -> list[SubtitleEvent]:
    """The last `limit` events before `before_line`, in chronological order,
    restricted to one content_type partition.

    The window is deliberately NOT cross-partition. Chunks are scheduled per
    partition (dialogue is a gated sequential chain; signs/karaoke/songs run
    in parallel), so lines from another partition are usually still
    untranslated at this point — they would occupy context slots with
    source-only rows and crowd out the translated context the window exists
    to provide. Within the partition the ordering guarantees hold.
    """
    if limit <= 0:
        return []
    rows = list(session.scalars(
        select(SubtitleEvent)
        .where(SubtitleEvent.file_id == file_id)
        .where(SubtitleEvent.event_type == "dialogue")
        .where(SubtitleEvent.content_type == content_type)
        .where(SubtitleEvent.line_index < before_line)
        .order_by(SubtitleEvent.line_index.desc())
        .limit(limit)
    ).all())
    rows.reverse()
    return rows


def load_following_context_events(
    session: Session,
    file_id: int,
    content_type: str,
    after_line: int,
    limit: int,
) -> list[SubtitleEvent]:
    """The first `limit` events after `after_line`, in chronological order,
    restricted to one content_type partition.

    The mirror image of load_preceding_context_events, and the reason the two
    are not one function: these lines are almost always still untranslated
    (the dialogue chain translates strictly forward), so callers render them
    as source-only lookahead. Without it the last lines of every chunk are
    translated blind to what follows — the setup of a joke whose punchline
    is in the next chunk, a question whose answer fixes the register.
    """
    if limit <= 0:
        return []
    return list(session.scalars(
        select(SubtitleEvent)
        .where(SubtitleEvent.file_id == file_id)
        .where(SubtitleEvent.event_type == "dialogue")
        .where(SubtitleEvent.content_type == content_type)
        .where(SubtitleEvent.line_index > after_line)
        .order_by(SubtitleEvent.line_index)
        .limit(limit)
    ).all())


def build_lookahead_lines(events: list[SubtitleEvent]) -> list[str]:
    """Source-only [AHEAD] rows for the lines that follow a chunk."""
    lines = []
    for e in events:
        text = plain_text(e.source_text)
        if not text:
            continue
        lines.append(f"[AHEAD] {e.line_index}: {text}")
    return lines


# ---------------------------------------------------------------------------
# Reading-speed budget
# ---------------------------------------------------------------------------

# Below this the CPS-derived budget is unsatisfiable noise ("max 0 chars"
# for a 40 ms sign frame) — omit the constraint and let the deterministic
# readability check flag real CPS problems after the fact.
MIN_CHAR_BUDGET = 10

# The budget floors at the source's own length (times this ratio) rather
# than the raw CPS number, so a line never gets instructed to shrink below
# what the English original already carried in the same slot. If the source
# itself was timed at or above the CPS limit, that's a timing quirk of this
# event (often a sentence split across several short events), not evidence
# the content needs cutting — Czech shouldn't be forced shorter than English
# was. Kept under 1.0 so it never mandates expansion on its own.
SOURCE_FLOOR_RATIO = 0.9


def char_budget(
    start_ms: int, end_ms: int, cps_limit: float, source_text: str | None = None,
) -> int | None:
    """Characters that fit in the line's on-screen time at the CPS limit,
    or None when the duration makes the number meaningless.

    When source_text is given, the result is never stricter than the
    source's own length (see SOURCE_FLOOR_RATIO) — see module note above.
    """
    duration_ms = end_ms - start_ms
    if duration_ms <= 0:
        return None
    budget = int(cps_limit * duration_ms / 1000.0)
    if source_text:
        source_floor = int(len(plain_text(source_text)) * SOURCE_FLOOR_RATIO)
        budget = max(budget, source_floor)
    return budget if budget >= MIN_CHAR_BUDGET else None


def load_prompt_characters(session: Session, project_id: int) -> list[ProjectCharacter]:
    characters = list(session.scalars(
        select(ProjectCharacter)
        .where(ProjectCharacter.project_id == project_id)
        .order_by(ProjectCharacter.id)
    ).all())
    return [
        c for c in characters
        if not (c.character_type and c.character_type.strip().lower() in _NON_SPEAKING_CHARACTER_TYPES)
    ]


def load_unmapped_gendered_speakers(session: Session, project_id: int) -> list[ProjectSpeaker]:
    speakers = list(session.scalars(
        select(ProjectSpeaker)
        .where(ProjectSpeaker.project_id == project_id)
        .where(ProjectSpeaker.gender.isnot(None))
        .where(ProjectSpeaker.character_id.is_(None))
        .order_by(ProjectSpeaker.name)
    ).all())
    return [speaker for speaker in speakers if speaker.gender]


def build_speaker_identity_map(
    session: Session, project_id: int,
) -> dict[str, tuple[str | None, str | None]]:
    """Map raw subtitle speaker name → (display name, gender) for per-line
    prompt annotations. Mapped speakers use their character's name/gender
    (speaker gender wins when set explicitly); unmapped speakers fall back
    to their own name/gender."""
    speakers = list(session.scalars(
        select(ProjectSpeaker)
        .where(ProjectSpeaker.project_id == project_id)
        .options(selectinload(ProjectSpeaker.character))
    ).all())

    identities: dict[str, tuple[str | None, str | None]] = {}
    for speaker in speakers:
        character = speaker.character
        if character is not None:
            name = character.name or speaker.name
            gender = speaker.gender or character.gender
        else:
            name = speaker.name
            gender = speaker.gender
        identities[speaker.name] = (name, gender)
    return identities


def _truncated_description(description: str | None) -> str | None:
    if not description:
        return None
    stripped = description.strip()
    if not stripped:
        return None
    if len(stripped) <= _DESCRIPTION_MAX_LEN:
        return stripped
    return stripped[:_DESCRIPTION_MAX_LEN].rstrip() + "…"


def build_character_block(characters: list[ProjectCharacter]) -> str:
    lines = []
    for character in characters:
        extras = [(key, value) for key, value in [
            ("gender", character.gender),
            ("social_position", character.social_position),
            ("personality", _truncated_description(character.description)),
            ("note", character.note),
        ] if value and value.strip()]
        if not extras:
            continue
        parts = [character.name] + [f"{key}: {value}" for key, value in extras]
        lines.append("- " + ", ".join(parts))
    return "\n".join(lines)


def build_unmapped_speaker_block(speakers: list[ProjectSpeaker]) -> str:
    lines = []
    for speaker in speakers:
        if speaker.gender and speaker.gender.strip():
            lines.append(f"- {speaker.name}, gender: {speaker.gender}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Glossary
# ---------------------------------------------------------------------------

# Categories that are always injected regardless of chunk content — names
# and places must stay consistent even when only inflected forms appear.
_ALWAYS_INCLUDED_CATEGORIES = {"name", "place"}
_MAX_GLOSSARY_LINES = 80


def load_glossary_terms(session: Session, project_id: int) -> list[ProjectGlossaryTerm]:
    return list(session.scalars(
        select(ProjectGlossaryTerm)
        .where(ProjectGlossaryTerm.project_id == project_id)
        .where(ProjectGlossaryTerm.is_active == 1)
        .order_by(ProjectGlossaryTerm.category, ProjectGlossaryTerm.source_term)
    ).all())


def build_glossary_block(terms: list[ProjectGlossaryTerm], chunk_texts: list[str]) -> str:
    """Names/places always; other categories only when the term occurs in
    the chunk's source text. Capped so a huge glossary can't flood the
    prompt — and the cap is applied to the two groups separately, because
    `terms` arrives ordered by category: a plain slice would drop 'name' and
    'place' (alphabetically late) in favour of matched 'honorific'/'item'
    rows, evicting exactly the terms that must never drift."""
    combined = " ".join(plain_text(t) for t in chunk_texts).casefold()

    always: list[ProjectGlossaryTerm] = []
    matched: list[ProjectGlossaryTerm] = []
    for term in terms:
        if term.category in _ALWAYS_INCLUDED_CATEGORIES:
            always.append(term)
        elif term.source_term.casefold() in combined:
            matched.append(term)

    selected = always[:_MAX_GLOSSARY_LINES]
    selected += matched[:max(0, _MAX_GLOSSARY_LINES - len(selected))]

    lines = []
    for term in selected:
        extras = []
        if term.vocative:
            extras.append(f"vocative: {term.vocative}")
        if term.gender:
            extras.append(f"gender: {term.gender}")
        if term.note:
            extras.append(term.note)
        suffix = f" ({'; '.join(extras)})" if extras else ""
        if term.source_term == term.target_term:
            lines.append(f'- "{term.source_term}" — keep as-is{suffix}')
        else:
            lines.append(f'- "{term.source_term}" => "{term.target_term}"{suffix}')
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Style bible + character voices + address pairs
# ---------------------------------------------------------------------------

@dataclass
class StyleContext:
    tone_summary: str | None = None
    register_notes: str | None = None
    honorific_policy: str | None = None
    voices: dict[str, tuple[str | None, str | None]] = field(default_factory=dict)  # character name → (voice, register)
    pairs: list[tuple[str, str, str]] = field(default_factory=list)  # (speaker, addressee, mode)

    @property
    def has_content(self) -> bool:
        return bool(self.tone_summary or self.register_notes or self.honorific_policy
                    or self.voices or self.pairs)


def load_style_context(session: Session, project_id: int) -> StyleContext:
    ctx = StyleContext()

    bible = session.scalar(
        select(ProjectStyleBible)
        .where(ProjectStyleBible.project_id == project_id)
        .order_by(ProjectStyleBible.version.desc())
        .limit(1)
    )
    if bible is not None:
        ctx.tone_summary = bible.tone_summary
        ctx.register_notes = bible.register_notes
        ctx.honorific_policy = bible.honorific_policy

    rows = session.execute(
        select(ProjectCharacter.name, ProjectCharacterStyle.voice_note, ProjectCharacterStyle.register)
        .join(ProjectCharacterStyle, ProjectCharacterStyle.project_character_id == ProjectCharacter.id)
        .where(ProjectCharacter.project_id == project_id)
    ).all()
    for name, voice_note, register in rows:
        ctx.voices[name] = (voice_note, register)

    ctx.pairs = canonical_address_pairs(session, project_id)

    return ctx


def build_style_block(
    style: StyleContext,
    raw_speakers_present: set[str],
    identities: dict[str, tuple[str | None, str | None]],
) -> str:
    """Project tone/policy plus voices and address pairs for the speakers
    that actually appear in the chunk."""
    parts: list[str] = []
    if style.tone_summary:
        parts.append(f"Tone: {style.tone_summary}")
    if style.register_notes:
        parts.append(f"Register: {style.register_notes}")
    if style.honorific_policy:
        parts.append(f"Honorifics: {style.honorific_policy}")

    # Character names for the raw speakers present in this chunk.
    present_characters = {
        identities.get(raw, (raw, None))[0]
        for raw in raw_speakers_present if raw
    }
    voice_lines = [
        f"- {name}: {voice or ''}" + (f" (register: {register})" if register else "")
        for name, (voice, register) in sorted(style.voices.items())
        if name in present_characters
    ]
    if voice_lines:
        parts.append("Character voices:\n" + "\n".join(voice_lines))

    present_identities = {
        (identities.get(raw, (raw, None))[0] or raw).casefold()
        for raw in raw_speakers_present if raw
    }
    pair_lines = [
        f"- {speaker} addresses {addressee}: {mode}"
        for speaker, addressee, mode in style.pairs
        if speaker.casefold() in present_identities
    ]
    if pair_lines:
        parts.append("Address (T-V) pairs:\n" + "\n".join(pair_lines))

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Episode metadata
# ---------------------------------------------------------------------------

def load_episode_context(session: Session, file: File) -> str | None:
    """One-line episode identification ('Episode 12: "Title"') for prompts,
    from the filename-parsed episode number + provider episode metadata."""
    if file.episode_number is None:
        return None
    episode = session.scalar(
        select(ProjectEpisode)
        .where(ProjectEpisode.project_id == file.project_id)
        .where(ProjectEpisode.episode_number == file.episode_number)
    )
    if episode is not None and episode.title:
        return f'Episode {file.episode_number}: "{episode.title}"'
    return f"Episode {file.episode_number}"


# ---------------------------------------------------------------------------
# File analysis (synopsis / scenes / tricky lines)
# ---------------------------------------------------------------------------

@dataclass
class AnalysisContext:
    synopsis: str | None = None
    scenes: list[dict] = field(default_factory=list)        # {from_line,to_line,summary,setting}
    tricky_lines: dict[int, str] = field(default_factory=dict)  # line_index → note


def load_analysis_context(session: Session, file_id: int) -> AnalysisContext | None:
    analysis = session.scalar(
        select(FileAnalysis).where(FileAnalysis.file_id == file_id)
    )
    if analysis is None:
        return None

    ctx = AnalysisContext(synopsis=analysis.synopsis)
    try:
        ctx.scenes = json.loads(analysis.scenes_json or "[]")
    except json.JSONDecodeError:
        ctx.scenes = []
    try:
        ctx.tricky_lines = {
            int(item["i"]): str(item["note"])
            for item in json.loads(analysis.tricky_lines_json or "[]")
            if isinstance(item, dict) and "i" in item and "note" in item
        }
    except (json.JSONDecodeError, TypeError, ValueError):
        ctx.tricky_lines = {}
    return ctx


def build_scene_block(analysis: AnalysisContext, from_line: int, to_line: int) -> str:
    """Episode synopsis plus scene summaries overlapping the chunk range."""
    parts: list[str] = []
    if analysis.synopsis:
        parts.append(f"Episode synopsis: {analysis.synopsis}")

    overlapping = [
        s for s in analysis.scenes
        if isinstance(s, dict)
        and isinstance(s.get("from_line"), int) and isinstance(s.get("to_line"), int)
        and s["from_line"] <= to_line and s["to_line"] >= from_line
    ]
    if overlapping:
        scene_lines = []
        for s in overlapping:
            setting = f" [{s['setting']}]" if s.get("setting") else ""
            scene_lines.append(f"- lines {s['from_line']}-{s['to_line']}{setting}: {s.get('summary', '')}")
        parts.append("Current scenes:\n" + "\n".join(scene_lines))

    return "\n".join(parts)


def build_tricky_notes_block(analysis: AnalysisContext, line_indices: list[int]) -> str:
    lines = [
        f"- line {li}: {analysis.tricky_lines[li]}"
        for li in line_indices if li in analysis.tricky_lines
    ]
    return "\n".join(lines)
