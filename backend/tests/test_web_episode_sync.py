# ruff: noqa: F811
"""Metadata-only publication, durable remote ids, subtitle receipts and progress.

Uses the normalized fake website of ``test_web_publishing`` (the real wire contract
of the current website API is covered by ``test_current_web_contract.py``).
"""

import json

import pytest
from pydantic import ValidationError

from app.db.models import File, JobRecord, Project, ProjectEpisode
from app.jobs.handlers import publish_web as handler
from app.publishing import remote as remote_mod
from app.publishing import service
from app.publishing.schemas import Mapping, PublishRequest, SaveRequest, WebConfig
from app.publishing.tmdb import TmdbClient
from app.publishing.web_client import PublishingError, WebClient
from tests.test_web_publishing import env, run, setup  # noqa: F401  (fixtures/helpers)

EXPECTED = 3


def seasoned(env, monkeypatch, mappings=None, expected=EXPECTED, season="500", count=3):
    """Save a SEASONED title; the TMDB catalog becomes the stored ProjectEpisodes."""
    config = WebConfig.model_validate(
        {
            **env.config.model_dump(),
            "media_type": "tv",
            "episode_structure": "SEASONED",
            "season": {"external_id": season, "season_number": 1, "name_english": "S1"},
        }
    )
    base = int(season)
    episodes = [
        {"tmdb_episode_id": str(base + i), "episode_number": i, "name_english": f"Ep {i}"}
        for i in range(1, count + 1)
    ]
    monkeypatch.setattr(
        TmdbClient,
        "catalog",
        lambda *_a, **_k: {
            "seasons": [config.season.model_dump()],
            "episodes_by_season": {season: episodes},
        },
    )
    version = service.state(env.pid)["version"] if env.config_saved else 0
    env.config_saved = True
    env.seasoned = config
    return service.save(
        env.pid,
        SaveRequest(
            version=version,
            config=config,
            expected_episode_count=expected,
            mappings=mappings if mappings is not None else [],
        ),
    )


def pe_rows(env):
    with env.sync() as s:
        return {
            r.episode_number: r
            for r in s.query(ProjectEpisode).filter_by(project_id=env.pid)
        }


def receipt(env):
    with env.sync() as s:
        return remote_mod.receipt_progress(s, s.get(Project, env.pid))


def metadata_only(env, **kwargs):
    return run(env, file_ids=[], release_label=None, changelog=None, **kwargs)


@pytest.fixture(autouse=True)
def _flags(env):
    env.config_saved = False


def mapping(file_id, tmdb, number):
    return Mapping(file_id=file_id, tmdb_episode_id=tmdb, episode_number=number, name_english=f"Ep {number}")


@pytest.mark.asyncio
@pytest.mark.parametrize("deleted", ["title", "season", "episode", "subtitle"])
@pytest.mark.parametrize("read_first", [False, True])
async def test_deleted_website_records_can_be_republished(env, monkeypatch, deleted, read_first):
    seasoned(env, monkeypatch, mappings=[mapping(1, "501", 1), mapping(2, "502", 2)])
    assert run(env, file_ids=[1, 2])["status"] == "succeeded"
    before = pe_rows(env)
    local_ids = {n: (p.id, p.tmdb_episode_id) for n, p in before.items()}
    if deleted == "title":
        env.remote.snapshot = None
    elif deleted == "season":
        env.remote.snapshot["seasons"].clear()
    elif deleted == "episode":
        env.remote.snapshot["seasons"][0]["episodes"].pop(0)
    else:
        env.remote.snapshot["seasons"][0]["episodes"][0]["current_subtitle"] = None
    if read_first:
        call_count = len(env.remote.calls)
        service.remote_state(env.pid)  # Opening WEB Publish must no longer fail.
        state = service.state(env.pid)
        assert state["locked"] and state["expected_episode_count"] == EXPECTED
        assert all(method == "GET" for method, _, _ in env.remote.calls[call_count:])
        # Saving Title must also succeed, even if every remote entity was deleted.
        service.save(env.pid, SaveRequest(version=state["version"], config=env.seasoned))
    result = run(env, file_ids=[1, 2])
    assert result["status"] == "succeeded", result
    episodes = env.remote.snapshot["seasons"][0]["episodes"]
    assert len(episodes) == 3
    assert all(e.get("current_subtitle") for e in episodes if e["episode_number"] in (1, 2))
    after = pe_rows(env)
    assert {n: (p.id, p.tmdb_episode_id) for n, p in after.items()} == local_ids
    assert all(p.web_episode_uuid and p.web_identity_locked_at for p in after.values())
    if deleted in ("title", "season", "episode"):
        assert after[1].web_episode_uuid != before[1].web_episode_uuid
    else:
        assert after[1].web_episode_uuid == before[1].web_episode_uuid
    assert receipt(env)["percent"] == 66
    assert metadata_only(env)["status"] == "succeeded"
    assert len(env.remote.snapshot["seasons"][0]["episodes"]) == 3


