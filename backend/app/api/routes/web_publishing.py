import asyncio
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.archive_guard import reject_if_archived
from app.jobs.manager import job_manager
from app.publishing import service
from app.publishing import description
from app.publishing.schemas import PublishRequest, SaveRequest, WebConfig
from app.publishing.tmdb import TmdbClient
from app.publishing.web_client import PublishingError, require_available

router = APIRouter(
    prefix="/projects/{project_id}/web",
    tags=["web"],
    dependencies=[Depends(reject_if_archived)],
)
metadata_router = APIRouter(prefix="/web/tmdb", tags=["web"])


async def call(fn, *args, **kwargs):
    try:
        require_available()
        return await asyncio.to_thread(fn, *args, **kwargs)
    except PublishingError as exc:
        raise HTTPException(
            exc.status, detail={"code": exc.code, "message": exc.message}
        ) from None


@router.get("")
async def get_state(project_id: int):
    return await call(service.state, project_id)


@router.put("")
async def save_config(project_id: int, body: SaveRequest):
    return await call(service.save, project_id, body)


@router.get("/remote")
async def remote_state(project_id: int):
    return await call(service.remote_state, project_id)


@router.post("/translate-description")
async def translate_description(project_id: int, body: SaveRequest):
    return await call(description.translate, project_id, body)


@router.post("/publish", status_code=202)
async def publish(project_id: int, body: PublishRequest):
    try:
        return await service.start(project_id, body, job_manager.enqueue)
    except PublishingError as exc:
        raise HTTPException(
            exc.status, detail={"code": exc.code, "message": exc.message}
        ) from None


@metadata_router.get("/search")
async def search(q: str = Query(min_length=1, max_length=255)):
    return await call(lambda: TmdbClient().search(q))


@metadata_router.get("/details")
async def details(
    media_type: Literal["tv", "movie"],
    title_id: str = Query(pattern=r"^\d+$"),
    refresh: bool = False,
):
    return await call(lambda: TmdbClient().details(media_type, title_id, refresh))


@metadata_router.get("/catalog")
async def catalog(
    title_id: str = Query(pattern=r"^\d+$"),
    group_id: str | None = Query(default=None, pattern=r"^[A-Za-z0-9_-]+$"),
    season_number: int | None = Query(default=None, ge=0),
    refresh: bool = False,
):
    return await call(
        lambda: TmdbClient().catalog(title_id, group_id, season_number, refresh)
    )


@metadata_router.post("/lookup")
async def lookup(body: WebConfig):
    # Explicit read-only lookup; saving the wizard never invokes a remote PUT.
    return await call(service.lookup, body)
