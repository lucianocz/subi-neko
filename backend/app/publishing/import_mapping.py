"""Confirmed TMDB mapping captured while importing a project.

Reuses the WEB publishing structures instead of introducing parallel ones:

* the approved title/placement is a :class:`~app.publishing.schemas.WebConfig`
  stored in ``Project.web_config_json`` (the same envelope the WEB wizard edits);
* TMDB episodes become ``ProjectEpisode`` rows (``tmdb_*`` columns) — one
  identity per episode, independent of any ``File``;
* ``expected_episode_count`` and ``web_publish_intent`` are plain project columns.

Nothing here talks to the website or schedules a job. The envelope is stored with
``destination: null`` (unbound): the WEB wizard binds it on its first save, which
keeps the "website destination changed" safety check meaningful.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Project
from app.metadata.sync import upsert_tmdb_episodes
from app.publishing import service
from app.publishing.remote import ensure_content_episode
from app.publishing.schemas import StrictModel, WebConfig
from app.publishing.tmdb import TmdbClient
from app.publishing.web_client import PublishingError

ImportIntent = Literal["import_only", "publish_and_import"]
MAX_EXPECTED_EPISODES = 100_000


class TmdbImportMapping(StrictModel):
    """What the import wizard's TMDB step confirms."""
    config: WebConfig
    expected_episode_count: Annotated[
        int, Field(ge=1, le=MAX_EXPECTED_EPISODES, strict=True)
    ]


@dataclass
class PreparedMapping:
    config: dict                 # validated WebConfig as a plain dict
    expected_episode_count: int
    episodes: list[dict]         # detailed TMDB catalog episodes (may be empty)

    def envelope_json(self) -> str:
        return service.dumps({"destination": None, "config": self.config})


def prepare(mapping: TmdbImportMapping) -> PreparedMapping:
    """Verify the mapping against TMDB and capture the authoritative episodes.

    Synchronous and network-bound: call it *before* opening the project
    transaction (via ``asyncio.to_thread``). The client-supplied episode list is
    never trusted — identities come from TMDB for the selected order/season.
    """
    config = mapping.config
    if config.episode_structure == "SEASONED" and not config.season:
        raise PublishingError("web_mapping", "Select a season before importing.", 422)

    client = TmdbClient()
    details = client.details(config.media_type, config.tmdb_title_id)
    data = config.model_dump()
    if not data["metadata"].get("original_description"):
        data["metadata"]["original_description"] = details["metadata"]["original_description"]
        if not data["metadata"].get("description"):
            data["metadata"]["description"] = details["metadata"]["original_description"]

    episodes: list[dict] = []
    if config.media_type == "tv" and (config.season or config.group_id):
        catalog = client.catalog(
            config.tmdb_title_id,
            config.group_id,
            config.season.season_number if config.season else None,
            detailed=True,
        )
        if config.season:
            selected = next(
                (s for s in catalog["seasons"] if s["external_id"] == config.season.external_id),
                None,
            )
            if not selected or selected["season_number"] != config.season.season_number:
                raise PublishingError(
                    "web_mapping",
                    "Select a real TMDB season/subgroup from the selected order.",
                    422,
                )
            episodes = catalog["episodes_by_season"].get(selected["external_id"], [])
    seen: set[int] = set()
    unique = []
    for episode in sorted(episodes, key=lambda e: e["episode_number"]):
        if episode["episode_number"] in seen:   # keep the first of a duplicated number
            continue
        seen.add(episode["episode_number"])
        unique.append(episode)
    return PreparedMapping(data, mapping.expected_episode_count, unique)


async def apply(
    session: AsyncSession, project: Project, prepared: PreparedMapping,
    intent: ImportIntent | None,
) -> int:
    """Write the mapping onto a (flushed) project inside the caller's transaction.

    Returns the number of TMDB episodes stored. ``web_publish_state`` is left
    untouched: persisting an intent never means anything was published.
    """
    project.web_config_json = prepared.envelope_json()
    project.web_config_version = 1
    project.expected_episode_count = prepared.expected_episode_count
    project.web_publish_intent = intent
    stored = await upsert_tmdb_episodes(session, project.id, prepared.episodes)
    # A movie has no TMDB episode; its single content episode gets a stable
    # publisher key now so later uploads/progress have an identity to attach to.
    config = WebConfig.model_validate(prepared.config)
    await session.run_sync(lambda sync: ensure_content_episode(sync, project.id, config))
    return stored