@pytest.mark.asyncio
async def test_missing_subtitle_clears_obsolete_receipt_without_implicit_upload(env, monkeypatch):
    seasoned(env, monkeypatch, mappings=[mapping(1, "501", 1), mapping(2, "502", 2)])
    assert run(env, file_ids=[1, 2])["status"] == "succeeded"
    env.remote.snapshot["seasons"][0]["episodes"][0]["current_subtitle"] = None
    calls = len(env.remote.calls)
    service.remote_state(env.pid)
    assert receipt(env)["percent"] == 33
    assert metadata_only(env)["status"] == "succeeded"
    assert not any(path.endswith("/subtitles") for _, path, _ in env.remote.calls[calls:])
    assert receipt(env)["percent"] == 33


@pytest.mark.asyncio
async def test_failed_remote_read_never_discards_confirmed_receipts(env, monkeypatch):
    seasoned(env, monkeypatch, mappings=[mapping(1, "501", 1)])
    assert run(env, file_ids=[1])["status"] == "succeeded"
    old = pe_rows(env)[1]

    def unavailable(_config):
        raise PublishingError("web_uncertain", "Network unavailable", 502)

    monkeypatch.setattr(service, "lookup", unavailable)
    with pytest.raises(PublishingError, match="Network unavailable"):
        service.remote_state(env.pid)
    current = pe_rows(env)[1]
    assert current.web_episode_uuid == old.web_episode_uuid
    assert current.web_subtitle_sha256 == old.web_subtitle_sha256


@pytest.mark.asyncio
@pytest.mark.parametrize("recreated", ["title", "season", "episode"])
async def test_reuse_current_api_records_already_recreated_by_another_project(env, monkeypatch, recreated):
    seasoned(env, monkeypatch, mappings=[mapping(1, "501", 1)])
    assert run(env, file_ids=[1])["status"] == "succeeded"
    snapshot = env.remote.snapshot
    snapshot["current_api"] = True  # WebClient validated the same external identities.
    if recreated == "title":
        snapshot["id"] = "recreated-title"
    if recreated in ("title", "season"):
        snapshot["seasons"][0]["id"] = "recreated-season"
    episode = snapshot["seasons"][0]["episodes"][0]
    episode["id"] = "recreated-episode"
    episode["current_subtitle"] = None
    service.remote_state(env.pid)
    state = service.state(env.pid)
    assert state["locked"]
    service.save(env.pid, SaveRequest(version=state["version"], config=env.seasoned))
    result = run(env, file_ids=[1])
    assert result["status"] == "succeeded", result
    assert pe_rows(env)[1].web_episode_uuid == "recreated-episode"
    assert len(snapshot["seasons"][0]["episodes"]) == 3


@pytest.mark.asyncio
async def test_recreated_episode_with_subtitles_preserves_confirmed_progress(env, monkeypatch):
    seasoned(env, monkeypatch, mappings=[mapping(1, "501", 1)])
    assert run(env, file_ids=[1])["status"] == "succeeded"
    env.remote.snapshot["current_api"] = True
    env.remote.snapshot["seasons"][0]["episodes"][0]["id"] = "already-restored-episode"
    service.remote_state(env.pid)
    assert receipt(env)["percent"] == 33
    assert metadata_only(env)["status"] == "succeeded"
    assert receipt(env)["percent"] == 33


