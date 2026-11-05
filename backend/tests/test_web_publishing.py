"""Publishing tests use temporary databases and mocked HTTP only."""

import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from app.core.config import settings
from app.core.database import Base
from app.db.models import File, JobRecord, Project, Subtitle, SubtitleEvent
from app.db.options import AppOptions
from app.jobs.context import JobContext
from app.jobs.handlers import publish_web as handler
from app.publishing import service
from app.publishing.schemas import Mapping, PublishRequest, SaveRequest, WebConfig
from app.publishing.tmdb import TmdbClient
from app.publishing.web_client import PublishingError, WebClient
from jsonschema import Draft202012Validator
from sqlalchemy import create_engine, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import sessionmaker


@pytest_asyncio.fixture
async def env(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "subineko_web_url", "https://web.example")
    monkeypatch.setattr(settings, "subineko_web_token", "test-token-do-not-leak")
    monkeypatch.setattr(settings, "tmdb_api_token", "tmdb-secret")
    monkeypatch.setattr(settings, "config_root", tmp_path)
    db = tmp_path / "web.db"
    engine = create_engine(f"sqlite:///{db}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    sync = sessionmaker(engine, expire_on_commit=False)
    async_engine = create_async_engine(f"sqlite+aiosqlite:///{db}")
    asess = async_sessionmaker(async_engine, expire_on_commit=False)
    for target in ("app.publishing.service", "app.jobs.handlers.publish_web"):
        monkeypatch.setattr(f"{target}.SyncSessionLocal", sync)
    for target in (
        "app.publishing.service",
        "app.db.archive_state",
        "app.jobs.manager",
        "app.orchestrator.orchestrator",
    ):
        monkeypatch.setattr(f"{target}.AsyncSessionLocal", asess)
    opts = AppOptions(target_lang_name="Czech", target_lang_code="cs")
    monkeypatch.setattr(service.options_store, "snapshot", lambda: opts)
    # A real network call is always a test failure, even on an error path.
    monkeypatch.setattr(
        httpx.Client, "send", lambda *_a, **_k: pytest.fail("Unexpected external HTTP")
    )
    with sync() as s:
        p = Project(
            name="Test",
            source_directory="missing",
            anime_provider="anidb",
            anime_external_id="5",
        )
        s.add(p)
        s.flush()
        for i in range(3):
            f = File(
                project_id=p.id,
                filename=f"[Group] episode {i + 1}.mkv",
                relative_path=f"{i + 1}.mkv",
                episode_number=i + 1,
                status="accepted" if i < 2 else "ready",
            )
            s.add(f)
            s.flush()
            s.add(Subtitle(file_id=f.id, script_type="v4.00+"))
            s.add(
                SubtitleEvent(
                    file_id=f.id,
                    line_index=0,
                    event_type="dialogue",
                    layer=0,
                    start_ms=0,
                    end_ms=1500,
                    original_start_ms=0,
                    original_end_ms=1500,
                    style="Default",
                    source_text="Hello",
                    translated_text="Ahoj světe",
                )
            )
        s.commit()
        pid = p.id
    config = WebConfig(
        tmdb_title_id="100",
        media_type="movie",
        episode_structure="FLAT",
        metadata={"title_english": "Film", "original_language": "ja"},
    )
    remote = FakeWebsite(config)
    monkeypatch.setattr(service, "lookup", lambda _c: copy.deepcopy(remote.snapshot))
    monkeypatch.setattr(WebClient, "request", remote.request)
    monkeypatch.setattr(TmdbClient, "details", lambda *_a, **_k: config.model_dump())
    yield SimpleNamespace(
        sync=sync,
        asess=asess,
        pid=pid,
        config=config,
        remote=remote,
        opts=opts,
        ctx=JobContext(tmp_path / "missing", tmp_path / "output", opts),
        tmp=tmp_path,
    )
    service._live.clear()
    await async_engine.dispose()
    engine.dispose()


class FakeWebsite:
    def __init__(self, config):
        self.config = config
        self.snapshot = None
        self.calls = []
        self.fail_upload = None
        self.hook = None

    def request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        if method == "GET":
            return copy.deepcopy(self.snapshot)
        if path.endswith("/poster"):
            return {"outcome": "created", "poster": {"sha256": "a" * 64}}
        if path.startswith("/api/v1/episodes/") and path.endswith("/subtitles"):
            eid = path.split("/")[-2]
            if eid == self.fail_upload:
                raise PublishingError("web_validation", "Rejected ASS", 502)
            episode = next(
                e
                for e in self.snapshot["episodes"]
                + [e for s in self.snapshot["seasons"] for e in s["episodes"]]
                if e["id"] == eid
            )
            content = kwargs["files"]["file"][1]
            sha = hashlib.sha256(content).hexdigest()
            previous = episode.get("current_subtitle")
            outcome = (
                "noop"
                if previous and previous["sha256"] == sha
                else "updated"
                if previous
                else "created"
            )
            if outcome != "noop":
                episode["current_subtitle"] = {"sha256": sha, **kwargs["data"]}
            if self.hook:
                self.hook()
            return {"outcome": outcome, "revision": episode["current_subtitle"]}
        body = kwargs["json"]
        schemas = json.loads(
            (Path(__file__).parent / "fixtures/web_publish_schemas.json").read_text(
                encoding="utf-8"
            )
        )
        schema_name = (
            "SeasonPublish"
            if path.endswith("/seasons")
            else "FlatPublish"
            if path.endswith("/episodes")
            else "TitlePublish"
        )
        Draft202012Validator(
            {**schemas, "$ref": f"#/components/schemas/{schema_name}"}
        ).validate(body)
        if path.endswith("/seasons"):
            season = next(
                (
                    s
                    for s in self.snapshot["seasons"]
                    if s["mappings"] == body["mappings"]
                ),
                None,
            )
            if not season:
                season = {**copy.deepcopy(body), "id": "season-uuid", "episodes": []}
                self.snapshot["seasons"].append(season)
            target = season["episodes"]
        elif path.endswith("/episodes"):
            target = self.snapshot["episodes"]
        else:
            if not self.snapshot:
                self.snapshot = {
                    **copy.deepcopy(body),
                    "id": "title-uuid",
                    "seasons": [],
                    "episodes": [],
                }
            else:
                self.snapshot.update(copy.deepcopy(body))
            return {"outcome": "created", "title": copy.deepcopy(self.snapshot)}
        for index, episode in enumerate(body["episodes"]):
            found = next(
                (
                    e
                    for e in target
                    if (
                        e.get("publisher_key") == episode.get("publisher_key")
                        if episode.get("publisher_key")
                        else e["mappings"] == episode["mappings"]
                    )
                ),
                None,
            )
            if not found:
                target.append(
                    {
                        **copy.deepcopy(episode),
                        "id": f"episode-{index + 1}",
                        "current_subtitle": None,
                    }
                )
        return {"outcome": "created", "title": copy.deepcopy(self.snapshot)}


def setup(env, mappings=True):
    body = SaveRequest(
        version=0,
        config=env.config,
        mappings=[
            Mapping(file_id=i, episode_number=i, name_english=f"Film {i}")
            for i in (1, 2)
        ]
        if mappings
        else None,
    )
    return service.save(env.pid, body)


def run(env, **kwargs):
    with env.sync() as s:
        p = s.get(Project, env.pid)
        p.web_publish_attempt += 1
        p.web_publish_state = "publishing"
        payload = {
            "project_id": env.pid,
            "attempt": p.web_publish_attempt,
            "version": p.web_config_version,
            "file_ids": [1, 2],
            "release_label": "Group",
            "changelog": "First",
            **kwargs,
        }
        s.add(
            JobRecord(
                project_id=p.id,
                job_type="publish_web",
                status="running",
                payload_json=service.dumps(payload),
                dedupe_key=f"publish_web:{p.id}:{p.web_publish_attempt}",
            )
        )
        s.commit()
    return handler.publish_web(payload, env.ctx, lambda *_: None)


def test_configuration_syntax_and_only_boolean(monkeypatch):
    from app.core.config import Settings

    for url in (
        "",
        "ftp://web.example",
        "https://u:p@web.example",
        "https://web.example/api",
        "https://web.example?q=a",
    ):
        assert not Settings(
            _env_file=None, subineko_web_url=url, subineko_web_token="secret"
        ).web_publishing_available
    for url in ("https://web.example", "http://web.example", "http://localhost:8000"):
        assert Settings(
            _env_file=None,
            subineko_web_url=url,
            subineko_web_token="secret",
        ).web_publishing_available
    monkeypatch.setattr(settings, "subineko_web_token", "")
    with pytest.raises(PublishingError, match="not configured"):
        service.require_available()


@pytest.mark.asyncio
async def test_save_restore_keys_versions_and_destination(env, monkeypatch):
    state = setup(env)
    assert state["release_label"] == "Group"
    assert state["version"] == 1
    assert env.remote.calls == []
    with env.sync() as s:
        keys = [s.get(File, i).web_publisher_key for i in (1, 2)]
    assert keys[0] != keys[1] and all(k.startswith("subineko:") for k in keys)
    service.save(
        env.pid,
        SaveRequest(
            version=1,
            config=env.config,
            mappings=[
                Mapping(file_id=i, episode_number=i, name_english=f"Film {i}")
                for i in (1, 2)
            ],
        ),
    )
    with env.sync() as s:
        assert [s.get(File, i).web_publisher_key for i in (1, 2)] == keys
    with pytest.raises(PublishingError, match="changed"):
        service.save(env.pid, SaveRequest(version=0, config=env.config))
    monkeypatch.setattr(settings, "subineko_web_url", "https://other.example")
    with pytest.raises(PublishingError, match="destination changed"):
        service.state(env.pid)


@pytest.mark.asyncio
@pytest.mark.parametrize("save_ass", [True, False])
async def test_movie_subset_repeated_noop_never_skips_upload(env, save_ass):
    env.opts.publish_save_ass_files = save_ass
    setup(env)
    first = run(env)
    assert first["status"] == "succeeded", first
    assert [r["outcome"] for r in first["result"]["files"]] == ["created", "created"]
    second = run(env, release_label="Different", changelog="Changed")
    assert second["status"] == "succeeded", second
    assert [r["outcome"] for r in second["result"]["files"]] == ["noop", "noop"]
    assert [path for _, path, _ in env.remote.calls if path.endswith("/subtitles")] == [
        "/api/v1/episodes/episode-1/subtitles",
        "/api/v1/episodes/episode-2/subtitles",
    ] * 2
    assert (
        env.remote.snapshot["episodes"][0]["current_subtitle"]["release_label"]
        == "Group"
    )
    with env.sync() as s:
        p = s.get(Project, env.pid)
        assert p.publish_state is None and p.published_revision is None
        assert s.get(File, 3).status == "ready"
        assert s.get(File, 1).web_identity_locked_at
    assert service.state(env.pid)["state"] == "published"


@pytest.mark.asyncio
async def test_partial_upload_retains_identity_receipt_and_locks(env):
    setup(env)
    env.remote.fail_upload = "episode-2"
    result = run(env)
    assert result["status"] == "failed"
    with env.sync() as s:
        assert s.get(File, 1).web_published_sha256
        assert not s.get(File, 2).web_published_sha256
        assert s.get(File, 2).web_episode_uuid == "episode-2"
        assert s.get(File, 2).web_identity_locked_at
    with pytest.raises(PublishingError, match="locked"):
        service.save(
            env.pid,
            SaveRequest(
                version=1,
                config=env.config,
                mappings=[Mapping(file_id=2, episode_number=9)],
            ),
        )
    env.remote.fail_upload = None
    assert run(env)["status"] == "succeeded"


@pytest.mark.asyncio
async def test_remote_deletion_requires_recovery_no_recreation(env):
    setup(env)
    assert run(env)["status"] == "succeeded"
    env.remote.snapshot["episodes"].pop()
    previous = len(env.remote.calls)
    result = run(env)
    assert result["error_code"] == "web_recovery_required"
    assert all(method == "GET" for method, _, _ in env.remote.calls[previous:])


@pytest.mark.asyncio
async def test_concurrent_edit_uploads_snapshot_and_shows_changes(env):
    setup(env)

    def edit():
        with env.sync() as s:
            e = s.scalar(select(SubtitleEvent).where(SubtitleEvent.file_id == 2))
            e.translated_text = "Novější editace"
            s.commit()

    env.remote.hook = edit
    assert run(env)["status"] == "succeeded"
    assert service.state(env.pid)["state"] == "changes_available"


@pytest.mark.asyncio
async def test_atomic_claim_cancel_fence_archive_and_reconcile(env):
    from app.db.archive_state import ArchiveNotAllowed, archive_project

    setup(env)
    body = PublishRequest(version=1, file_ids=[1], release_label="Group")

    async def enqueue(*_a, **kwargs):
        async with env.asess() as s:
            job = JobRecord(
                job_type="publish_web",
                project_id=env.pid,
                status="queued",
                dedupe_key=kwargs["dedupe_key"],
                payload_json=service.dumps(kwargs["payload"]),
            )
            s.add(job)
            await s.commit()
            return job

    await service.start(env.pid, body, enqueue)
    with pytest.raises(PublishingError):
        await service.start(env.pid, body, enqueue)
    with pytest.raises(ArchiveNotAllowed):
        await archive_project(env.pid)
    with env.sync() as s:
        j = s.scalar(select(JobRecord))
        j.status = "cancelled"
        s.commit()
    service._live.add((env.pid, 1))
    await service.reconcile()
    with env.sync() as s:
        assert s.get(Project, env.pid).web_publish_state == "publishing"
    service._live.clear()
    await service.reconcile()
    with env.sync() as s:
        assert s.get(Project, env.pid).web_publish_state == "failed"


@pytest.mark.asyncio
async def test_duplicate_mapping_and_archived_rejection(env, monkeypatch):
    tv = env.config.model_copy(
        update={
            "media_type": "tv",
            "episode_structure": "SEASONED",
            "season": {"external_id": "500", "season_number": 0},
        }
    )
    tv = WebConfig.model_validate(tv.model_dump())
    monkeypatch.setattr(
        TmdbClient,
        "catalog",
        lambda *_a, **_k: {
            "seasons": [tv.season.model_dump()],
            "episodes_by_season": {"500": [{"tmdb_episode_id": "501"}]},
        },
    )
    with pytest.raises(PublishingError, match="same episode"):
        service.save(
            env.pid,
            SaveRequest(
                version=0,
                config=tv,
                mappings=[
                    Mapping(file_id=i, tmdb_episode_id="501", episode_number=i)
                    for i in (1, 2)
                ],
            ),
        )
    with env.sync() as s:
        p = s.get(Project, env.pid)
        p.archived_at = service.now()
        s.commit()
    with pytest.raises(PublishingError, match="Unarchive"):
        service.save(env.pid, SaveRequest(version=0, config=env.config))


def test_http_exact_retry_and_sanitized_errors(monkeypatch):
    monkeypatch.setattr(settings, "subineko_web_url", "https://web.example")
    monkeypatch.setattr(settings, "subineko_web_token", "do-not-leak")
    monkeypatch.setattr("app.publishing.web_client.time.sleep", lambda _: None)
    bodies = []

    def respond(request):
        bodies.append(request.read())
        return httpx.Response(
            503 if len(bodies) == 1 else 200, json={"outcome": "noop", "revision": {}}
        )

    client = WebClient()
    client.client.close()
    client.client = httpx.Client(
        base_url="https://web.example", transport=httpx.MockTransport(respond)
    )
    result = client.upload(
        "/api/v1/episodes/x/subtitles",
        "a.ass",
        b"exact\r\nbytes",
        {"release_label": "Group", "changelog": ""},
    )
    assert result["outcome"] == "noop"
    # Multipart boundaries may differ; fields and file bytes are identical.
    assert all(b"exact\r\nbytes" in b and b"Group" in b for b in bodies)
    client.client.close()
    client.client = httpx.Client(
        base_url="https://web.example",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(401, json={"error": "do-not-leak"})
        ),
    )
    with pytest.raises(PublishingError) as exc:
        client.get_title("x")
    assert "do-not-leak" not in str(exc.value)
    client.close()


