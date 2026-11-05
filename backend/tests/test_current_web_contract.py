"""Current entity API boundary: no website/contract changes or real requests."""

import copy
import hashlib
import json
from email import policy
from email.parser import BytesParser
from urllib.parse import unquote

import httpx
import pytest

from app.core.config import settings
from app.publishing.schemas import WebConfig
from app.publishing.web_client import PublishingError, WebClient


class Receiver:
    """Strict in-memory implementation of the current website's wire shapes."""

    def __init__(self):
        self.titles = {}
        self.calls = []
        self.uncertain_episode = False

    @staticmethod
    def multipart(request):
        message = BytesParser(policy=policy.default).parsebytes(
            f"Content-Type: {request.headers['content-type']}\r\nMIME-Version: 1.0\r\n\r\n".encode()
            + request.content
        )
        return {
            part.get_param("name", header="content-disposition"): part
            for part in message.iter_parts()
        }

    def __call__(self, request):
        path = [unquote(v) for v in request.url.path.split("/")[4:]]
        self.calls.append((request.method, request.url.path, request.content))
        assert request.headers["Authorization"] == "Bearer isolated-token"
        assert not request.url.query  # obsolete provider-lookup API is forbidden
        title = self.titles.get(path[0]) if path else None
        if request.method == "GET":
            if len(path) == 1:
                return (
                    httpx.Response(200, json=copy.deepcopy(title))
                    if title
                    else httpx.Response(404)
                )
            if path[-1] == "poster":
                return (
                    httpx.Response(200, json=title["currentPoster"])
                    if title and title.get("currentPoster")
                    else httpx.Response(404)
                )
            raise AssertionError(request.url)
        assert request.method == "PUT"
        if len(path) == 1:
            body = json.loads(request.content)
            assert set(body) == {
                "mediaType",
                "translationProgress",
                "titleEnglish",
                "titleRomaji",
                "year",
                "originalLanguage",
                "descriptionEnglish",
            }
            assert type(body["translationProgress"]) is int
            created = title is None
            if created:
                title = self.titles[path[0]] = {
                    "id": f"title-{len(self.titles)}",
                    "externalId": path[0],
                    "slug": path[0],
                    "seasons": [],
                    "episodes": [],
                }
            title.update(body)
            return httpx.Response(
                201 if created else 200, json={"id": title["id"], "externalId": path[0]}
            )
        assert title is not None
        if path[-1] == "poster":
            parts = self.multipart(request)
            assert set(parts) == {"file"}
            image = parts["file"]
            content = image.get_payload(decode=True)
            poster = {
                "sha256": hashlib.sha256(content).hexdigest(),
                "contentType": image.get_content_type(),
                "sizeBytes": len(content),
            }
            title["currentPoster"] = poster
            return httpx.Response(201, json={"poster": poster})
        if path[1] == "seasons":
            body = json.loads(request.content)
            assert set(body) == {"seasonNumber", "nameEnglish", "year"}
            found = next(
                (s for s in title["seasons"] if s["externalId"] == path[2]), None
            )
            created = found is None
            if created:
                found = {
                    "id": f"season-{len(title['seasons'])}",
                    "externalId": path[2],
                    "episodes": [],
                }
                title["seasons"].append(found)
            found.update(body)
            return httpx.Response(
                201 if created else 200, json={"id": found["id"], "externalId": path[2]}
            )
        if path[1] == "episodes" and len(path) == 3:
            body = json.loads(request.content)
            assert set(body) == {"episodeNumber", "nameEnglish", "seasonExternalId"}
            all_episodes = title["episodes"] + [
                e for s in title["seasons"] for e in s["episodes"]
            ]
            found = next((e for e in all_episodes if e["externalId"] == path[2]), None)
            created = found is None
            if created:
                found = {"id": f"episode-{len(all_episodes)}", "externalId": path[2]}
                destination = next(
                    (
                        s["episodes"]
                        for s in title["seasons"]
                        if s["externalId"] == body["seasonExternalId"]
                    ),
                    title["episodes"],
                )
                destination.append(found)
            found.update(body)
            if self.uncertain_episode:
                self.uncertain_episode = False
                raise httpx.ReadError("lost acknowledgement", request=request)
            return httpx.Response(
                201 if created else 200, json={"id": found["id"], "externalId": path[2]}
            )
        if path[-1] == "subtitles":
            parts = self.multipart(request)
            assert {"file", "releaseLabel", "revisionNumber"} <= set(parts)
            assert set(parts) <= {"file", "releaseLabel", "revisionNumber", "changelog"}
            episode = next(
                e
                for e in title["episodes"]
                + [e for s in title["seasons"] for e in s["episodes"]]
                if e["externalId"] == path[2]
            )
            revision = int(parts["revisionNumber"].get_content())
            assert (
                revision
                == (episode.get("currentSubtitle") or {}).get("revisionNumber", 0) + 1
            )
            subtitle = {
                "sha256": hashlib.sha256(
                    parts["file"].get_payload(decode=True)
                ).hexdigest(),
                "revisionNumber": revision,
                "releaseLabel": parts["releaseLabel"].get_content(),
                "originalFilename": parts["file"].get_filename(),
                "changelog": parts["changelog"].get_content()
                if "changelog" in parts
                else None,
                "updatedAt": "2026-11-05T00:00:00Z",
            }
            episode["currentSubtitle"] = subtitle
            return httpx.Response(201, json={"subtitle": subtitle})
        raise AssertionError(request.url)