@pytest.mark.asyncio
async def test_deletion_during_final_verification_is_retryable_not_reported_as_published(env, monkeypatch):
    seasoned(env, monkeypatch)
    original = WebClient.get_title
    removed = False

    def deleted_before_read(client, title_id):
        nonlocal removed
        if not removed:
            env.remote.snapshot["seasons"][0]["episodes"].pop(0)
            removed = True
        return original(client, title_id)

    monkeypatch.setattr(WebClient, "get_title", deleted_before_read)
    result = metadata_only(env)
    assert result["error_code"] == "web_concurrent_change"
    assert metadata_only(env)["status"] == "succeeded"
    assert len(env.remote.snapshot["seasons"][0]["episodes"]) == 3


# --------------------------------------------------------------------------
# Metadata-only publication
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_metadata_only_publishes_every_stored_episode_without_any_file(env, monkeypatch):
    state = seasoned(env, monkeypatch)
    assert [e["tmdb_episode_id"] for e in state["episodes"]] == ["501", "502", "503"]
    assert state["expected_episode_count"] == EXPECTED and state["files"][0]["mapping"] is None

    result = metadata_only(env)
    assert result["status"] == "succeeded", result
    season = env.remote.snapshot["seasons"][0]
    assert [e["mappings"][0]["external_id"] for e in season["episodes"]] == ["501", "502", "503"]
    assert not [c for c in env.remote.calls if c[1].endswith("/subtitles")]
    rows = pe_rows(env)
    assert all(r.web_episode_uuid and r.web_identity_locked_at and r.web_confirmed_at for r in rows.values())
    # Confirmed metadata is not a subtitle: a metadata-only publication is 0%.
    assert receipt(env) == {
        "expected_episode_count": 3, "subtitles_published": 0, "confirmed_episodes": 3, "percent": 0,
    }
    assert env.remote.progress == []  # no own receipts yet: remote progress untouched
    state = service.state(env.pid)
    assert state["state"] == "published" and state["job"]["metadata_only"] is True
    assert state["job"]["progress"] == 1.0 and state["receipt"]["percent"] == 0
    assert all(e["confirmed"] and e["locked"] for e in state["episodes"])


@pytest.mark.asyncio
async def test_operation_log_and_progress_are_real_and_monotonic(env, monkeypatch):
    seasoned(env, monkeypatch)
    seen = []
    original = handler.publish_web

    def run_tracking(payload, ctx, progress):
        return original(payload, ctx, lambda pct, stage: (seen.append(pct), progress(pct, stage)))

    monkeypatch.setattr(handler, "publish_web", run_tracking)
    result = metadata_only(env)
    assert result["status"] == "succeeded"
    assert seen == sorted(seen) and seen[-1] == 1.0
    ops = result["result"]["operations"]
    assert [o["stage"] for o in ops][:3] == ["Lookup", "Title", "Season"]
    assert all(o["status"] != "running" and o["finished_at"] for o in ops)
    assert {"Verify", "Progress"} <= {o["stage"] for o in ops}
    assert result["result"]["progress"] == 1.0
    with env.sync() as s:
        stored = json.loads(s.query(JobRecord).one().result_json)
    assert stored["operations"] == ops  # the UI reads exactly this from the job row


@pytest.mark.asyncio
async def test_metadata_only_request_needs_no_label_but_uploads_do():
    assert PublishRequest(version=1, file_ids=[], release_label="").metadata_only
    assert PublishRequest(version=1).release_label is None
    with pytest.raises(ValidationError):
        PublishRequest(version=1, file_ids=[1], release_label="")


# --------------------------------------------------------------------------
# Durable ids, partial success, uncertain outcomes
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_partial_hierarchy_success_keeps_confirmed_ids_and_only_unproven_pending(env, monkeypatch):
    seasoned(env, monkeypatch)
    monkeypatch.setattr(handler, "BATCH", 1)
    original = WebClient.metadata
    calls = []

    def flaky(client, path, body):
        calls.append(path)
        if len(calls) == 3:  # title, season+episode 1, then episode 2 fails for good
            raise PublishingError("web_identity_conflict", "Rejected", 502)
        return original(client, path, body)

    monkeypatch.setattr(WebClient, "metadata", flaky)
    result = metadata_only(env)
    assert result["error_code"] == "web_identity_conflict"
    rows = pe_rows(env)
    assert rows[1].web_episode_uuid and rows[1].web_identity_locked_at
    assert not rows[2].web_episode_uuid and not rows[2].web_identity_locked_at
    with env.sync() as s:
        envelope = json.loads(s.get(Project, env.pid).web_config_json)
        assert envelope["pending_hierarchy"] == ["t:502"]  # only what is still unproven
        assert s.get(Project, env.pid).web_publish_state == "failed"
    assert service.state(env.pid)["state"] == "partial" and receipt(env)["confirmed_episodes"] == 1

    monkeypatch.setattr(WebClient, "metadata", original)
    assert metadata_only(env)["status"] == "succeeded"
    with env.sync() as s:
        assert "pending_hierarchy" not in json.loads(s.get(Project, env.pid).web_config_json)
    assert receipt(env)["confirmed_episodes"] == 3
    assert [e.web_episode_uuid for e in pe_rows(env).values()] == [
        "episode-1", "episode-2", "episode-3",
    ]


