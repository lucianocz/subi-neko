"""Automatic speaker→character mapping — replaces the manual mapping gate.

Two stages:
  0. Deterministic: normalized-name/alias matching against the roster
     (confidence 1.0, origin "fuzzy") and extra-detection patterns
     ("Boy A", "Crowd", "TV"…) that mark non-mappable speakers.
  1. One cheap-model structured LLM call for whatever remains, using each
     speaker's line count and sample lines as evidence. All results are
     written immediately with their confidence; low-confidence matches
     surface in the translation-context panel for human correction.

An LLM *error* fails the job (speaker_mapping_status stays "aggregated" so
the project blocks visibly at the context gate and the failure is retryable);
Stage 0 results are committed beforehand and survive. An LLM that merely
returns no match for some speakers is still a success — unmapped speakers
are an attention concern, not a failure. If the LLM is permanently
unavailable, manually mapping (or marking as extra) every speaker and
retrying succeeds deterministically: nothing is left for Stage 1.

Manually corrected rows (match_origin="manual") are never overwritten.
Ends by setting the project's speaker_mapping_status to "complete".
"""
from __future__ import annotations

import json
import logging
import re
import unicodedata
from datetime import datetime
from typing import Any

from sqlalchemy import select

from app.core.database import SyncSessionLocal
from app.db.models import Project, ProjectCharacter, ProjectSpeaker
from app.jobs.context import JobContext, JobResult, ProgressFn
from app.jobs.handlers.character_context import allocate_character_descriptions
from app.jobs.registry import register_job_handler
from app.llm import client as llm_client
from app.llm.schemas import MappingResponse

logger = logging.getLogger(__name__)

_HONORIFIC_RE = re.compile(
    r"[-\s](san|kun|chan|sama|senpai|sensei|dono|tan|han)$", re.IGNORECASE)
_PARENTHETICAL_RE = re.compile(r"\s*[(\[].*?[)\]]\s*")
_POSSESSIVE_RE = re.compile(r"'s\s+(voice|thoughts?|narration)$", re.IGNORECASE)
_NON_WORD_RE = re.compile(r"[^\w\s]")

# Speaker labels that denote unnamed extras / non-character sources — never
# mapped to roster characters, never sent to the LLM.
_GENERIC_EXTRA_PATTERNS: list[tuple[re.Pattern, str | None]] = [
    (re.compile(r"^(boy|man|guy|male student|male)\s*[a-d1-9]?$", re.IGNORECASE), "male"),
    (re.compile(r"^(girl|woman|lady|female student|female)\s*[a-d1-9]?$", re.IGNORECASE), "female"),
    (re.compile(r"^(student|soldier|villager|guard|guest|customer|clerk|waiter|"
                r"reporter|announcer|doctor|nurse|teacher|police(man)?|thug|bandit|"
                r"knight|maid|servant|stranger|passerby|voice)\s*[a-d1-9]?$", re.IGNORECASE), None),
]

# These labels are intrinsically collective or non-character sources. They
# remain extras even if a metadata roster happens to contain the same text.
_EXPLICIT_EXTRA_PATTERNS: list[tuple[re.Pattern, str | None]] = [
    (re.compile(r"^(crowd|everyone|all|both|others|students|children|kids|mob|audience)$",
                re.IGNORECASE), None),
    (re.compile(r"^(tv|radio|pa|announcement|intercom|phone|speaker|narration|narrator|"
                r"text|sign|note|caption|subtitle)s?$", re.IGNORECASE), None),
]


def normalize_name(name: str) -> str:
    """casefold + strip diacritics, honorifics, parentheticals, possessives
    and punctuation."""
    text = (name or "").strip()
    text = _PARENTHETICAL_RE.sub(" ", text)
    text = _POSSESSIVE_RE.sub("", text)
    text = _HONORIFIC_RE.sub("", text)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = _NON_WORD_RE.sub(" ", text.casefold())
    return " ".join(text.split())


