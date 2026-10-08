from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.metadata.base import CharacterRole, MediaType
from app.metadata.registry import get_provider, list_providers

router = APIRouter(prefix="/metadata", tags=["metadata"])


class SearchResultOut(BaseModel):
    provider_id: str
    title: str
    title_native: str | None
    year: int | None
    media_type: MediaType
    episode_count: int | None = None
    title_english: str | None = None


class SeriesDetailsOut(BaseModel):
    provider_id: str
    title: str
    title_native: str | None
    title_synonyms: list[str]
    year: int | None
    media_type: MediaType
    description: str | None
    episode_count: int | None


class CharacterOut(BaseModel):
    provider_id: str | None
    name: str
    name_native: str | None
    role: CharacterRole | None
    gender: str | None
    description: str | None


@router.get("/providers")
async def get_providers() -> list[str]:
    return list_providers()


@router.get("/search", response_model=list[SearchResultOut])
async def search(
    provider: str = Query(...),
    q: str = Query(..., min_length=1),
) -> list[SearchResultOut]:
    try:
        p = get_provider(provider)
    except KeyError as e:
        raise HTTPException(status_code=400, detail=str(e))
    try:
        results = await p.search(q)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Provider error: {e}")
    return [
        SearchResultOut(
            provider_id=r.provider_id,
            title=r.title,
            title_native=r.title_native,
            year=r.year,
            media_type=r.media_type,
            episode_count=r.episode_count,
            title_english=r.title_english,
        )
        for r in results
    ]


@router.get("/{provider}/{provider_id}/details", response_model=SeriesDetailsOut)
async def get_details(provider: str, provider_id: str) -> SeriesDetailsOut:
    try:
        p = get_provider(provider)
    except KeyError as e:
        raise HTTPException(status_code=400, detail=str(e))
    try:
        d = await p.get_details(provider_id)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Provider error: {e}")
    return SeriesDetailsOut(
        provider_id=d.provider_id,
        title=d.title,
        title_native=d.title_native,
        title_synonyms=d.title_synonyms,
        year=d.year,
        media_type=d.media_type,
        description=d.description,
        episode_count=d.episode_count,
    )


@router.get("/{provider}/{provider_id}/characters", response_model=list[CharacterOut])
async def get_characters(provider: str, provider_id: str) -> list[CharacterOut]:
    try:
        p = get_provider(provider)
    except KeyError as e:
        raise HTTPException(status_code=400, detail=str(e))
    try:
        chars = await p.get_characters(provider_id)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Provider error: {e}")
    return [
        CharacterOut(
            provider_id=c.provider_id,
            name=c.name,
            name_native=c.name_native,
            role=c.role,
            gender=c.gender,
            description=c.description,
        )
        for c in chars
    ]