@pytest.mark.asyncio
async def test_uncertain_write_without_a_readable_outcome_locks_nothing_but_freezes_identity(env, monkeypatch):
    seasoned(env, monkeypatch)
    original_meta, original_lookup = WebClient.metadata, WebClient.lookup
    state = {"lookups": 0}

    def lost_ack(client, path, body):
        result = original_meta(client, path, body)
        if "/seasons" in path:
            raise PublishingError("web_uncertain", "Acknowledgement lost")
        return result

    def lookup(client, config):
        state["lookups"] += 1
        if state["lookups"] > 1:  # the reconciling read fails as well
            raise PublishingError("web_uncertain", "still down")
        return original_lookup(client, config)

    monkeypatch.setattr(WebClient, "metadata", lost_ack)
    monkeypatch.setattr(WebClient, "lookup", lookup)
    assert metadata_only(env)["error_code"] == "web_uncertain"
    # The season PUT really committed, but nothing proved it: no episode id, no
    # lock — yet the unresolved write freezes the placement.
    assert not any(r.web_episode_uuid or r.web_identity_locked_at for r in pe_rows(env).values())
    with env.sync() as s:
        p = s.get(Project, env.pid)
        assert p.web_identity_locked_at  # the title id was proven by its own response
        assert json.loads(p.web_config_json)["pending_hierarchy"] == ["t:501", "t:502", "t:503"]
    assert service.state(env.pid)["identity_pending"] is True

    # An explicit read resolves what the website really holds.
    monkeypatch.setattr(WebClient, "lookup", original_lookup)
    service.remote_state(env.pid)
    assert all(r.web_episode_uuid for r in pe_rows(env).values())
    with env.sync() as s:
        assert "pending_hierarchy" not in json.loads(s.get(Project, env.pid).web_config_json)


@pytest.mark.asyncio
async def test_reconciling_read_keeps_unresolved_pending_refs(env, monkeypatch):
    seasoned(env, monkeypatch)
    assert metadata_only(env)["status"] == "succeeded"  # 501..503 are now proven
    with env.sync() as s:
        p = s.get(Project, env.pid)
        envelope = service.load(p)
        envelope["pending_hierarchy"] = ["t:501", "t:999"]
        p.web_config_json = service.dumps(envelope)
        s.commit()
    service.remote_state(env.pid)
    # A read proves what exists; a ref the website never shows stays pending.
    with env.sync() as s:
        assert json.loads(s.get(Project, env.pid).web_config_json)["pending_hierarchy"] == ["t:999"]


@pytest.mark.asyncio
async def test_unbound_import_mapping_binds_on_first_publish_only_with_the_intent(env, monkeypatch):
    seasoned(env, monkeypatch)

    def unbind(intent):
        with env.sync() as s:
            p = s.get(Project, env.pid)
            envelope = service.load(p)
            envelope["destination"] = None
            p.web_config_json = service.dumps(envelope)
            p.web_publish_intent = intent
            s.commit()

    unbind("import_only")
    with env.sync() as s, pytest.raises(PublishingError, match="Save the title"):
        service.prepare(s, s.get(Project, env.pid), PublishRequest(version=service.state(env.pid)["version"]), env.opts)
    unbind("publish_and_import")
    assert metadata_only(env)["status"] == "succeeded"
    with env.sync() as s:  # the first remote exchange bound the mapping to this website
        assert json.loads(s.get(Project, env.pid).web_config_json)["destination"] == service.destination()