@pytest.fixture
def boundary(monkeypatch):
    monkeypatch.setattr(settings, "subineko_web_url", "https://isolated.example")
    monkeypatch.setattr(settings, "subineko_web_token", "isolated-token")
    monkeypatch.setattr(settings, "tmdb_api_token", "isolated-token")
    monkeypatch.setattr("app.publishing.web_client.time.sleep", lambda _: None)
    receiver = Receiver()
    client = WebClient()
    client.client.close()
    client.client = httpx.Client(
        base_url="https://isolated.example",
        headers={"Authorization": "Bearer isolated-token"},
        transport=httpx.MockTransport(receiver),
    )
    config = WebConfig(
        tmdb_title_id="100",
        media_type="tv",
        episode_structure="SEASONED",
        season={"external_id": "501", "season_number": 1},
        metadata={"title_english": "Anime", "original_language": "ja"},
    )
    yield client, receiver, config
    client.close()


def title_body(config):
    return {**config.metadata.website_payload(), "media_type": config.media_type}


def catalog_body():
    return {
        "season_number": 1,
        "name_english": "Season 1",
        "year": 2025,
        "mappings": [{"external_id": "501"}],
        "episodes": [
            {
                "episode_number": i,
                "name_english": f"Episode {i}",
                "mappings": [{"external_id": str(900 + i)}],
            }
            for i in range(1, 14)
        ],
    }


def publish_catalog(client, config):
    client.lookup(config)
    title = client.metadata("/api/v1/titles", title_body(config))["title"]
    return client.metadata(f"/api/v1/titles/{title['id']}/seasons", catalog_body())


def test_metadata_only_all_episodes_entity_paths_and_repeat_noops(boundary):
    client, receiver, config = boundary
    result = publish_catalog(client, config)
    assert result["outcome"] == "created"
    assert len(result["title"]["seasons"][0]["episodes"]) == 13
    assert result["title"]["translation_progress"] == 0
    assert len([c for c in receiver.calls if c[0] == "PUT"]) == 15
    assert not any(path.endswith("/subtitles") for _, path, _ in receiver.calls)
    assert publish_catalog(client, config)["outcome"] == "noop"
    assert len([c for c in receiver.calls if c[0] == "PUT"]) == 15
    assert len(receiver.titles) == 1


def test_existing_bare_tmdb_title_reused_and_metadata_does_not_reset_progress(boundary):
    client, receiver, config = boundary
    publish_catalog(client, config)
    existing = receiver.titles.pop("tmdb-tv-100")
    existing["externalId"] = "100"
    existing["translationProgress"] = 73
    receiver.titles["100"] = existing
    snapshot = client.lookup(config)
    assert snapshot["id"] == existing["id"]
    assert (
        client.metadata(
            f"/api/v1/titles/{snapshot['id']}",
            {**title_body(config), "title_english": "Approved edit"},
        )["title"]["translation_progress"]
        == 73
    )
    assert len(receiver.titles) == 1


def test_progress_put_only_percentage_and_latest_descriptive_metadata(boundary):
    client, receiver, config = boundary
    publish_catalog(client, config)
    receiver.titles["tmdb-tv-100"]["descriptionEnglish"] = "New remote edit"
    assert client.update_progress(config, 46) == {"outcome": "updated", "percent": 46}
    body = json.loads([c[2] for c in receiver.calls if c[0] == "PUT"][-1])
    assert body["translationProgress"] == 46
    assert body["descriptionEnglish"] == "New remote edit"
    assert not any("count" in k.lower() or "status" in k.lower() for k in body)
    assert client.update_progress(config, 46)["outcome"] == "noop"


