"""Two-step project import: TMDB mapping persistence, intent, validation, the
AniBridge suggestion route and the website-independent TMDB lookups.

TMDB is faked at the ``TmdbClient`` boundary; any attempt to build a website
client, enqueue a publication job or touch the network fails the test."""
from __future__ import annotations

import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import select

from app.api.routes import import_ as import_routes
from app.api.routes import projects as projects_routes
from app.api.routes import web_publishing as web_routes
from app.core.config import settings
from app.db.models import File, JobRecord, Project, ProjectEpisode
from app.db.options import AppOptions
from app.metadata import anibridge
from app.metadata.base import Character, Episode
from app.publishing import description, service
from app.publishing.web_client import PublishingError, WebClient
from tests.test_anibridge import DATASET, Clock, Network, payload
from tests.test_archive import aenv, client  # noqa: F401  (fixtures)
from tests.test_publish import env  # noqa: F401  (fixture used by aenv)

STD_EPISODES = [
    {"tmdb_episode_id": "9002", "episode_number": 2, "name_english": "Second",
     "air_date": "2021-01-14", "overview": "Two.", "runtime": 24},
    {"tmdb_episode_id": "9001", "episode_number": 1, "name_english": "First",
     "air_date": "2021-01-07", "overview": None, "runtime": None},
]
GROUP_EPISODES = {
    "sub1": [{"tmdb_episode_id": "7001", "episode_number": 1, "name_english": "Arc one",
              "air_date": None, "overview": None, "runtime": 23}],
    "sub2": [{"tmdb_episode_id": "7002", "episode_number": 1, "name_english": "Arc two",
              "air_date": None, "overview": None, "runtime": 23},
             {"tmdb_episode_id": "7003", "episode_number": 2, "name_english": "Arc two b",
              "air_date": None, "overview": None, "runtime": 23}],
}


class FakeTmdb:
    """Records calls so tests can assert what was (not) requested."""
    calls: list[tuple] = []
    fail: Exception | None = None

    def __init__(self):
        if FakeTmdb.fail:
            raise FakeTmdb.fail

    def details(self, media, title_id, refresh=False):
        FakeTmdb.calls.append(("details", media, title_id))
        return {"metadata": {"original_description": "TMDB overview", "description": "TMDB overview"}}

    def catalog(self, title_id, group_id=None, season_number=None, refresh=False, detailed=False):
        FakeTmdb.calls.append(("catalog", title_id, group_id, season_number, detailed))
        assert detailed, "import must request the detailed episode metadata"
        if group_id:
            return {
                "seasons": [
                    {"external_id": "sub1", "season_number": 1, "name_english": "Arc 1", "year": None},
                    {"external_id": "sub2", "season_number": 2, "name_english": "Arc 2", "year": None},
                ],
                "episodes_by_season": copy.deepcopy(GROUP_EPISODES), "groups": [],
            }
        return {
            "seasons": [
                {"external_id": "s1id", "season_number": 1, "name_english": "Season 1", "year": 2020},
                {"external_id": "s2id", "season_number": 2, "name_english": "Season 2", "year": 2021},
            ],
            "episodes_by_season": (
                {"s2id": copy.deepcopy(STD_EPISODES)} if season_number == 2 else {}),
            "groups": [{"id": "grp", "name": "Arcs"}],
        }


class FakeProvider:
    async def get_details(self, pid):
        return SimpleNamespace(title_english="English")

    async def get_characters(self, pid):
        return [Character(name="Aria", provider_id="1")]

    async def get_episodes(self, pid):
        return [Episode(number=1, title="AniDB one")]


def config(**over):
    value = {
        "tmdb_title_id": "42", "media_type": "tv", "episode_structure": "SEASONED",
        "order_kind": "standard", "group_id": None,
        "season": {"external_id": "s2id", "season_number": 2, "name_english": "Season 2", "year": 2021},
        "metadata": {
            "title_english": "Approved Title", "title_romaji": "Romaji Title", "year": 2021,
            "original_language": "ja", "original_description": "TMDB overview",
            "description": "Schválený popis",
        },
        "poster_path": "/poster.jpg",
    }
    value.update(over)
    return value