@pytest.mark.parametrize(
    "filename,expected",
    [
        ("[SubsPlease] Film [CRC].mkv", "SubsPlease"),
        ("Film [CRC].mkv", ""),
        ("[ ] Film", ""),
    ],
)
def test_release(filename, expected):
    assert service.release_suggestion(filename) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("group", [False, True])
async def test_seasoned_standard_specials_and_group(env, monkeypatch, group):
    config = WebConfig.model_validate(
        {
            **env.config.model_dump(),
            "media_type": "tv",
            "episode_structure": "SEASONED",
            "order_kind": "episode_group" if group else "standard",
            "group_id": "group" if group else None,
            "season": {
                "external_id": "subgroup" if group else "500",
                "season_number": 1 if group else 0,
                "name_english": "Arc" if group else "Specials",
            },
        }
    )
    monkeypatch.setattr(
        TmdbClient,
        "catalog",
        lambda *_a, **_k: {
            "seasons": [config.season.model_dump()],
            "episodes_by_season": {
                config.season.external_id: [
                    {"tmdb_episode_id": str(500 + i)} for i in (1, 2)
                ]
            },
        },
    )
    service.save(
        env.pid,
        SaveRequest(
            version=0,
            config=config,
            mappings=[
                Mapping(
                    file_id=i,
                    tmdb_episode_id=str(500 + i),
                    episode_number=i,
                    name_english=f"Episode {i}",
                )
                for i in (1, 2)
            ],
        ),
    )
    result = run(env)
    assert result["status"] == "succeeded", result
    season = env.remote.snapshot["seasons"][0]
    assert season["mappings"][0]["external_id"] == config.season.external_id
    assert season["mappings"][0]["kind"] == (
        "episode_group_subgroup" if group else "season"
    )
    assert not env.remote.snapshot["episodes"]
    # Provider episodes disappear: saved confirmations still publish unchanged.
    monkeypatch.setattr(
        TmdbClient,
        "catalog",
        lambda *_a, **_k: pytest.fail("Confirmed mapping must not require TMDB"),
    )
    assert run(env)["status"] == "succeeded"