def test_bare_numeric_alias_of_other_media_type_is_not_rebound(boundary):
    client, receiver, config = boundary
    publish_catalog(client, config)
    existing = receiver.titles.pop("tmdb-tv-100")
    existing["externalId"] = "100"
    receiver.titles["100"] = existing
    movie = WebConfig(
        tmdb_title_id="100",
        media_type="movie",
        episode_structure="FLAT",
        metadata={"title_english": "Movie", "original_language": "ja"},
    )
    assert client.lookup(movie) is None
    client.metadata("/api/v1/titles", title_body(movie))
    assert receiver.titles["100"]["mediaType"] == "TV"
    assert receiver.titles["tmdb-movie-100"]["mediaType"] == "MOVIE"


def test_lost_episode_acknowledgement_retries_same_external_identity(boundary):
    client, receiver, config = boundary
    receiver.uncertain_episode = True
    result = publish_catalog(client, config)
    assert len(result["title"]["seasons"][0]["episodes"]) == 13
    ids = [e["id"] for e in result["title"]["seasons"][0]["episodes"]]
    assert len(set(ids)) == 13
    assert (
        len(
            [
                c
                for c in receiver.calls
                if c[0] == "PUT" and c[1].endswith("tmdb-episode-901")
            ]
        )
        == 2
    )


def test_current_multipart_external_urls_and_hash_noop(boundary):
    client, receiver, config = boundary
    result = publish_catalog(client, config)
    episode = result["title"]["seasons"][0]["episodes"][0]
    upload = client.upload(
        f"/api/v1/episodes/{episode['id']}/subtitles",
        "episode.ass",
        b"ASS-content",
        {"release_label": "BD"},
    )
    assert upload["outcome"] == "created"
    assert (
        client.upload(
            f"/api/v1/episodes/{episode['id']}/subtitles",
            "episode.ass",
            b"ASS-content",
            {"release_label": "BD"},
        )["outcome"]
        == "noop"
    )
    assert (
        len(
            [c for c in receiver.calls if c[0] == "PUT" and c[1].endswith("/subtitles")]
        )
        == 1
    )


def test_current_poster_mime_receipt_and_hash_noop(boundary):
    client, receiver, config = boundary
    result = publish_catalog(client, config)
    path = f"/api/v1/titles/{result['title']['id']}/poster"
    assert client.upload(path, "poster.webp", b"WEBP-content")["outcome"] == "created"
    assert client.upload(path, "poster.webp", b"WEBP-content")["outcome"] == "noop"
    assert (
        len([c for c in receiver.calls if c[0] == "PUT" and c[1].endswith("/poster")])
        == 1
    )


def test_changed_subtitle_bytes_increment_revision_and_identical_bytes_preserve_it(
    boundary,
):
    client, receiver, config = boundary
    result = publish_catalog(client, config)
    episode = result["title"]["seasons"][0]["episodes"][0]
    path = f"/api/v1/episodes/{episode['id']}/subtitles"
    assert (
        client.upload(path, "first.ass", b"first bytes", {"release_label": "BD"})[
            "revision"
        ]["revisionNumber"]
        == 1
    )
    second = client.upload(
        path,
        "second.ass",
        b"second bytes",
        {"release_label": "WEB", "changelog": "Correction"},
    )
    assert second["outcome"] == "updated"
    assert second["revision"]["revisionNumber"] == 2
    assert second["revision"]["sha256"] == hashlib.sha256(b"second bytes").hexdigest()
    assert second["revision"]["releaseLabel"] == "WEB"
    assert second["revision"]["changelog"] == "Correction"
    third = client.upload(
        path, "ignored.ass", b"second bytes", {"release_label": "Ignored"}
    )
    assert third["outcome"] == "noop"
    assert third["revision"]["revisionNumber"] == 2
    assert third["revision"]["releaseLabel"] == "WEB"
    assert (
        len(
            [c for c in receiver.calls if c[0] == "PUT" and c[1].endswith("/subtitles")]
        )
        == 2
    )


def test_two_projects_same_title_keep_seasons_and_do_not_aggregate_progress(boundary):
    client, receiver, config = boundary
    publish_catalog(client, config)
    client.update_progress(config, 50)
    second = config.model_copy(
        update={
            "season": config.season.model_copy(
                update={"external_id": "502", "season_number": 2}
            )
        }
    )
    client.lookup(second)
    body = catalog_body()
    body["season_number"] = 2
    body["mappings"] = [{"external_id": "502"}]
    body["episodes"] = [
        {
            "episode_number": 1,
            "name_english": "S2E1",
            "mappings": [{"external_id": "999"}],
        }
    ]
    client.metadata("/api/v1/titles/unused/seasons", body)
    client.update_progress(second, 25)
    assert len(receiver.titles) == 1
    assert len(receiver.titles["tmdb-tv-100"]["seasons"]) == 2
    assert receiver.titles["tmdb-tv-100"]["translationProgress"] == 25