def body(directory, *, provider="anilist", provider_id="7", tmdb=None, intent="import_only",
         expected=12, request_id=None, **cfg):
    out = {"directory_name": directory, "provider": provider, "provider_id": provider_id,
           "anime_title": "Romaji Title", "intent": intent}
    if request_id:
        out["request_id"] = request_id
    if tmdb is not False:
        out["tmdb"] = tmdb or {"config": config(**cfg), "expected_episode_count": expected}
    return out


@pytest.fixture(autouse=True)
def isolated(aenv, monkeypatch):  # noqa: F811
    FakeTmdb.calls, FakeTmdb.fail = [], None
    monkeypatch.setattr("app.publishing.import_mapping.TmdbClient", FakeTmdb)
    monkeypatch.setattr("app.api.routes.import_.get_provider", lambda name: FakeProvider())
    monkeypatch.setattr("app.api.routes.import_.settings.import_root", aenv.import_root)
    orchestrate = AsyncMock()
    monkeypatch.setattr("app.api.routes.import_.orchestrate_project", orchestrate)
    aenv.orchestrate = orchestrate

    def forbidden(*_a, **_k):
        pytest.fail("importing must never contact the website")
    monkeypatch.setattr(WebClient, "__init__", forbidden)
    monkeypatch.setattr(httpx.Client, "send", lambda *_a, **_k: pytest.fail("unexpected HTTP"))
    return aenv


def make_dirs(env, *names):
    for name in names:
        (env.import_root / name).mkdir()


def project(env, pid) -> Project:
    with env.sync() as s:
        return s.get(Project, pid)


def episodes(env, pid) -> list[ProjectEpisode]:
    with env.sync() as s:
        return list(s.scalars(select(ProjectEpisode).where(ProjectEpisode.project_id == pid)
                              .order_by(ProjectEpisode.episode_number)))


def stored_config(env, pid) -> dict:
    return json.loads(project(env, pid).web_config_json)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("provider,provider_id", [("anidb", "1"), ("anilist", "290")])
async def test_both_providers_persist_the_confirmed_mapping(client, isolated, provider, provider_id):
    make_dirs(isolated, "Show")
    r = await client.post("/api/import", json=body("Show", provider=provider, provider_id=provider_id))
    assert r.status_code == 201, r.text
    out = r.json()
    assert (out["tmdb_mapped"], out["tmdb_episodes"], out["expected_episode_count"]) == (True, 2, 12)

    p = project(isolated, out["id"])
    assert (p.anime_provider, p.anime_external_id) == (provider, provider_id)
    envelope = json.loads(p.web_config_json)
    assert envelope["destination"] is None            # unbound until the WEB wizard saves it
    cfg = envelope["config"]
    assert (cfg["tmdb_title_id"], cfg["media_type"], cfg["episode_structure"]) == ("42", "tv", "SEASONED")
    assert (cfg["order_kind"], cfg["group_id"]) == ("standard", None)
    assert cfg["season"] == {"external_id": "s2id", "season_number": 2, "name_english": "Season 2", "year": 2021}
    assert cfg["metadata"]["title_english"] == "Approved Title"
    assert cfg["metadata"]["description"] == "Schválený popis"         # approved
    assert cfg["metadata"]["original_description"] == "TMDB overview"  # original kept apart
    assert cfg["poster_path"] == "/poster.jpg"
    assert p.web_config_version == 1 and p.web_publish_state is None


@pytest.mark.asyncio
async def test_tmdb_episodes_are_persisted_without_any_discovered_files(client, isolated):
    make_dirs(isolated, "Show")
    pid = (await client.post("/api/import", json=body("Show", provider="anilist"))).json()["id"]
    with isolated.sync() as s:
        assert s.scalars(select(File).where(File.project_id == pid)).first() is None
    rows = episodes(isolated, pid)
    assert [(e.episode_number, e.tmdb_episode_id, e.tmdb_name, e.tmdb_air_date, e.tmdb_overview, e.tmdb_runtime)
            for e in rows] == [
        (1, "9001", "First", "2021-01-07", None, None),
        (2, "9002", "Second", "2021-01-14", "Two.", 24),
    ]
    assert FakeTmdb.calls[-1] == ("catalog", "42", None, 2, True)


@pytest.mark.asyncio
async def test_provider_episode_titles_merge_into_the_same_rows(client, isolated):
    """AniDB titles and TMDB identities share one row per episode number."""
    make_dirs(isolated, "Show")
    pid = (await client.post("/api/import", json=body("Show", provider="anidb", provider_id="1"))).json()["id"]
    first, second = episodes(isolated, pid)
    assert (first.title, first.tmdb_episode_id) == ("AniDB one", "9001")
    assert (second.title, second.tmdb_episode_id) == (None, "9002")
    assert len(episodes(isolated, pid)) == 2          # no duplicate identity rows