@pytest.mark.asyncio
async def test_uncertain_hierarchy_creation_reconciles_without_new_identities(
    env, monkeypatch
):
    setup(env)
    original = WebClient.metadata

    def uncertain(client, path, body):
        result = original(client, path, body)
        if path.endswith("/episodes"):
            raise PublishingError("web_uncertain", "Acknowledgement lost")
        return result

    monkeypatch.setattr(WebClient, "metadata", uncertain)
    assert run(env)["error_code"] == "web_uncertain"
    ids = [e["id"] for e in env.remote.snapshot["episodes"]]
    with env.sync() as s:
        assert s.get(File, 1).web_identity_locked_at
        assert not s.get(File, 1).web_episode_uuid
    monkeypatch.setattr(WebClient, "metadata", original)
    assert run(env)["status"] == "succeeded"
    assert [e["id"] for e in env.remote.snapshot["episodes"]] == ids


@pytest.mark.asyncio
async def test_cancellation_stops_between_uploads_but_keeps_acknowledgement(env):
    setup(env)

    def cancel():
        with env.sync() as s:
            p = s.get(Project, env.pid)
            assert p.web_publish_state == "publishing"
            assert (env.pid, p.web_publish_attempt) in service._live
            job = s.scalar(select(JobRecord).where(JobRecord.job_type == "publish_web"))
            job.status = "cancelled"
            s.commit()

    env.remote.hook = cancel
    result = run(env)
    assert result["error_code"] == "web_cancelled"
    assert len(result["result"]["files"]) == 1
    with env.sync() as s:
        assert s.get(File, 1).web_published_sha256
        assert not s.get(File, 2).web_published_sha256
        assert s.get(Project, env.pid).web_publish_state == "failed"
    assert not service._live