def _detect_from_patterns(
    name: str, patterns: list[tuple[re.Pattern, str | None]],
) -> tuple[bool, str | None]:
    normalized = normalize_name(name)
    for pattern, gender in patterns:
        if pattern.match(normalized):
            return True, gender
    return False, None


def detect_extra(name: str) -> tuple[bool, str | None]:
    """Compatibility helper covering both explicit and generic extras."""
    explicit = _detect_from_patterns(name, _EXPLICIT_EXTRA_PATTERNS)
    return explicit if explicit[0] else _detect_from_patterns(name, _GENERIC_EXTRA_PATTERNS)


# LLM confidence for cryptic labels is capped below the auto-accept
# threshold: a label like "20" or "2" often collects lines from several
# characters, so a confident single-character mapping can't be trusted no
# matter how sure the model sounds. The cap keeps the mapping as a
# suggestion but surfaces it in the context panel for human confirmation.
_CRYPTIC_CONFIDENCE_CAP = 0.5


def is_cryptic_label(name: str) -> bool:
    """True for speaker labels that carry no recognizable name (pure
    numbers, one/two-character abbreviations)."""
    normalized = normalize_name(name)
    if not normalized:
        return True
    if normalized.replace(" ", "").isdigit():
        return True
    return len(normalized) < 3


def _character_token_index(characters: list[ProjectCharacter]) -> dict[str, list[ProjectCharacter]]:
    """Map normalized full names AND individual name tokens (given/family
    name) + aliases to characters. Ambiguous tokens map to multiple."""
    index: dict[str, list[ProjectCharacter]] = {}

    def _add(key: str, character: ProjectCharacter) -> None:
        if not key:
            return
        index.setdefault(key, [])
        if character not in index[key]:
            index[key].append(character)

    for character in characters:
        full = normalize_name(character.name)
        _add(full, character)
        tokens = full.split()
        if len(tokens) > 1:
            for token in tokens:
                if len(token) >= 3:  # skip initials/particles
                    _add(token, character)
        for alias in (character.aliases or "").split(","):
            _add(normalize_name(alias), character)

    return index


def fuzzy_match(name: str, index: dict[str, list[ProjectCharacter]]) -> ProjectCharacter | None:
    """Unambiguous normalized-name/token match, or None."""
    normalized = normalize_name(name)
    if not normalized:
        return None
    candidates = index.get(normalized)
    if candidates and len(candidates) == 1:
        return candidates[0]
    return None


_VALID_GENDERS = {"male", "female"}

_MAPPING_COMPLETION_BASE = 1024
_MAPPING_COMPLETION_PER_SPEAKER = 160
_MAPPING_COMPLETION_CAP = 16000


def mapping_completion_budget(unresolved_count: int, configured_max: int) -> int:
    """Bound response space by expected output entries, not roster size."""
    requested = _MAPPING_COMPLETION_BASE + max(0, unresolved_count) * _MAPPING_COMPLETION_PER_SPEAKER
    return min(max(1, configured_max), _MAPPING_COMPLETION_CAP, requested)