@pytest.mark.asyncio
async def test_both_import_actions_persist_equivalent_metadata(client, isolated):
    make_dirs(isolated, "A", "B")
    a = (await client.post("/api/import", json=body("A", intent="import_only"))).json()
    b = (await client.post("/api/import", json=body("B", intent="publish_and_import"))).json()
    assert (a["web_publish_intent"], b["web_publish_intent"]) == ("import_only", "publish_and_import")

    def comparable(pid):
        p = project(isolated, pid)
        return (
            p.web_config_json, p.web_config_version, p.expected_episode_count, p.web_publish_state,
            p.status, p.anime_provider, p.anime_external_id,
            [(e.episode_number, e.tmdb_episode_id, e.tmdb_name) for e in episodes(isolated, pid)],
        )
    assert comparable(a["id"]) == comparable(b["id"])
    assert project(isolated, a["id"]).web_publish_intent == "import_only"
    assert project(isolated, b["id"]).web_publish_intent == "publish_and_import"


@pytest.mark.asyncio
async def test_alternative_episode_order_is_persisted_from_the_group(client, isolated):
    make_dirs(isolated, "Show")
    cfg = dict(order_kind="episode_group", group_id="grp",
               season={"external_id": "sub2", "season_number": 2, "name_english": "Arc 2", "year": None})
    r = await client.post("/api/import", json=body("Show", expected=2, **cfg))
    assert r.status_code == 201, r.text
    stored = stored_config(isolated, r.json()["id"])["config"]
    assert (stored["order_kind"], stored["group_id"], stored["season"]["external_id"]) == ("episode_group", "grp", "sub2")
    assert [(e.episode_number, e.tmdb_episode_id) for e in episodes(isolated, r.json()["id"])] == [(1, "7002"), (2, "7003")]
    assert FakeTmdb.calls[-1] == ("catalog", "42", "grp", 2, True)


@pytest.mark.asyncio
async def test_season_selection_decides_which_episodes_are_stored(client, isolated):
    make_dirs(isolated, "Show")
    cfg = dict(order_kind="episode_group", group_id="grp",
               season={"external_id": "sub1", "season_number": 1, "name_english": "Arc 1", "year": None})
    pid = (await client.post("/api/import", json=body("Show", expected=1, **cfg))).json()["id"]
    assert [e.tmdb_episode_id for e in episodes(isolated, pid)] == ["7001"]


@pytest.mark.asyncio
async def test_flat_tv_and_movies_store_no_episodes(client, isolated):
    make_dirs(isolated, "Flat", "Film")
    flat = await client.post("/api/import", json=body("Flat", expected=24, episode_structure="FLAT", season=None))
    assert flat.status_code == 201, flat.text
    assert flat.json()["tmdb_episodes"] == 0
    assert all(e.tmdb_episode_id is None for e in episodes(isolated, flat.json()["id"]))

    FakeTmdb.calls.clear()
    movie = await client.post("/api/import", json=body(
        "Film", expected=1, media_type="movie", episode_structure="FLAT", season=None, tmdb_title_id="99"))
    assert movie.status_code == 201, movie.text
    assert stored_config(isolated, movie.json()["id"])["config"]["media_type"] == "movie"
    assert not any(c[0] == "catalog" for c in FakeTmdb.calls)    # nothing episodic to load


@pytest.mark.asyncio
async def test_season_zero_specials_are_a_valid_selection(client, isolated, monkeypatch):
    make_dirs(isolated, "Show")
    original = FakeTmdb.catalog

    def with_specials(self, *a, **k):
        data = original(self, *a, **k)
        data["seasons"].append({"external_id": "s0id", "season_number": 0, "name_english": "Specials", "year": None})
        data["episodes_by_season"] = {"s0id": [dict(STD_EPISODES[1], tmdb_episode_id="8001")]}
        return data
    monkeypatch.setattr(FakeTmdb, "catalog", with_specials)
    r = await client.post("/api/import", json=body("Show", expected=1, season={
        "external_id": "s0id", "season_number": 0, "name_english": "Specials", "year": None}))
    assert r.status_code == 201, r.text
    assert [e.tmdb_episode_id for e in episodes(isolated, r.json()["id"])] == ["8001"]