@pytest.mark.asyncio
@pytest.mark.parametrize("poster_fails", [False, True])
async def test_poster_requested_failure_reported_without_discarding_subtitles(
    env, monkeypatch, poster_fails
):
    config = env.config.model_copy(update={"poster_path": "/poster.jpg"})
    service.save(
        env.pid,
        SaveRequest(
            version=0,
            config=config,
            mappings=[
                Mapping(file_id=i, episode_number=i, name_english=f"Film {i}")
                for i in (1, 2)
            ],
        ),
    )

    def poster(*_args):
        if poster_fails:
            raise PublishingError("poster_request", "Poster unavailable")
        return b"mock poster"

    monkeypatch.setattr(TmdbClient, "poster", poster)
    result = run(env)
    assert result["result"]["poster"]["outcome"] == (
        "failed" if poster_fails else "created"
    )
    assert len(result["result"]["files"]) == 2
    assert result["status"] == ("failed" if poster_fails else "succeeded")
    # A later omitted provider poster never discards the approval.
    updated = service.save(env.pid, SaveRequest(version=1, config=env.config))
    assert updated["config"]["poster_path"] == "/poster.jpg"


@pytest.mark.asyncio
async def test_missing_czech_language_no_job_and_release_validation(env):
    from pydantic import ValidationError

    setup(env)
    env.opts.target_lang_code = "en"
    with pytest.raises(PublishingError, match="Czech"):
        await service.start(
            env.pid,
            PublishRequest(version=1, file_ids=[1], release_label="Group"),
            None,
        )
    for label in ("", " ", "x" * 201):
        with pytest.raises(ValidationError):
            PublishRequest(version=1, file_ids=[1], release_label=label)