# --------------------------------------------------------------------------
# Subtitle receipts and the project-level percentage
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_percentage_counts_only_published_subtitles_per_project(env, monkeypatch):
    seasoned(env, monkeypatch, mappings=[mapping(1, "501", 1), mapping(2, "502", 2)])
    assert run(env, file_ids=[1])["status"] == "succeeded"
    assert receipt(env)["subtitles_published"] == 1 and receipt(env)["percent"] == 33
    assert env.remote.progress == [33]
    assert pe_rows(env)[1].web_subtitle_sha256 and not pe_rows(env)[2].web_subtitle_sha256

    # Repeating a noop upload or a metadata-only run changes nothing and re-sends nothing.
    assert run(env, file_ids=[1])["status"] == "succeeded"
    assert metadata_only(env)["status"] == "succeeded"
    assert env.remote.progress == [33]
    assert run(env, file_ids=[1, 2])["status"] == "succeeded"
    assert env.remote.progress == [33, 66] and receipt(env)["percent"] == 66


@pytest.mark.asyncio
async def test_changed_expected_count_is_synchronized_by_the_next_metadata_only_run(env, monkeypatch):
    seasoned(env, monkeypatch, mappings=[mapping(1, "501", 1)])
    assert run(env, file_ids=[1])["status"] == "succeeded"
    assert env.remote.progress == [33]
    version = service.state(env.pid)["version"]
    service.save(
        env.pid,
        SaveRequest(version=version, config=env.seasoned, expected_episode_count=2,
                    mappings=[mapping(1, "501", 1)]),
    )
    assert metadata_only(env)["status"] == "succeeded"
    assert env.remote.progress == [33, 50]  # the exact new ratio, even if it were lower


@pytest.mark.asyncio
async def test_project_without_receipts_never_resets_remote_progress(env, monkeypatch):
    seasoned(env, monkeypatch)
    env.remote.snapshot = None
    assert metadata_only(env)["status"] == "succeeded"
    env.remote.snapshot["translation_progress"] = 80  # e.g. another season's project
    assert metadata_only(env)["status"] == "succeeded"
    assert env.remote.progress == [] and env.remote.snapshot["translation_progress"] == 80


@pytest.mark.asyncio
async def test_failed_progress_update_is_retried_without_repeating_the_upload(env, monkeypatch):
    seasoned(env, monkeypatch, mappings=[mapping(1, "501", 1)])
    original = WebClient.update_progress

    def broken(*_a):
        raise PublishingError("web_uncertain", "down")

    monkeypatch.setattr(WebClient, "update_progress", broken)
    result = run(env, file_ids=[1])
    assert result["status"] == "failed" and result["result"]["progress_update"]["outcome"] == "failed"
    assert pe_rows(env)[1].web_subtitle_sha256  # the upload receipt is kept
    monkeypatch.setattr(WebClient, "update_progress", original)
    before = len([c for c in env.remote.calls if c[1].endswith("/subtitles")])
    assert metadata_only(env)["status"] == "succeeded"
    assert len([c for c in env.remote.calls if c[1].endswith("/subtitles")]) == before
    assert env.remote.progress == [33]


@pytest.mark.asyncio
async def test_lost_upload_acknowledgement_is_reconciled_by_the_final_read(env, monkeypatch):
    seasoned(env, monkeypatch, mappings=[mapping(1, "501", 1), mapping(2, "502", 2)])
    fired = []

    def lost():
        if not fired:  # first upload commits, then the acknowledgement is lost
            fired.append(1)
            raise PublishingError("web_uncertain", "timeout")

    env.remote.hook = lost
    result = run(env, file_ids=[1, 2])
    rows = {r["file_id"]: r for r in result["result"]["files"]}
    assert rows[1]["outcome"] == "reconciled" and "error" not in rows[1]
    assert rows[2]["outcome"] == "created" and result["status"] == "succeeded"
    assert pe_rows(env)[1].web_subtitle_sha256 and receipt(env)["subtitles_published"] == 2
    assert env.remote.progress == [66]  # the proven upload counts
    assert any(o["stage"] == "Reconcile subtitle" for o in result["result"]["operations"])


