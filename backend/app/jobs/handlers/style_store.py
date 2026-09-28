"""Shared persistence helpers for LLM-produced style/glossary data.

All writers are ADDITIVE by design: LLM output never overwrites a row the
user edited (locked=1) and never replaces an existing glossary term — new
knowledge is inserted (or fills fields still empty on unlocked rows, e.g.
the vocative of a metadata-seeded name), corrections happen through the
API/UI.
"""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    ProjectAddressPair,
    ProjectCharacter,
    ProjectCharacterStyle,
    ProjectGlossaryTerm,
    ProjectSpeaker,
)
from app.llm.schemas import AddressPairOut, CharacterVoiceOut, GlossaryTermOut

logger = logging.getLogger(__name__)

_VALID_CATEGORIES = {"name", "place", "technique", "item", "honorific", "catchphrase", "other"}
_VALID_MODES = {"tykani", "vykani", "mixed"}


class AddressIdentityResolver:
    """Resolve address-pair names through the project's established speaker
    mappings. Unknown labels deliberately remain distinct fallback identities;
    no fuzzy or alias matching is performed here."""

    def __init__(self, session: Session, project_id: int):
        characters = list(session.scalars(
            select(ProjectCharacter).where(ProjectCharacter.project_id == project_id)
        ).all())
        self._characters = {
            character.name.casefold(): character.name
            for character in characters
            if character.name and character.name.strip()
        }
        characters_by_id = {
            character.id: character.name
            for character in characters
        }
        self._speakers = {
            speaker.name.casefold(): characters_by_id.get(speaker.character_id)
            for speaker in session.scalars(
                select(ProjectSpeaker).where(ProjectSpeaker.project_id == project_id)
            ).all()
            if speaker.name and speaker.name.strip()
        }

    def resolve(self, name: str) -> tuple[str, str]:
        cleaned = name.strip()
        folded = cleaned.casefold()
        mapped = self._speakers.get(folded)
        if mapped:
            return mapped.casefold(), mapped
        canonical = self._characters.get(folded)
        if canonical:
            return canonical.casefold(), canonical
        return folded, cleaned


def canonical_address_pairs(
    session: Session,
    project_id: int,
) -> list[tuple[str, str, str]]:
    """Return one safe, canonical instruction per directed relationship.

    Locked/manual rows outrank generated rows, then the oldest established row
    wins. Equally authoritative user-managed rows that conflict are ambiguous,
    so the relationship is omitted rather than injecting both instructions.
    """
    resolver = AddressIdentityResolver(session, project_id)
    grouped: dict[tuple[str, str], list[ProjectAddressPair]] = {}
    for row in session.scalars(
        select(ProjectAddressPair)
        .where(ProjectAddressPair.project_id == project_id)
        .order_by(ProjectAddressPair.id)
    ).all():
        speaker_key, _ = resolver.resolve(row.speaker_name)
        addressee_key, _ = resolver.resolve(row.addressee_name)
        grouped.setdefault((speaker_key, addressee_key), []).append(row)

    result: list[tuple[str, str, str]] = []
    for rows in grouped.values():
        best_rank = max((1 if row.locked else 0, 1 if row.origin == "manual" else 0)
                        for row in rows)
        authoritative = [
            row for row in rows
            if (1 if row.locked else 0, 1 if row.origin == "manual" else 0) == best_rank
        ]
        if best_rank != (0, 0) and len({row.mode for row in authoritative}) > 1:
            logger.warning("Omitting ambiguous user-managed address relationship for project %s",
                           project_id)
            continue
        chosen = authoritative[0]
        _, speaker = resolver.resolve(chosen.speaker_name)
        _, addressee = resolver.resolve(chosen.addressee_name)
        result.append((speaker, addressee, chosen.mode))
    return result


def insert_new_glossary_terms(
    session: Session,
    project_id: int,
    terms: list[GlossaryTermOut],
    origin: str,
    now: str,
) -> int:
    """Insert terms whose source term doesn't exist yet (case-insensitive,
    ANY category — otherwise the LLM re-suggests seeded names under a
    different category and the glossary fills with near-duplicates).
    Existing unlocked rows are never replaced, but EMPTY fields (gender,
    vocative, note) are filled in — metadata-seeded character names start
    without a vocative/note and the LLM supplies them here."""
    existing = {
        row.source_term.casefold(): row
        for row in session.scalars(
            select(ProjectGlossaryTerm).where(ProjectGlossaryTerm.project_id == project_id)
        ).all()
    }

    inserted = 0
    for term in terms:
        source = (term.source or "").strip()
        target = (term.target or "").strip()
        if not source or not target:
            continue
        category = term.category if term.category in _VALID_CATEGORIES else "other"
        key = source.casefold()
        row = existing.get(key)
        if row is not None:
            if not row.locked and _fill_missing_term_fields(row, term, now):
                logger.debug("Filled missing glossary fields for %r", row.source_term)
            continue
        row = ProjectGlossaryTerm(
            project_id=project_id,
            source_term=source,
            target_term=target,
            category=category,
            gender=(term.gender or None),
            vocative=(term.vocative or None),
            note=(term.note or None),
            origin=origin,
            locked=0,
            is_active=1,
            created_at=now,
            updated_at=now,
        )
        session.add(row)
        existing[key] = row
        inserted += 1
    return inserted