# ---------------------------------------------------------------------------
# Expected episode count
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [0, -3, 1.5, "12", None, True, 100_001])
async def test_expected_episode_count_must_be_a_positive_integer(client, isolated, bad):
    make_dirs(isolated, "Show")
    r = await client.post("/api/import", json=body("Show", expected=bad))
    assert r.status_code == 422
    with isolated.sync() as s:
        assert s.scalars(select(Project)).first() is None        # nothing created
    assert FakeTmdb.calls == []                                   # rejected before any TMDB call


@pytest.mark.asyncio
async def test_expected_count_is_independent_of_episodes_and_stays_fixed(client, isolated, monkeypatch):
    make_dirs(isolated, "Show")
    pid = (await client.post("/api/import", json=body("Show", provider="anidb", provider_id="1", expected=24))).json()["id"]
    assert project(isolated, pid).expected_episode_count == 24 and len(episodes(isolated, pid)) == 2

    # A metadata refresh adds/changes episodes but never touches the expected count.
    monkeypatch.setattr("app.metadata.registry.get_provider", lambda name: SimpleNamespace(
        get_details=FakeProvider().get_details, get_characters=FakeProvider().get_characters,
        get_episodes=AsyncMock(return_value=[Episode(number=n, title=f"T{n}") for n in range(1, 31)])))
    assert (await client.post(f"/api/projects/{pid}/refresh-metadata")).status_code == 200
    assert len(episodes(isolated, pid)) == 30
    assert project(isolated, pid).expected_episode_count == 24
    assert [e.tmdb_episode_id for e in episodes(isolated, pid)[:2]] == ["9001", "9002"]   # TMDB ids intact


@pytest.mark.asyncio
async def test_project_api_exposes_expected_count_and_intent(client, isolated):
    make_dirs(isolated, "Show")
    pid = (await client.post("/api/import", json=body("Show", intent="publish_and_import", expected=13))).json()["id"]
    out = (await client.get(f"/api/projects/{pid}")).json()
    assert (out["expected_episode_count"], out["web_publish_intent"]) == (13, "publish_and_import")


# ---------------------------------------------------------------------------
# Legacy compatibility and validation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_legacy_import_without_tmdb_is_unchanged(client, isolated, monkeypatch):
    make_dirs(isolated, "Old")
    monkeypatch.setattr("app.publishing.import_mapping.TmdbClient",
                        lambda: pytest.fail("legacy import must not use TMDB"))
    r = await client.post("/api/import", json={
        "directory_name": "Old", "provider": "anidb", "provider_id": "5", "anime_title": "Old Show"})
    assert r.status_code == 201
    out = r.json()
    assert (out["tmdb_mapped"], out["tmdb_episodes"], out["expected_episode_count"], out["web_publish_intent"]) == (
        False, 0, None, None)
    p = project(isolated, out["id"])
    assert p.web_config_json is None and p.expected_episode_count is None and p.web_publish_intent is None
    assert p.web_config_version == 0
    assert [e.title for e in episodes(isolated, out["id"])] == ["AniDB one"]
    assert all(e.tmdb_episode_id is None for e in episodes(isolated, out["id"]))
    isolated.orchestrate.assert_awaited_once()                    # Discover still kicks off


@pytest.mark.asyncio
async def test_source_only_import_stores_no_tmdb_and_no_web_intent(client, isolated):
    make_dirs(isolated, "Skip")
    r = await client.post("/api/import", json=body("Skip", tmdb=False, intent="import_only",
                                                   request_id="req-source-1"))
    assert r.status_code == 201
    assert r.json()["web_publish_intent"] is None and r.json()["tmdb_mapped"] is False
    p = project(isolated, r.json()["id"])
    assert p.web_config_json is None and p.web_publish_intent is None
    assert p.expected_episode_count is None and p.import_request_id == "req-source-1"
    assert FakeTmdb.calls == []                       # TMDB was never resolved
    assert all(e.tmdb_episode_id is None for e in episodes(isolated, p.id))


