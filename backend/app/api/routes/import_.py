import asyncio
import logging
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.db.models import Project, ProjectEpisode
from app.jobs.manager import job_manager
from app.metadata import anibridge
from app.metadata.registry import get_provider
from app.metadata.sync import set_english_title, upsert_characters, upsert_episodes
from app.orchestrator.orchestrator import orchestrate_project
from app.publishing import description, import_mapping
from app.publishing.import_mapping import ImportIntent, TmdbImportMapping
from app.publishing.web_client import PublishingError

router = APIRouter(prefix="/import", tags=["import"])
logger = logging.getLogger(__name__)


class DirectoryOut(BaseModel):
    name: str
    file_count: int


class ImportRequest(BaseModel):
    directory_name: str
    provider: str
    provider_id: str
    anime_title: str
    anime_title_native: str | None = None
    anime_title_english: str | None = None
    anime_year: int | None = None
    # Optional so legacy clients (and an explicit "skip TMDB mapping") still work.
    tmdb: TmdbImportMapping | None = None
    # Recorded with a confirmed TMDB mapping only; importing never publishes.
    intent: ImportIntent | None = None
    # Client-generated id, stable across retries of the same import: a repeat
    # recovers the project it created instead of failing as a duplicate.
    request_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{8,64}$")


class ImportResultOut(BaseModel):
    id: int
    name: str
    source_directory: str
    status: str
    # False when the request_id matched an already imported project (a retry).
    created: bool = True
    # Import success is reported separately from publication: nothing is ever
    # published by this endpoint, so there is no publication status here.
    web_publish_intent: ImportIntent | None = None
    tmdb_mapped: bool = False
    tmdb_episodes: int = 0
    expected_episode_count: int | None = None
    web_config_version: int | None = None


class DescriptionIn(BaseModel):
    description: str = Field(min_length=1, max_length=10000)


def _publishing_http_error(exc: PublishingError) -> HTTPException:
    return HTTPException(exc.status, detail={"code": exc.code, "message": exc.message})


@router.get("/tmdb-suggestion")
async def tmdb_suggestion(
    provider: Literal["anidb", "anilist"],
    provider_id: str = Query(min_length=1, max_length=64),
):
    """AniBridge's suggestion for the TMDB title/season of a source identity.

    Advisory and never an error: an unavailable dataset is reported as
    ``status="unavailable"`` so the wizard falls back to manual TMDB search."""
    return await asyncio.to_thread(anibridge.suggest, provider, provider_id)


@router.post("/translate-description")
async def translate_description(body: DescriptionIn):
    """Translate an English synopsis (no project exists yet at import time)."""
    try:
        return await asyncio.to_thread(description.translate_text, body.description)
    except PublishingError as exc:
        raise _publishing_http_error(exc) from None


@router.get("/directories", response_model=list[DirectoryOut])
async def list_import_directories() -> list[DirectoryOut]:
    import_root = settings.import_root
    if not import_root.exists():
        return []

    async with AsyncSessionLocal() as session:
        existing: set[str] = set(
            (await session.scalars(select(Project.source_directory))).all()
        )

    dirs: list[DirectoryOut] = []
    for entry in sorted(import_root.iterdir()):
        if not entry.is_dir():
            continue
        if entry.name in existing:
            continue
        file_count = sum(1 for f in entry.rglob("*") if f.is_file())
        dirs.append(DirectoryOut(name=entry.name, file_count=file_count))

    return dirs


async def _replay(project: Project) -> ImportResultOut:
    """The result of an import that already happened (idempotent retry)."""
    async with AsyncSessionLocal() as session:
        episodes = await session.scalar(
            select(func.count()).select_from(ProjectEpisode).where(
                ProjectEpisode.project_id == project.id,
                ProjectEpisode.tmdb_episode_id.is_not(None),
            )
        )
    return ImportResultOut(
        id=project.id,
        name=project.name,
        source_directory=project.source_directory,
        status=project.status,
        created=False,
        web_publish_intent=project.web_publish_intent,
        tmdb_mapped=bool(project.web_config_json),
        tmdb_episodes=episodes or 0,
        expected_episode_count=project.expected_episode_count,
        web_config_version=project.web_config_version if project.web_config_json else None,
    )


async def _by_request_id(request_id: str | None) -> Project | None:
    if not request_id:
        return None
    async with AsyncSessionLocal() as session:
        return await session.scalar(
            select(Project).where(Project.import_request_id == request_id)
        )


def _same_import(project: Project, body: ImportRequest) -> bool:
    return (
        project.source_directory == body.directory_name
        and project.anime_provider == body.provider
        and project.anime_external_id == body.provider_id
    )


