from datetime import datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.database import Base
from app.db.models import Project, ProjectCharacter
from app.metadata.base import Character, CharacterGender, CharacterRole
from app.metadata.sync import upsert_characters


@pytest.mark.asyncio
async def test_character_import_preserves_full_provider_and_manual_metadata():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    now = datetime.utcnow().isoformat()
    long_description = "Relationship evidence. " + "x" * 1000

    async with factory() as session:
        project = Project(name="P", source_directory="sync", anime_provider="anidb",
                          anime_external_id="1", created_at=now, updated_at=now)
        session.add(project)
        await session.flush()
        await upsert_characters(session, project.id, [Character(
            name="Aria", provider_id="77", role=CharacterRole.MAIN,
            gender=CharacterGender.FEMALE, description=long_description,
            voice_actor="Jane Actor", character_type="Human")])
        await session.commit()

        row = await session.scalar(select(ProjectCharacter))
        assert row.description == long_description
        assert row.role == "MAIN" and row.gender == "female"
        assert row.voice_actor == "Jane Actor" and row.character_type == "Human"

        row.social_position = "Princess"
        row.aliases = "Hero"
        row.note = "User note"
        row.gender = "other"
        await session.commit()

        refreshed_description = long_description + " Updated."
        await upsert_characters(session, project.id, [Character(
            name="Aria", provider_id="77", role=CharacterRole.SUPPORTING,
            gender=CharacterGender.MALE, description=refreshed_description,
            voice_actor=None, character_type=None)])
        await session.commit()
        await session.refresh(row)
        assert row.description == refreshed_description
        assert row.role == "SUPPORTING"
        assert row.social_position == "Princess"
        assert row.aliases == "Hero"
        assert row.note == "User note"
        assert row.gender == "other"
        assert row.voice_actor == "Jane Actor"  # missing optional data is non-destructive
        assert row.character_type == "Human"

    await engine.dispose()