@pytest.mark.asyncio
async def test_repeating_a_request_id_recovers_the_same_project(client, isolated):
    make_dirs(isolated, "Show")
    first = await client.post("/api/import", json=body("Show", request_id="req-12345678"))
    assert first.status_code == 201 and first.json()["created"] is True
    calls = list(FakeTmdb.calls)
    again = await client.post("/api/import", json=body("Show", request_id="req-12345678"))
    assert again.status_code == 200 and again.json()["created"] is False
    assert again.json()["id"] == first.json()["id"]
    assert again.json()["tmdb_mapped"] is True and again.json()["web_config_version"] == 1
    assert FakeTmdb.calls == calls                    # a replay never touches TMDB again
    with isolated.sync() as s:
        assert len(list(s.scalars(select(Project)))) == 1
    # the same id for a different import is a conflict, never a silent reuse
    make_dirs(isolated, "Other")
    clash = await client.post("/api/import", json=body("Other", request_id="req-12345678"))
    assert clash.status_code == 409 and clash.json()["detail"]["code"] == "import_request_conflict"


@pytest.mark.asyncio
async def test_a_movie_import_creates_one_stable_content_episode_without_a_tmdb_id(client, isolated):
    make_dirs(isolated, "Film")
    movie = body("Film", tmdb={"config": config(
        media_type="movie", episode_structure="FLAT", season=None, tmdb_title_id="77"),
        "expected_episode_count": 1})
    pid = (await client.post("/api/import", json=movie)).json()["id"]
    rows = episodes(isolated, pid)
    assert len(rows) == 1 and rows[0].tmdb_episode_id is None
    assert rows[0].web_publisher_key.startswith("subineko:")


@pytest.mark.asyncio
async def test_publish_and_import_requires_a_confirmed_mapping(client, isolated):
    make_dirs(isolated, "Show")
    r = await client.post("/api/import", json=body("Show", tmdb=False, intent="publish_and_import"))
    assert r.status_code == 422 and r.json()["detail"]["code"] == "web_mapping"
    with isolated.sync() as s:
        assert s.scalars(select(Project)).first() is None


@pytest.mark.asyncio
async def test_unknown_intent_is_rejected(client, isolated):
    make_dirs(isolated, "Show")
    r = await client.post("/api/import", json=body("Show", intent="publish_now"))
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_seasoned_import_requires_a_season(client, isolated):
    make_dirs(isolated, "Show")
    r = await client.post("/api/import", json=body("Show", season=None))
    assert r.status_code == 422 and r.json()["detail"]["code"] == "web_mapping"


@pytest.mark.asyncio
async def test_a_season_outside_the_selected_order_is_rejected(client, isolated):
    make_dirs(isolated, "Show")
    r = await client.post("/api/import", json=body("Show", season={
        "external_id": "nope", "season_number": 9, "name_english": None, "year": None}))
    assert r.status_code == 422 and "season" in r.json()["detail"]["message"].lower()
    with isolated.sync() as s:
        assert s.scalars(select(Project)).first() is None


@pytest.mark.asyncio
async def test_invalid_structures_are_rejected_by_the_shared_schema(client, isolated):
    make_dirs(isolated, "Show")
    movie_seasoned = await client.post("/api/import", json=body("Show", media_type="movie"))
    group_without_id = await client.post("/api/import", json=body("Show", order_kind="episode_group"))
    flat_with_season = await client.post("/api/import", json=body("Show", episode_structure="FLAT"))
    assert {movie_seasoned.status_code, group_without_id.status_code, flat_with_season.status_code} == {422}


@pytest.mark.asyncio
async def test_tmdb_failure_creates_nothing_and_the_retry_succeeds(client, isolated):
    make_dirs(isolated, "Show")
    FakeTmdb.fail = PublishingError("tmdb_request", "TMDB could not provide metadata. Check credentials or retry.", 502)
    r = await client.post("/api/import", json=body("Show"))
    assert r.status_code == 502 and r.json()["detail"]["code"] == "tmdb_request"
    with isolated.sync() as s:
        assert s.scalars(select(Project)).first() is None
    assert [d["name"] for d in (await client.get("/api/import/directories")).json()] == ["Show"]

    FakeTmdb.fail = None
    assert (await client.post("/api/import", json=body("Show"))).status_code == 201


@pytest.mark.asyncio
async def test_unconfigured_tmdb_is_reported_and_skipping_still_works(client, isolated):
    make_dirs(isolated, "Show")
    FakeTmdb.fail = PublishingError("tmdb_unconfigured", "Configure TMDB_API_TOKEN on the server.", 503)
    r = await client.post("/api/import", json=body("Show"))
    assert r.status_code == 503 and r.json()["detail"]["code"] == "tmdb_unconfigured"
    assert (await client.post("/api/import", json=body("Show", tmdb=False))).status_code == 201