@router.post("", response_model=ImportResultOut, status_code=201)
async def import_project(body: ImportRequest, response: Response) -> ImportResultOut:
    # Safety: reject names with path separators or traversal
    if "/" in body.directory_name or "\\" in body.directory_name or ".." in body.directory_name:
        raise HTTPException(status_code=400, detail="Invalid directory name")

    # Retry of an import that already committed (e.g. the response was lost):
    # recover that project before touching TMDB or the filesystem again.
    previous = await _by_request_id(body.request_id)
    if previous is not None:
        if not _same_import(previous, body):
            raise HTTPException(
                status_code=409,
                detail={"code": "import_request_conflict",
                        "message": "This request id was already used for a different import."},
            )
        response.status_code = 200
        return await _replay(previous)

    import_root = settings.import_root.resolve()
    dir_path = (import_root / body.directory_name).resolve()

    if not dir_path.is_relative_to(import_root) or not dir_path.is_dir():
        raise HTTPException(status_code=400, detail="Directory not found under import root")

    if body.intent == "publish_and_import" and body.tmdb is None:
        raise HTTPException(
            status_code=422,
            detail={"code": "web_mapping",
                    "message": "Publish & Import requires a confirmed TMDB mapping."},
        )
    # A source-only import resolves and stores nothing about TMDB or the web.
    intent = body.intent if body.tmdb is not None else None

    # Verify the mapping against TMDB *before* creating anything, so a TMDB
    # failure leaves no half-imported project and the form can simply be retried.
    prepared = None
    if body.tmdb is not None:
        try:
            prepared = await asyncio.to_thread(import_mapping.prepare, body.tmdb)
        except PublishingError as exc:
            raise _publishing_http_error(exc) from None

    # Create project
    project = Project(
        name=body.anime_title,
        source_directory=body.directory_name,
        anime_provider=body.provider,
        anime_external_id=body.provider_id,
        status="new",
        speaker_mapping_status="awaiting_discovery",
        import_request_id=body.request_id,
    )

    tmdb_episodes = 0
    try:
        async with AsyncSessionLocal() as session:
            session.add(project)
            await session.flush()
            if prepared is not None:
                # Same transaction as the project row: the mapping is all-or-nothing.
                tmdb_episodes = await import_mapping.apply(session, project, prepared, intent)
            await session.commit()
            await session.refresh(project)
    except IntegrityError:
        # A concurrent duplicate of the same request wins; recover its project.
        raced = await _by_request_id(body.request_id)
        if raced is not None and _same_import(raced, body):
            response.status_code = 200
            return await _replay(raced)
        raise HTTPException(status_code=409, detail="Directory already imported")

    # English title: the search result's value when the client sent one, else
    # the provider's details. Best-effort and never synthesized.
    try:
        english = body.anime_title_english
        if not (english and english.strip()):
            english = (await get_provider(body.provider).get_details(body.provider_id)).title_english
        async with AsyncSessionLocal() as session:
            await set_english_title(session, project.id, english)
            await session.commit()
    except Exception:
        logger.warning(
            "Could not fetch English title for %s/%s (project %d)",
            body.provider, body.provider_id, project.id, exc_info=True,
        )

    # Fetch + store characters and episode metadata in a separate
    # transaction (best-effort — the project works without them).
    try:
        provider = get_provider(body.provider)
        characters = await provider.get_characters(body.provider_id)
        episodes = await provider.get_episodes(body.provider_id)
        if not characters:
            # Not an error, but a real quality gap: no genders, no mapping
            # targets. The context panel surfaces this as an attention item.
            logger.warning(
                "Provider %s returned no characters for %s (project %d) — "
                "speaker mapping will have an empty roster",
                body.provider, body.provider_id, project.id,
            )
        if characters or episodes:
            async with AsyncSessionLocal() as session:
                await upsert_characters(session, project.id, characters)
                await upsert_episodes(session, project.id, episodes)
                await session.commit()
    except Exception:
        logger.warning(
            "Failed to fetch metadata for %s/%s (project %d created without characters/episodes)",
            body.provider, body.provider_id, project.id, exc_info=True,
        )

    # Kick off scan immediately (don't wait for 30s sweep)
    try:
        await orchestrate_project(project.id, job_manager.enqueue)
    except Exception:
        logger.warning("Failed to trigger initial scan for project %d", project.id, exc_info=True)

    return ImportResultOut(
        id=project.id,
        name=project.name,
        source_directory=project.source_directory,
        status=project.status,
        web_publish_intent=project.web_publish_intent,
        tmdb_mapped=prepared is not None,
        tmdb_episodes=tmdb_episodes,
        expected_episode_count=project.expected_episode_count,
        web_config_version=project.web_config_version if prepared is not None else None,
    )