@pytest.mark.asyncio
async def test_existing_remote_order_conflict_rejected_before_mutation(env):
    env.remote.snapshot = {
        "id": "existing",
        "media_type": "movie",
        "episode_structure": "FLAT",
        "episode_order": {"kind": "episode_group", "group_id": "group"},
    }
    with pytest.raises(PublishingError, match="immutable"):
        setup(env)
    assert not env.remote.calls


@pytest.mark.asyncio
async def test_uncommitted_provisional_lock_can_be_reconciled_to_draft(env):
    setup(env)
    with env.sync() as s:
        p = s.get(Project, env.pid)
        envelope = service.load(p)
        envelope["pending_title"] = True
        p.web_config_json = service.dumps(envelope)
        p.web_identity_locked_at = service.now()
        s.commit()
    assert service.remote_state(env.pid) is None
    assert not service.state(env.pid)["locked"]


@pytest.mark.asyncio
@pytest.mark.parametrize("save_ass", [True, False])
async def test_download_and_web_export_identical_without_media(env, monkeypatch, save_ass):
    env.opts.publish_save_ass_files = save_ass
    from unittest.mock import AsyncMock

    from app.api.routes.projects import download_file_subtitles
    from app.subs.ass_export import export_ass

    monkeypatch.setattr("app.api.routes.projects.AsyncSessionLocal", env.asess)
    monkeypatch.setattr(
        "app.api.routes.projects.options_store.asnapshot",
        AsyncMock(return_value=env.opts),
    )
    downloaded = await download_file_subtitles(env.pid, 1, "translated")
    with env.sync() as s:
        filename, content = export_ass(s, s.get(File, 1), env.opts)
    assert filename == "[Group] episode 1.ass"
    assert downloaded.body == content
    assert "Ahoj světe" in content.decode("utf-8")