@pytest.mark.asyncio
async def test_duplicate_directory_and_traversal_still_rejected(client, isolated):
    make_dirs(isolated, "Show")
    assert (await client.post("/api/import", json=body("Show"))).status_code == 201
    assert (await client.post("/api/import", json=body("Show"))).status_code == 409
    assert (await client.post("/api/import", json=body("../x"))).status_code == 400


@pytest.mark.asyncio
async def test_mapping_is_all_or_nothing_with_the_project_row(client, isolated, monkeypatch):
    make_dirs(isolated, "Show")

    async def boom(*_a, **_k):
        raise RuntimeError("disk full")
    monkeypatch.setattr("app.api.routes.import_.import_mapping.apply", boom)
    with pytest.raises(RuntimeError):
        await client.post("/api/import", json=body("Show"))
    with isolated.sync() as s:
        assert s.scalars(select(Project)).first() is None


# ---------------------------------------------------------------------------
# No website requests / publication jobs
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("intent", ["import_only", "publish_and_import"])
async def test_importing_never_starts_publication(client, isolated, monkeypatch, intent):
    make_dirs(isolated, "Show")
    monkeypatch.setattr(settings, "subineko_web_url", "https://web.example")
    monkeypatch.setattr(settings, "subineko_web_token", "token")
    r = await client.post("/api/import", json=body("Show", intent=intent))
    assert r.status_code == 201
    pid = r.json()["id"]
    p = project(isolated, pid)
    assert p.web_publish_state is None and p.web_publish_attempt == 0 and p.web_identity_locked_at is None
    with isolated.sync() as s:
        assert s.scalars(select(JobRecord).where(JobRecord.job_type == "publish_web")).first() is None
    isolated.enqueue.assert_not_called()
    # the only orchestration is the regular Discover kick-off
    isolated.orchestrate.assert_awaited_once()
    assert isolated.orchestrate.await_args.args[0] == pid
    assert "publication" not in json.dumps(r.json()).lower()


# ---------------------------------------------------------------------------
# WEB wizard compatibility with an import-created (unbound) mapping
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_web_service_reads_an_import_mapping_and_requires_a_first_save(client, isolated, monkeypatch):
    make_dirs(isolated, "Show")
    pid = (await client.post("/api/import", json=body("Show"))).json()["id"]
    monkeypatch.setattr(settings, "subineko_web_url", "https://web.example")
    monkeypatch.setattr(settings, "subineko_web_token", "token")
    monkeypatch.setattr(service, "SyncSessionLocal", isolated.sync)
    monkeypatch.setattr(service.options_store, "snapshot", lambda: AppOptions(target_lang_code="cs"))

    state = service.state(pid)
    assert state["config"]["tmdb_title_id"] == "42" and state["state"] == "configured"
    assert state["version"] == 1 and state["locked"] is False

    # Publishing from an unsaved import mapping is refused until the wizard
    # saves (and thereby binds) it to this website.
    with isolated.sync() as s:
        request = SimpleNamespace(version=1, file_ids=[])
        with pytest.raises(PublishingError) as exc:
            service.prepare(s, s.get(Project, pid), request, AppOptions(target_lang_code="cs"))
    assert exc.value.code == "web_mapping"


def test_destination_binding_rules():
    def holder(destination):
        return SimpleNamespace(web_config_json=json.dumps({"destination": destination, "config": {}}))
    settings_backup = (settings.subineko_web_url, settings.subineko_web_token)
    settings.subineko_web_url, settings.subineko_web_token = "https://web.example", "token"
    try:
        assert service.load(holder(None))["destination"] is None               # unbound import mapping
        assert service.load(holder(service.destination()))["destination"] == service.destination()
        with pytest.raises(PublishingError) as exc:                            # a bound, different site still fails closed
            service.load(holder("0" * 64))
        assert exc.value.code == "web_destination_changed"
        assert service.load(SimpleNamespace(web_config_json=None)) == {}       # legacy projects
    finally:
        settings.subineko_web_url, settings.subineko_web_token = settings_backup