def test_does_not_move_bound_episode_to_other_season(boundary):
    client, _, config = boundary
    publish_catalog(client, config)
    body = catalog_body()
    body["mappings"] = [{"external_id": "502"}]
    with pytest.raises(PublishingError, match="different season"):
        client.metadata("/api/v1/titles/unused/seasons", body)


@pytest.mark.parametrize("stage", ["before_write", "receipt", "after_write"])
def test_title_receipt_change_fails_closed_on_every_write_boundary(
    boundary, monkeypatch, stage
):
    client, receiver, config = boundary
    published = publish_catalog(client, config)["title"]
    title = receiver.titles["tmdb-tv-100"]
    if stage == "before_write":
        title["id"] = "replacement-title"
    else:
        request = client.request

        def changed(method, path, **kwargs):
            result = request(method, path, **kwargs)
            if method == "PUT" and path == "/api/v1/titles/tmdb-tv-100":
                if stage == "receipt":
                    return {**result, "id": "replacement-title"}
                title["id"] = "replacement-title"
            return result

        monkeypatch.setattr(client, "request", changed)
    puts = len([c for c in receiver.calls if c[0] == "PUT"])
    with pytest.raises(PublishingError, match="changed") as error:
        client.metadata(
            f"/api/v1/titles/{published['id']}",
            {**title_body(config), "title_english": "Changed title"},
        )
    assert error.value.code == "web_recovery_required"
    assert client.raw_title["id"] == published["id"]
    if stage == "before_write":
        assert len([c for c in receiver.calls if c[0] == "PUT"]) == puts


@pytest.mark.parametrize("entity", ["season", "episode"])
def test_bound_hierarchy_uuid_change_is_rejected_before_put(boundary, entity):
    client, receiver, config = boundary
    published = publish_catalog(client, config)["title"]["seasons"][0]
    body = catalog_body()
    body["id"] = published["id"]
    body["episodes"][0]["id"] = published["episodes"][0]["id"]
    remote = receiver.titles["tmdb-tv-100"]["seasons"][0]
    (remote if entity == "season" else remote["episodes"][0])["id"] = "replaced-uuid"
    puts = len([c for c in receiver.calls if c[0] == "PUT"])
    with pytest.raises(PublishingError) as error:
        client.metadata("/api/v1/titles/unused/seasons", body)
    assert error.value.code == "web_recovery_required"
    assert len([c for c in receiver.calls if c[0] == "PUT"]) == puts


def test_title_metadata_reads_newest_remote_progress_and_keeps_progress_update(
    boundary,
):
    client, receiver, config = boundary
    publish_catalog(client, config)
    receiver.titles["tmdb-tv-100"]["translationProgress"] = 73
    client.metadata(
        "/api/v1/titles/unused",
        {**title_body(config), "title_english": "New approved title"},
    )
    assert receiver.titles["tmdb-tv-100"]["translationProgress"] == 73
    client.update_progress(config, 46)
    client.metadata(
        "/api/v1/titles/unused",
        {**title_body(config), "title_english": "Another approved title"},
    )
    assert client.raw_title["translationProgress"] == 46
    assert receiver.titles["tmdb-tv-100"]["translationProgress"] == 46


def test_canonical_episode_and_season_aliases_win_over_earlier_bare_duplicates(
    boundary,
):
    client, receiver, config = boundary
    publish_catalog(client, config)
    title = receiver.titles["tmdb-tv-100"]
    title["episodes"].insert(
        0,
        {
            "id": "legacy-episode",
            "externalId": "901",
            "seasonExternalId": None,
            "episodeNumber": 1,
            "nameEnglish": "Legacy",
        },
    )
    title["seasons"].insert(
        0,
        {
            "id": "legacy-season",
            "externalId": "501",
            "seasonNumber": 1,
            "nameEnglish": "Legacy",
            "year": None,
            "episodes": [],
        },
    )
    body = catalog_body()
    body["episodes"][0]["name_english"] = "Approved episode"
    snapshot = client.metadata("/api/v1/titles/unused/seasons", body)["title"]
    assert title["seasons"][1]["episodes"][0]["nameEnglish"] == "Approved episode"
    assert title["episodes"][0]["nameEnglish"] == "Legacy"
    assert snapshot["episodes"][0]["mappings"] == []
    assert snapshot["seasons"][0]["mappings"] == []