@pytest.mark.asyncio
async def test_config_api_exposes_boolean_only_and_disabled_endpoint(env, monkeypatch):
    from app.main import create_app

    app = create_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://local"
    ) as client:
        response = await client.get("/api/config")
        assert response.json() == {"web_publishing_available": True}
        monkeypatch.setattr(settings, "subineko_web_token", "")
        response = await client.get(f"/api/projects/{env.pid}/web")
        assert response.status_code == 503
        assert "test-token-do-not-leak" not in response.text


@pytest.mark.asyncio
async def test_stale_remote_lookup_cannot_bind_a_newer_mapping(env, monkeypatch):
    setup(env)
    with env.sync() as session:
        key = session.get(File, 1).web_publisher_key
    snapshot = {
        "id": "existing-title",
        "media_type": "movie",
        "episode_structure": "FLAT",
        "episode_order": {"provider": "tmdb", "kind": "standard", "group_id": None},
        "seasons": [],
        "episodes": [{"id": "old-episode", "publisher_key": key, "mappings": []}],
    }

    def concurrent_save(_config):
        with env.sync() as session:
            project = session.get(Project, env.pid)
            project.web_config_version += 1
            session.get(File, 1).web_publisher_key = "subineko:new-draft"
            session.commit()
        return snapshot

    monkeypatch.setattr(service, "lookup", concurrent_save)
    service.remote_state(env.pid)
    with env.sync() as session:
        file = session.get(File, 1)
        assert file.web_publisher_key == "subineko:new-draft"
        assert file.web_episode_uuid is None
        assert session.get(Project, env.pid).web_config_version == 2


@pytest.mark.asyncio
async def test_reopening_active_job_restores_exact_selection(env):
    setup(env)
    result = run(
        env, file_ids=[2], release_label="Selected", changelog="Only second file"
    )
    assert result["status"] == "succeeded"
    state = service.state(env.pid)
    assert state["job"]["file_ids"] == [2]
    assert state["job"]["release_label"] == "Selected"
    assert state["job"]["changelog"] == "Only second file"


@pytest.mark.asyncio
async def test_cleanup_failure_cannot_leave_a_dead_worker_live(env, monkeypatch):
    setup(env)

    def failed_close(_client):
        raise RuntimeError("Simulated close failure")

    monkeypatch.setattr(WebClient, "close", failed_close)
    with pytest.raises(RuntimeError, match="close failure"):
        run(env)
    assert not service._live


def test_http_request_logs_do_not_expose_publisher_configuration(monkeypatch, caplog):
    import logging

    monkeypatch.setattr(
        settings, "subineko_web_url", "https://private-publisher.example"
    )
    monkeypatch.setattr(settings, "subineko_web_token", "private-bearer-token")
    with caplog.at_level(logging.INFO, logger="httpx"):
        logging.getLogger("httpx").info(
            "HTTP Request: PUT %s/api/v1/titles token=%s",
            settings.subineko_web_url,
            settings.subineko_web_token,
        )
    assert "private-publisher.example" not in caplog.text
    assert "private-bearer-token" not in caplog.text