# ---------------------------------------------------------------------------
# AniBridge suggestion route
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_suggestion_route_shapes_and_degrades(client, isolated, tmp_path, monkeypatch):
    store = anibridge.AniBridgeStore(tmp_path / "ab", fetch=Network(payload()), now=Clock(), spawn=lambda fn: fn())
    monkeypatch.setattr(anibridge, "_store", store)

    ok = await client.get("/api/import/tmdb-suggestion", params={"provider": "anidb", "provider_id": "1"})
    assert ok.status_code == 200 and ok.json()["status"] == "matched"
    assert ok.json()["candidates"][0]["tmdb_id"] == "26209"

    ambiguous = (await client.get("/api/import/tmdb-suggestion", params={"provider": "anilist", "provider_id": "5"})).json()
    assert ambiguous["status"] == "ambiguous" and ambiguous["title"] is None and len(ambiguous["candidates"]) == 2

    none = (await client.get("/api/import/tmdb-suggestion", params={"provider": "anilist", "provider_id": "123456"})).json()
    assert none["status"] == "none"

    assert (await client.get("/api/import/tmdb-suggestion", params={"provider": "mal", "provider_id": "1"})).status_code == 422

    monkeypatch.setattr(anibridge, "_store", anibridge.AniBridgeStore(
        tmp_path / "down", fetch=Network(OSError("offline")), now=Clock(), spawn=lambda fn: fn()))
    down = await client.get("/api/import/tmdb-suggestion", params={"provider": "anidb", "provider_id": "1"})
    assert down.status_code == 200 and down.json()["status"] == "unavailable"


# ---------------------------------------------------------------------------
# Website-independent TMDB lookups and description translation
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def web_client():
    app = FastAPI()
    app.include_router(web_routes.metadata_router, prefix="/api")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        yield c


@pytest.mark.asyncio
async def test_tmdb_lookups_work_without_a_configured_website(web_client, monkeypatch):
    monkeypatch.setattr(settings, "subineko_web_url", "")
    monkeypatch.setattr(settings, "subineko_web_token", "")
    assert not settings.web_publishing_available
    monkeypatch.setattr(web_routes, "TmdbClient", FakeTmdb)
    monkeypatch.setattr(FakeTmdb, "search", lambda self, q: [{"id": "1", "title": q}], raising=False)
    monkeypatch.setattr(FakeTmdb, "catalog", lambda self, *a, **k: {"seasons": [], "episodes_by_season": {}, "groups": []})

    assert (await web_client.get("/api/web/tmdb/search", params={"q": "x"})).json() == [{"id": "1", "title": "x"}]
    assert (await web_client.get("/api/web/tmdb/catalog", params={"title_id": "1"})).status_code == 200
    # Remote website operations still require the website to be configured.
    lookup = await web_client.post("/api/web/tmdb/lookup", json=config())
    assert lookup.status_code == 503 and lookup.json()["detail"]["code"] == "web_unavailable"


@pytest.mark.asyncio
async def test_tmdb_errors_keep_their_code(web_client, monkeypatch):
    FakeTmdb.fail = PublishingError("tmdb_unconfigured", "Configure TMDB_API_TOKEN on the server.", 503)
    monkeypatch.setattr(web_routes, "TmdbClient", FakeTmdb)
    r = await web_client.get("/api/web/tmdb/search", params={"q": "x"})
    assert r.status_code == 503 and r.json()["detail"]["code"] == "tmdb_unconfigured"


@pytest.mark.asyncio
async def test_description_translation_needs_no_project(client, monkeypatch):
    seen = {}

    def fake_complete(**kwargs):
        seen.update(kwargs)
        return SimpleNamespace(description="  Český popis  "), None
    monkeypatch.setattr(description.client, "complete", fake_complete)
    monkeypatch.setattr(description.options_store, "snapshot",
                        lambda: AppOptions(target_lang_name="Czech", target_lang_code="cs"))
    r = await client.post("/api/import/translate-description", json={"description": "A hero rises."})
    assert r.status_code == 200 and r.json() == {"description": "Český popis"}
    assert seen["project_id"] is None and seen["user"] == "A hero rises." and "Czech" in seen["system"]

    bad = await client.post("/api/import/translate-description", json={"description": "bad\x00text"})
    assert bad.status_code == 422 and bad.json()["detail"]["code"] == "web_description_source"
    assert (await client.post("/api/import/translate-description", json={"description": ""})).status_code == 422