def _fill_missing_term_fields(row: ProjectGlossaryTerm, term: GlossaryTermOut, now: str) -> bool:
    """Additive field fill on an existing unlocked term: only fields the row
    doesn't have yet are taken from the LLM suggestion. Returns True if
    anything was written."""
    changed = False
    for field in ("gender", "vocative", "note"):
        value = (getattr(term, field) or "").strip()
        if value and not getattr(row, field):
            setattr(row, field, value)
            changed = True
    if changed:
        row.updated_at = now
    return changed


def upsert_address_pairs(
    session: Session,
    project_id: int,
    pairs: list[AddressPairOut],
    origin: str,
    now: str,
    *,
    update_existing_mode: bool = True,
) -> int:
    """Persist pairs by canonical directed identity.

    ``update_existing_mode=False`` is used by incremental episode updates:
    generated translations may add a relationship, but can never revise an
    established convention. Initial-generation callers retain the historical
    ability to refine an unlocked row.
    """
    resolver = AddressIdentityResolver(session, project_id)
    rows: dict[tuple[str, str], ProjectAddressPair] = {}
    for row in session.scalars(
        select(ProjectAddressPair)
        .where(ProjectAddressPair.project_id == project_id)
        .order_by(ProjectAddressPair.id)
    ).all():
        speaker_key, _ = resolver.resolve(row.speaker_name)
        addressee_key, _ = resolver.resolve(row.addressee_name)
        key = (speaker_key, addressee_key)
        current = rows.get(key)
        if current is None or (
            (row.locked, row.origin == "manual")
            > (current.locked, current.origin == "manual")
        ):
            rows[key] = row

    written = 0
    for pair in pairs:
        speaker = (pair.speaker or "").strip()
        addressee = (pair.addressee or "").strip()
        mode = (pair.mode or "").strip().lower()
        if not speaker or not addressee or mode not in _VALID_MODES:
            continue
        speaker_key, canonical_speaker = resolver.resolve(speaker)
        addressee_key, canonical_addressee = resolver.resolve(addressee)
        key = (speaker_key, addressee_key)
        row = rows.get(key)
        if row is None:
            row = ProjectAddressPair(
                project_id=project_id,
                speaker_name=canonical_speaker,
                addressee_name=canonical_addressee,
                mode=mode,
                origin=origin,
                locked=0,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            rows[key] = row
            written += 1
        elif update_existing_mode and not row.locked and row.mode != mode:
            row.mode = mode
            row.origin = origin
            row.updated_at = now
            written += 1
    return written


def upsert_character_voices(
    session: Session,
    project_id: int,
    voices: list[CharacterVoiceOut],
    origin: str,
    now: str,
) -> int:
    """Attach voice notes to characters matched by name (case-insensitive);
    existing unlocked styles are updated, locked ones left alone."""
    characters = {
        c.name.casefold(): c
        for c in session.scalars(
            select(ProjectCharacter).where(ProjectCharacter.project_id == project_id)
        ).all()
    }
    styles = {
        s.project_character_id: s
        for s in session.scalars(
            select(ProjectCharacterStyle)
            .join(ProjectCharacter, ProjectCharacter.id == ProjectCharacterStyle.project_character_id)
            .where(ProjectCharacter.project_id == project_id)
        ).all()
    }

    written = 0
    for voice in voices:
        character = characters.get((voice.name or "").strip().casefold())
        if character is None:
            continue
        style = styles.get(character.id)
        if style is None:
            style = ProjectCharacterStyle(
                project_character_id=character.id,
                voice_note=voice.voice_note or None,
                register=voice.register or None,
                origin=origin,
                locked=0,
                created_at=now,
                updated_at=now,
            )
            session.add(style)
            styles[character.id] = style
            written += 1
        elif not style.locked:
            style.voice_note = voice.voice_note or style.voice_note
            style.register = voice.register or style.register
            style.origin = origin
            style.updated_at = now
            written += 1
    return written