@pytest.mark.asyncio
async def test_failed_upload_with_a_different_remote_hash_is_never_counted(env, monkeypatch):
    seasoned(env, monkeypatch, mappings=[mapping(1, "501", 1), mapping(2, "502", 2)])
    env.remote.fail_upload = "episode-2"
    result = run(env, file_ids=[1, 2])
    assert result["status"] == "failed"
    assert pe_rows(env)[1].web_subtitle_sha256 and not pe_rows(env)[2].web_subtitle_sha256
    assert receipt(env)["subtitles_published"] == 1


@pytest.mark.asyncio
async def test_remote_read_backfills_a_receipt_the_website_proves(env, monkeypatch):
    seasoned(env, monkeypatch, mappings=[mapping(1, "501", 1)])
    assert run(env, file_ids=[1])["status"] == "succeeded"
    with env.sync() as s:  # simulate a lost local receipt
        pe = s.query(ProjectEpisode).filter_by(project_id=env.pid, episode_number=1).one()
        pe.web_subtitle_sha256 = pe.web_subtitle_published_at = None
        s.commit()
    assert receipt(env)["subtitles_published"] == 0
    service.remote_state(env.pid)
    assert receipt(env)["subtitles_published"] == 1


@pytest.mark.asyncio
async def test_unrenderable_file_fails_alone_and_the_catalog_still_publishes(env, monkeypatch):
    seasoned(env, monkeypatch, mappings=[mapping(1, "501", 1), mapping(2, "502", 2)])
    original = service.export_ass

    def export(session, file, opts):
        if file.id == 2:
            raise ValueError("broken")
        return original(session, file, opts)

    monkeypatch.setattr(service, "export_ass", export)
    result = run(env, file_ids=[1, 2])
    rows = {r["file_id"]: r for r in result["result"]["files"]}
    assert rows[1]["outcome"] == "created" and rows[2]["code"] == "web_render"
    assert result["status"] == "failed" and len(env.remote.snapshot["seasons"][0]["episodes"]) == 3
    with env.sync() as s:
        assert json.loads(s.get(Project, env.pid).web_config_json)["published_config_hash"]


@pytest.mark.asyncio
async def test_receipt_percentage_is_clamped_and_needs_an_expected_count(env, monkeypatch):
    seasoned(env, monkeypatch, mappings=[mapping(1, "501", 1), mapping(2, "502", 2)])
    run(env, file_ids=[1, 2])
    with env.sync() as s:
        p = s.get(Project, env.pid)
        p.expected_episode_count = 1
        assert remote_mod.receipt_progress(s, p)["percent"] == 100
        p.expected_episode_count = None
        assert remote_mod.receipt_progress(s, p)["percent"] is None


# --------------------------------------------------------------------------
# Mapping edits: full replacement, reassignment, unbound consistency
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_explicit_mappings_replace_the_association_and_keep_episode_receipts(env, monkeypatch):
    seasoned(env, monkeypatch, mappings=[mapping(1, "501", 1), mapping(2, "502", 2)])
    assert run(env, file_ids=[1, 2])["status"] == "succeeded"
    before = receipt(env)
    numbers = {}
    with env.sync() as s:
        numbers = {f.id: f.episode_number for f in s.query(File).filter_by(project_id=env.pid)}
    version = service.state(env.pid)["version"]
    # File 3 takes episode 501; files 1 and 2 are omitted => unassigned.
    service.save(env.pid, SaveRequest(version=version, config=env.seasoned, mappings=[mapping(3, "501", 1)]))
    with env.sync() as s:
        files = {f.id: f for f in s.query(File).filter_by(project_id=env.pid)}
        for fid in (1, 2):
            f = files[fid]
            assert f.web_mapping_json is None and f.web_tmdb_episode_id is None
            assert f.web_episode_uuid is None and f.web_published_sha256 is None
        assert files[3].web_tmdb_episode_id == "501"
        assert {i: f.episode_number for i, f in files.items()} == numbers  # never altered
    rows = pe_rows(env)
    assert rows[1].web_subtitle_sha256 and rows[2].web_subtitle_sha256 and rows[1].web_episode_uuid
    assert receipt(env) == before  # progress is unchanged by reassignment
    with env.sync() as s:
        targets = remote_mod.collect_targets(s, env.pid, env.seasoned)
    assert [t["file_id"] for t in targets] == [3, None, None]  # no duplicate target

    # None keeps the association; an empty list clears it.
    version = service.state(env.pid)["version"]
    service.save(env.pid, SaveRequest(version=version, config=env.seasoned))
    with env.sync() as s:
        assert s.get(File, 3).web_tmdb_episode_id == "501"
    service.save(env.pid, SaveRequest(version=version + 1, config=env.seasoned, mappings=[]))
    with env.sync() as s:
        assert s.get(File, 3).web_mapping_json is None
    assert receipt(env) == before


