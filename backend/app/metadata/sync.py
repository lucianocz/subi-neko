"""Persist provider metadata (characters, episodes) into a project.

Used by the initial import and by the refresh-metadata endpoint. Upserts are
keyed by provider external_id (characters) / episode_number (episodes) and
never touch user-owned character fields (social_position, aliases, note) or
a gender the user has already set.
"""
from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Project, ProjectCharacter, ProjectEpisode
from app.metadata.base import Character, Episode

logger = logging.getLogger(__name__)


async def set_english_title(
    session: AsyncSession, project_id: int, title_english: str | None,
) -> bool:
    """Persist the provider's English title. A missing/blank provider value never
    clears or replaces an existing one. Returns True when the column changed.

    Core UPDATE, not an ORM flush: ``updated_at`` is left alone so a metadata
    top-up doesn't masquerade as project activity."""
    title = (title_english or "").strip()
    if not title:
        return False
    result = await session.execute(
        update(Project)
        .where(Project.id == project_id, or_(Project.name_english.is_(None), Project.name_english != title))
        .values(name_english=title, updated_at=Project.updated_at)
        .execution_options(synchronize_session=False)
    )
    return bool(result.rowcount)


async def upsert_characters(
    session: AsyncSession, project_id: int, characters: list[Character],
) -> tuple[int, int]:
    """Returns (created, updated). Matching is by external_id when present,
    else by case-insensitive name."""
    now = datetime.utcnow().isoformat()
    existing = list((await session.scalars(
        select(ProjectCharacter).where(ProjectCharacter.project_id == project_id)
    )).all())
    by_external = {c.external_id: c for c in existing if c.external_id}
    by_name = {c.name.casefold(): c for c in existing}

    created = updated = 0
    for char in characters:
        if not char.name or not char.name.strip():
            continue
        row = None
        if char.provider_id and char.provider_id in by_external:
            row = by_external[char.provider_id]
        elif char.name.casefold() in by_name:
            row = by_name[char.name.casefold()]

        role = char.role.value if char.role else None
        gender = char.gender.value if char.gender else None

        if row is None:
            row = ProjectCharacter(
                project_id=project_id,
                external_id=char.provider_id,
                name=char.name,
                role=role,
                gender=gender,
                description=char.description,
                voice_actor=char.voice_actor,
                character_type=char.character_type,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            if char.provider_id:
                by_external[char.provider_id] = row
            by_name[char.name.casefold()] = row
            created += 1
            continue

        # Provider-sourced fields only; user-owned fields (social_position,
        # aliases, note) and an already-set gender are preserved.
        changed = False
        if char.provider_id and not row.external_id:
            row.external_id = char.provider_id
            changed = True
        for attr, value in (
            ("role", role),
            ("description", char.description),
            ("voice_actor", char.voice_actor),
            ("character_type", char.character_type),
        ):
            if value and getattr(row, attr) != value:
                setattr(row, attr, value)
                changed = True
        if gender and not row.gender:
            row.gender = gender
            changed = True
        if changed:
            row.updated_at = now
            updated += 1

    return created, updated


async def upsert_episodes(
    session: AsyncSession, project_id: int, episodes: list[Episode],
) -> tuple[int, int]:
    """Returns (created, updated), keyed by episode_number."""
    now = datetime.utcnow().isoformat()
    existing = {
        row.episode_number: row
        for row in (await session.scalars(
            select(ProjectEpisode).where(ProjectEpisode.project_id == project_id)
        )).all()
    }

    created = updated = 0
    for episode in episodes:
        row = existing.get(episode.number)
        if row is None:
            session.add(ProjectEpisode(
                project_id=project_id,
                episode_number=episode.number,
                title=episode.title,
                title_native=episode.title_native,
                air_date=episode.air_date,
                created_at=now,
                updated_at=now,
            ))
            created += 1
            continue
        if (row.title, row.title_native, row.air_date) != (episode.title, episode.title_native, episode.air_date):
            row.title = episode.title or row.title
            row.title_native = episode.title_native or row.title_native
            row.air_date = episode.air_date or row.air_date
            row.updated_at = now
            updated += 1

    return created, updated