@register_job_handler("infer_character_mapping")
def infer_character_mapping(
    payload: dict[str, Any],
    ctx: JobContext,
    progress: ProgressFn,
) -> JobResult:
    project_id: int = payload["project_id"]
    model: str = payload.get("model") or ctx.options.openai_model_cheap
    now = datetime.utcnow().isoformat()

    progress(0.05, "Loading speakers and roster")

    with SyncSessionLocal() as session:
        project = session.get(Project, project_id)
        if project is None:
            return JobResult(status="failed", result=None,
                             error_code="PROJECT_NOT_FOUND",
                             error_message=f"Project id={project_id} not found")
        project_name = project.name

        speakers = list(session.scalars(
            select(ProjectSpeaker)
            .where(ProjectSpeaker.project_id == project_id)
            .order_by(ProjectSpeaker.line_count.desc())
        ).all())
        characters = list(session.scalars(
            select(ProjectCharacter)
            .where(ProjectCharacter.project_id == project_id)
            .order_by(ProjectCharacter.id)
        ).all())

        # --- Stage 0: deterministic ---------------------------------------
        index = _character_token_index(characters)
        fuzzy_matched = 0
        extras = 0
        unresolved: list[ProjectSpeaker] = []

        for speaker in speakers:
            if speaker.match_origin == "manual":
                continue  # user decision is final

            is_extra, extra_gender = _detect_from_patterns(
                speaker.name, _EXPLICIT_EXTRA_PATTERNS)
            if is_extra:
                speaker.is_extra = 1
                speaker.character_id = None
                speaker.match_origin = "fuzzy"
                speaker.match_confidence = 1.0
                speaker.match_rationale = "unnamed extra / non-character source"
                if extra_gender and not speaker.gender:
                    speaker.gender = extra_gender
                speaker.updated_at = now
                extras += 1
                continue

            character = fuzzy_match(speaker.name, index)
            if character is not None:
                speaker.character_id = character.id
                speaker.match_origin = "fuzzy"
                speaker.match_confidence = 1.0
                speaker.match_rationale = "exact name match"
                if character.gender and not speaker.gender:
                    speaker.gender = character.gender
                speaker.updated_at = now
                fuzzy_matched += 1
                continue

            # Broad role labels are extras only after an exact unambiguous
            # canonical-name/alias match had a chance to win.
            is_extra, extra_gender = _detect_from_patterns(
                speaker.name, _GENERIC_EXTRA_PATTERNS)
            if is_extra:
                speaker.is_extra = 1
                speaker.character_id = None
                speaker.match_origin = "fuzzy"
                speaker.match_confidence = 1.0
                speaker.match_rationale = "unnamed extra / non-character source"
                if extra_gender and not speaker.gender:
                    speaker.gender = extra_gender
                speaker.updated_at = now
                extras += 1
                continue

            unresolved.append(speaker)

        unresolved_snapshot = [
            {
                "id": s.id,
                "name": s.name,
                "line_count": s.line_count or 0,
                "samples": json.loads(s.sample_lines_json or "[]"),
            }
            for s in unresolved
        ]
        relevant_character_ids = {
            s.character_id for s in speakers if s.character_id is not None
        }
        speaking_characters = [
            c for c in characters
            if not (c.character_type and c.character_type.strip().lower()
                    in ("organization", "vessel"))
        ]
        descriptions = allocate_character_descriptions(
            speaking_characters,
            ctx.options.mapping_character_description_max,
            ctx.options.mapping_character_description_budget,
            relevant_character_ids,
        )
        roster_snapshot = [
            {
                "external_id": c.external_id or f"internal:{c.id}",
                "id": c.id,
                "name": c.name,
                "gender": c.gender,
                "role": c.role,
                "aliases": c.aliases,
                "voice_actor": c.voice_actor,
                "character_type": c.character_type,
                "social_position": c.social_position,
                "note": c.note,
                "description": descriptions.get(c.id),
            }
            for c in speaking_characters
        ]
        session.commit()

    # --- Stage 1: LLM for the rest -----------------------------------------
    llm_matched = 0
    llm_gendered = 0

    if unresolved_snapshot and not roster_snapshot:
        # Nothing to match against: the mapping stage completes as a no-op.
        # The context panel flags the empty roster so the user doesn't read
        # "complete" as "everything is mapped".
        logger.warning(
            "Project %d has %d unresolved speaker(s) but an empty character "
            "roster — mapping inference skipped",
            project_id, len(unresolved_snapshot),
        )

    if unresolved_snapshot and roster_snapshot:
        progress(0.4, f"LLM inference for {len(unresolved_snapshot)} speaker(s)")

        lang_neutral_prompt = ctx.options.resolved_mapping_prompt().strip()
        speaker_lines = []
        for s in unresolved_snapshot:
            samples = "; ".join(f'"{line}"' for line in s["samples"][:5])
            speaker_lines.append(f'- "{s["name"]}" ({s["line_count"]} lines) — {samples or "no sample lines"}')
        roster_lines = [
            f'- id={c["external_id"]}: {c["name"]}'
            + (f' (gender: {c["gender"]})' if c["gender"] else "")
            + (f' (role: {c["role"]})' if c["role"] else "")
            + (f' (aliases: {c["aliases"]})' if c["aliases"] else "")
            + (f' (type: {c["character_type"]})' if c["character_type"] else "")
            + (f' (VA: {c["voice_actor"]})' if c["voice_actor"] else "")
            + (f' (social position: {c["social_position"]})' if c["social_position"] else "")
            + (f' (note: {c["note"]})' if c["note"] else "")
            + (f' — {c["description"]}' if c["description"] else "")
            for c in roster_snapshot
        ]
        user_message = (
            f"## Series\n{project_name}\n\n"
            "## Speakers\n" + "\n".join(speaker_lines) + "\n\n"
            "## Roster\n" + "\n".join(roster_lines)
        )

        try:
            response, _stats = llm_client.complete(
                task="analyze",
                model=model,
                system=lang_neutral_prompt.strip(),
                user=user_message,
                schema=MappingResponse,
                options=ctx.options,
                max_completion_tokens=mapping_completion_budget(
                    len(unresolved_snapshot), ctx.options.llm_max_completion_tokens),
                project_id=project_id,
            )
        except llm_client.LlmError as exc:
            # Fail the job: speaker_mapping_status stays "aggregated", the
            # project blocks at the context gate, and the failure is
            # retryable. Stage 0 results were committed above and survive.
            logger.warning("Mapping inference LLM call failed for project %d: %s",
                           project_id, exc.message)
            return JobResult(
                status="failed",
                result={
                    "speakers_total": len(speakers),
                    "fuzzy_matched": fuzzy_matched,
                    "extras_detected": extras,
                },
                error_code=exc.code or "MAPPING_LLM_FAILED",
                error_message=exc.message,
            )

        if response is not None:
            by_external = {c["external_id"]: c for c in roster_snapshot}
            speaker_ids = {s["name"]: s["id"] for s in unresolved_snapshot}
            with SyncSessionLocal() as session:
                for match in response.matches:
                    speaker_id = speaker_ids.get(match.speaker)
                    if speaker_id is None:
                        continue
                    speaker = session.get(ProjectSpeaker, speaker_id)
                    if speaker is None or speaker.match_origin == "manual":
                        continue
                    confidence = max(0.0, min(1.0, match.confidence))
                    rationale = match.rationale
                    if match.character_external_id and is_cryptic_label(match.speaker):
                        confidence = min(confidence, _CRYPTIC_CONFIDENCE_CAP)
                        rationale = (f"{rationale} [confidence capped: cryptic speaker "
                                     "label may cover multiple characters]")
                    character = by_external.get(match.character_external_id or "")
                    speaker.character_id = character["id"] if character else None
                    speaker.match_origin = "llm"
                    speaker.match_confidence = confidence
                    speaker.match_rationale = rationale
                    gender = (match.inferred_gender or "").strip().lower()
                    cryptic = is_cryptic_label(match.speaker)
                    reliable_character = (
                        character is not None
                        and confidence >= ctx.options.auto_mapping_accept_threshold
                    )
                    if not speaker.gender and not cryptic:
                        # Canonical provider/user metadata is stronger than
                        # dialogue-based inference for a reliable match.
                        if reliable_character and character.get("gender"):
                            speaker.gender = character["gender"]
                        elif gender in _VALID_GENDERS:
                            speaker.gender = gender
                            llm_gendered += 1
                    speaker.updated_at = now
                    if character:
                        llm_matched += 1
                session.commit()

    progress(0.9, "Finalizing mapping")

    with SyncSessionLocal() as session:
        project = session.get(Project, project_id)
        if project is not None:
            project.speaker_mapping_status = "complete"
            project.updated_at = now
            session.commit()

    progress(1.0, "Done")
    return JobResult(
        status="succeeded",
        result={
            "speakers_total": len(speakers),
            "fuzzy_matched": fuzzy_matched,
            "extras_detected": extras,
            "llm_matched": llm_matched,
            "llm_gender_inferred": llm_gendered,
        },
        error_code=None,
        error_message=None,
    )