@pytest.mark.asyncio
async def test_swapping_two_files_between_episodes_is_allowed(env, monkeypatch):
    seasoned(env, monkeypatch, mappings=[mapping(1, "501", 1), mapping(2, "502", 2)])
    version = service.state(env.pid)["version"]
    service.save(
        env.pid,
        SaveRequest(version=version, config=env.seasoned,
                    mappings=[mapping(1, "502", 2), mapping(2, "501", 1)]),
    )
    with env.sync() as s:
        assert (s.get(File, 1).web_tmdb_episode_id, s.get(File, 2).web_tmdb_episode_id) == ("502", "501")


@pytest.mark.asyncio
async def test_unbound_placement_edit_resyncs_the_stored_episodes(env, monkeypatch):
    seasoned(env, monkeypatch)
    with env.sync() as s:
        p = s.get(Project, env.pid)
        envelope = service.load(p)
        envelope["destination"] = None  # as captured at import, never saved/published
        p.web_config_json = service.dumps(envelope)
        s.commit()
    assert sorted(pe_rows(env)) == [1, 2, 3]
    state = seasoned(env, monkeypatch, season="600", count=2)
    assert [e["tmdb_episode_id"] for e in state["episodes"]] == ["601", "602"]
    assert sorted(pe_rows(env)) == [1, 2]  # no mix of the old and the new selection
    assert state["expected_episode_count"] == EXPECTED  # never recomputed from TMDB


@pytest.mark.asyncio
async def test_confirmed_episodes_survive_a_resync_and_freeze_the_placement(env, monkeypatch):
    seasoned(env, monkeypatch)
    assert metadata_only(env)["status"] == "succeeded"
    with env.sync() as s:
        remote_mod.replace_tmdb_episodes(
            s, env.pid, [{"tmdb_episode_id": "900", "episode_number": 1, "name_english": "X"}]
        )
        s.commit()
    rows = pe_rows(env)
    assert rows[1].tmdb_episode_id == "501" and rows[1].web_episode_uuid  # confirmed row untouched
    with pytest.raises(PublishingError, match="cannot be changed"):
        seasoned(env, monkeypatch, season="600", count=2)


@pytest.mark.asyncio
async def test_movie_gets_one_stable_content_episode_with_a_publisher_key(env):
    setup(env, mappings=False)
    assert metadata_only(env)["status"] == "succeeded"
    rows = list(pe_rows(env).values())
    assert len(rows) == 1 and rows[0].tmdb_episode_id is None
    assert rows[0].web_publisher_key.startswith("subineko:") and rows[0].web_episode_uuid
    key = rows[0].web_publisher_key
    assert metadata_only(env)["status"] == "succeeded"
    assert pe_rows(env)[1].web_publisher_key == key and len(env.remote.snapshot["episodes"]) == 1


@pytest.mark.asyncio
async def test_invalid_ass_is_a_per_file_failure_with_its_own_code(env, monkeypatch):
    seasoned(env, monkeypatch, mappings=[mapping(1, "501", 1), mapping(2, "502", 2)])
    original = service.validate_ass

    def validate(filename, content):
        if filename.startswith("[Group] episode 2"):
            raise PublishingError("web_invalid_ass", "ASS must include a nonempty Dialogue.")
        return original(filename, content)

    monkeypatch.setattr(service, "validate_ass", validate)
    result = run(env, file_ids=[1, 2])
    rows = {r["file_id"]: r for r in result["result"]["files"]}
    assert rows[1]["outcome"] == "created"
    assert rows[2]["outcome"] == "failed" and rows[2]["code"] == "web_invalid_ass"
    assert len(env.remote.snapshot["seasons"][0]["episodes"]) == 3  # catalog still synchronized
    assert not pe_rows(env)[2].web_subtitle_sha256