def test_flat_movie_and_episode_group_use_current_entity_contract(boundary):
    client, receiver, _ = boundary
    movie = WebConfig(
        tmdb_title_id="200",
        media_type="movie",
        episode_structure="FLAT",
        metadata={"title_english": "Movie", "original_language": "ja"},
    )
    client.lookup(movie)
    client.metadata("/api/v1/titles", title_body(movie))
    result = client.metadata(
        "/api/v1/titles/unused/episodes",
        {
            "episodes": [
                {
                    "episode_number": 1,
                    "name_english": "Movie",
                    "publisher_key": "subineko:stable-content",
                }
            ]
        },
    )
    assert result["title"]["episodes"][0]["publisher_key"] == "subineko:stable-content"
    assert receiver.titles["tmdb-movie-200"]["episodes"][0]["seasonExternalId"] is None
    group = WebConfig(
        tmdb_title_id="300",
        media_type="tv",
        order_kind="episode_group",
        group_id="group-a",
        season={"external_id": "subgroup-b", "season_number": 1},
        metadata={"title_english": "Grouped", "original_language": "ja"},
    )
    client.lookup(group)
    client.metadata("/api/v1/titles", title_body(group))
    body = catalog_body()
    body["mappings"] = [{"external_id": "subgroup-b"}]
    result = client.metadata("/api/v1/titles/unused/seasons", body)
    assert (
        result["title"]["seasons"][0]["external_id"] == "tmdb-group-group-a-subgroup-b"
    )
    assert len(result["title"]["seasons"][0]["episodes"]) == 13


def test_real_job_metadata_only_without_files_records_all_receipts(
    boundary, tmp_path, monkeypatch
):
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import sessionmaker

    from app.core.database import Base
    from app.db.models import File, JobRecord, Project, ProjectEpisode
    from app.db.options import AppOptions
    from app.jobs.context import JobContext
    from app.jobs.handlers import publish_web as handler
    from app.publishing import service

    client, receiver, config = boundary
    engine = create_engine(f"sqlite:///{tmp_path / 'isolated.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(service, "SyncSessionLocal", sessions)
    monkeypatch.setattr(handler, "SyncSessionLocal", sessions)

    def factory(check):
        client.check = check
        return client

    monkeypatch.setattr(handler, "WebClient", factory)
    options = AppOptions(target_lang_name="Czech", target_lang_code="cs")
    with sessions() as session:
        project = Project(
            name="Anime",
            source_directory="not-real",
            anime_provider="anidb",
            anime_external_id="7",
            expected_episode_count=13,
            web_publish_intent="publish_and_import",
            web_publish_attempt=1,
            web_publish_state="publishing",
            web_config_json=service.dumps(
                {"config": config.model_dump(), "destination": service.destination()}
            ),
        )
        session.add(project)
        session.flush()
        pid = project.id
        payload = {
            "project_id": pid,
            "attempt": 1,
            "version": 0,
            "file_ids": [],
            "release_label": "",
            "changelog": None,
        }
        for i in range(1, 14):
            session.add(
                ProjectEpisode(
                    project_id=pid,
                    episode_number=i,
                    tmdb_episode_id=str(900 + i),
                    tmdb_name=f"Episode {i}",
                )
            )
        session.add(
            JobRecord(
                project_id=pid,
                job_type="publish_web",
                status="running",
                dedupe_key=f"publish_web:{pid}:1",
                payload_json=service.dumps(payload),
            )
        )
        session.commit()
    samples = []
    result = handler.publish_web(
        payload,
        JobContext(tmp_path / "no-media", tmp_path / "no-output", options),
        lambda pct, _: samples.append(pct),
    )
    assert result["status"] == "succeeded", result
    assert result["result"]["receipt"]["percent"] == 0
    assert samples == sorted(samples)
    assert samples[-1] == 1
    with sessions() as session:
        assert not session.scalar(select(File.id))
        rows = list(session.scalars(select(ProjectEpisode)))
        assert len(rows) == 13
        assert all(r.web_episode_uuid and r.web_identity_locked_at for r in rows)
        assert not any(r.web_subtitle_sha256 for r in rows)
    assert receiver.titles["tmdb-tv-100"]["translationProgress"] == 0
    assert len(receiver.titles["tmdb-tv-100"]["seasons"][0]["episodes"]) == 13
    engine.dispose()
