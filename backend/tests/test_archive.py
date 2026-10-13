"""Project archiving: eligibility, the pure-state-change guarantee, backend
enforcement (router guard + job manager + orchestrator), lightweight lists,
English-title metadata, publish source preflight and source-free ASS download.

Reuses the file-backed ``env`` fixture of ``test_publish`` (async + sync engines
on one real SQLite DB, ``tmp_path`` import/output roots)."""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import select, update

from app.api.routes import import_ as import_routes
from app.api.routes import projects as projects_routes
from app.api.routes import qc as qc_routes
from app.db.models import File, JobRecord, Project, SubtitleEvent
from app.jobs.manager import JobManager
from app.metadata.anidb import AniDBProvider
from app.metadata.anilist import AniListProvider
from app.metadata.base import Character, Episode, MediaType, SeriesDetails
from app.metadata.sync import set_english_title
from tests.test_publish import click_publish, env, seed_project, mux, run_job  # noqa: F401

BACKEND = Path(__file__).resolve().parent.parent


def _now() -> str:
    return datetime.utcnow().isoformat()


@pytest.fixture
def aenv(env, monkeypatch):  # noqa: F811
    monkeypatch.setattr("app.api.routes.qc.AsyncSessionLocal", env.asess)
    monkeypatch.setattr("app.api.routes.import_.AsyncSessionLocal", env.asess)
    env.enqueue.reset_mock()
    env.cancel_all = AsyncMock()
    projects_routes.job_manager.cancel_project_jobs = env.cancel_all
    return env


@pytest_asyncio.fixture
async def client(aenv):
    app = FastAPI()
    app.include_router(projects_routes.router, prefix="/api")
    app.include_router(qc_routes.router, prefix="/api")
    app.include_router(import_routes.router, prefix="/api")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        yield c


def row(env, pid) -> Project:
    with env.sync() as s:
        return s.get(Project, pid)


PROTECTED_COLUMNS = (
    "status", "is_paused", "output_revision", "published_revision", "publish_state",
    "published_at", "publish_attempt", "publish_target_revision", "updated_at",
    "context_approved_at", "name", "name_english", "source_directory",
)


def snapshot(env, pid) -> dict:
    p = row(env, pid)
    with env.sync() as s:
        files = list(s.execute(select(File.id, File.status, File.updated_at).where(File.project_id == pid)))
    return {**{c: getattr(p, c) for c in PROTECTED_COLUMNS}, "files": [tuple(f) for f in files]}


def add_job(env, pid, status="queued", job_type="translate_chunk", key=None):
    with env.sync() as s:
        s.add(JobRecord(
            job_type=job_type, project_id=pid, status=status, dedupe_key=key or f"{job_type}:{pid}:{status}",
            priority=100, payload_json="{}", attempt_count=0, max_attempts=1,
            scheduled_at=_now(), created_at=_now(), updated_at=_now(),
        ))
        s.commit()


async def archive(client, pid):
    return await client.post(f"/api/projects/{pid}/archive")


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------

def test_existing_projects_stay_active_after_migration(tmp_path):
    cfg = tmp_path / "config"
    cfg.mkdir()
    environ = {
        **os.environ,
        "CONFIG_ROOT": str(cfg), "IMPORT_ROOT": str(tmp_path / "i"), "OUTPUT_ROOT": str(tmp_path / "o"),
    }

    def alembic(*args):
        subprocess.run([sys.executable, "-m", "alembic", *args], cwd=BACKEND, env=environ,
                       check=True, capture_output=True)

    alembic("upgrade", "b5c6d7e8f9a0")
    db = next(cfg.glob("*.db"))
    con = sqlite3.connect(db)
    con.execute("INSERT INTO projects (name, source_directory, anime_provider, anime_external_id) "
                "VALUES ('Old', 'old', 'anilist', '1')")
    con.commit()
    con.close()

    alembic("upgrade", "head")
    con = sqlite3.connect(db)
    assert con.execute("SELECT archived_at, name_english, name FROM projects").fetchall() == [(None, None, "Old")]
    con.close()

    alembic("downgrade", "b5c6d7e8f9a0")   # reversible, data kept
    con = sqlite3.connect(db)
    assert con.execute("SELECT name FROM projects").fetchall() == [("Old",)]
    con.close()


# ---------------------------------------------------------------------------
# Archive / unarchive lifecycle
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_archive_is_a_pure_state_change_and_unarchive_restores_it(client, aenv):
    pid = seed_project(aenv, n_files=2)
    with aenv.sync() as s:   # a published, paused, revision-bumped project
        s.execute(update(Project).where(Project.id == pid).values(
            publish_state="published", published_revision=0, published_at=_now(),
            output_revision=0, is_paused=True))
        s.commit()
    before = snapshot(aenv, pid)

    r = await archive(client, pid)
    assert r.status_code == 200
    body = r.json()
    assert body["archived_at"] is not None and body["archive"]["eligible"] is False

    after = snapshot(aenv, pid)
    assert after == before                      # nothing but archived_at changed
    assert row(aenv, pid).archived_at == body["archived_at"]

    r = await client.post(f"/api/projects/{pid}/unarchive")
    assert r.status_code == 200 and r.json()["archived_at"] is None
    assert snapshot(aenv, pid) == before        # state, revisions, pause all preserved
    assert row(aenv, pid).archived_at is None
    aenv.enqueue.assert_not_awaited()           # unarchive starts nothing


@pytest.mark.asyncio
async def test_repeat_archive_and_unarchive_are_safe(client, aenv):
    pid = seed_project(aenv)
    first = (await archive(client, pid)).json()["archived_at"]
    again = await archive(client, pid)
    assert again.status_code == 200 and again.json()["archived_at"] == first   # not re-stamped

    assert (await client.post(f"/api/projects/{pid}/unarchive")).status_code == 200
    assert (await client.post(f"/api/projects/{pid}/unarchive")).status_code == 200
    assert row(aenv, pid).archived_at is None
    assert (await archive(client, pid + 99)).status_code == 404
    assert (await client.post(f"/api/projects/{pid + 99}/unarchive")).status_code == 404


@pytest.mark.asyncio
async def test_empty_project_cannot_be_archived(client, aenv):
    pid = seed_project(aenv, n_files=0)
    before = snapshot(aenv, pid)
    r = await archive(client, pid)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "archive_not_allowed"
    assert "no files" in r.json()["detail"]["message"]
    assert snapshot(aenv, pid) == before and row(aenv, pid).archived_at is None


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["review_required", "processing", "ready", "waiting", "completed"])
async def test_any_non_accepted_file_blocks_archive(client, aenv, status):
    pid = seed_project(aenv, n_files=3)
    with aenv.sync() as s:
        s.execute(update(File).where(File.project_id == pid, File.relative_path == "ep3.mkv").values(status=status))
        s.commit()
    before = snapshot(aenv, pid)
    r = await archive(client, pid)
    assert r.status_code == 409 and "1 of 3 file(s) are not accepted" in r.json()["detail"]["message"]
    assert snapshot(aenv, pid) == before and row(aenv, pid).archived_at is None
    info = (await client.get(f"/api/projects/{pid}")).json()["archive"]
    assert info["eligible"] is False and "not accepted" in info["reason"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["queued", "running"])
async def test_queued_or_running_jobs_block_archive(client, aenv, status):
    pid = seed_project(aenv)
    add_job(aenv, pid, status)
    r = await archive(client, pid)
    assert r.status_code == 409 and "queued or running" in r.json()["detail"]["message"]
    assert row(aenv, pid).archived_at is None

    add_job(aenv, pid, "completed", key="done")   # finished jobs don't block
    with aenv.sync() as s:
        s.execute(update(JobRecord).where(JobRecord.status == status).values(status="cancelled"))
        s.commit()
    assert (await archive(client, pid)).status_code == 200


@pytest.mark.asyncio
async def test_publish_in_progress_blocks_archive(client, aenv):
    pid = seed_project(aenv)
    with aenv.sync() as s:
        s.execute(update(Project).where(Project.id == pid).values(publish_state="publishing"))
        s.commit()
    r = await archive(client, pid)
    assert r.status_code == 409 and "publish is in progress" in r.json()["detail"]["message"]
    assert row(aenv, pid).archived_at is None


@pytest.mark.asyncio
async def test_archive_update_is_conditional_on_current_state(aenv, monkeypatch):
    """A job that appears after the eligibility check but before the write must
    stop the archive (the UPDATE re-asserts the conditions in SQL)."""
    from app.db import archive_state

    pid = seed_project(aenv)
    real = archive_state.archive_blockers

    async def racing(session, ids):
        result = await real(session, ids)
        add_job(aenv, pid, "queued")   # lands right after the check passed
        return result

    monkeypatch.setattr(archive_state, "archive_blockers", racing)
    with pytest.raises(archive_state.ArchiveNotAllowed):
        await archive_state.archive_project(pid)
    assert row(aenv, pid).archived_at is None


# ---------------------------------------------------------------------------
# Backend enforcement
# ---------------------------------------------------------------------------

MUTATIONS = [
    ("post", "/api/projects/{pid}/files/{fid}/translate", None),
    ("post", "/api/projects/{pid}/files/{fid}/retranslate", None),
    ("post", "/api/projects/{pid}/files/{fid}/accept-review", {}),
    ("put", "/api/projects/{pid}/files/{fid}/subtitle-events/{eid}", {"translated_text": "x"}),
    ("patch", "/api/projects/{pid}/files/{fid}/qc/events/{eid}", {"translated_text": "x"}),
    ("post", "/api/projects/{pid}/publish", None),
    ("post", "/api/projects/{pid}/pause", None),
    ("post", "/api/projects/{pid}/resume", None),
    ("post", "/api/projects/{pid}/refresh-metadata", None),
    ("post", "/api/projects/{pid}/approve-context", None),
    ("post", "/api/projects/{pid}/watched-words", {"word": "a", "word_type": "original"}),
    ("post", "/api/projects/{pid}/glossary", {"source_term": "a", "target_term": "b", "category": "name"}),
    ("put", "/api/projects/{pid}/style-bible", {"tone_summary": "x"}),
    ("post", "/api/projects/{pid}/characters", {"name": "A"}),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("method,url,body", MUTATIONS, ids=[f"{m} {u.split('{pid}')[1]}" for m, u, _ in MUTATIONS])
async def test_archived_project_rejects_mutations(client, aenv, method, url, body):
    pid = seed_project(aenv)
    await archive(client, pid)
    with aenv.sync() as s:
        fid = s.scalar(select(File.id).where(File.project_id == pid))
        eid = s.scalar(select(SubtitleEvent.id).where(SubtitleEvent.file_id == fid))
    before = snapshot(aenv, pid)

    r = await getattr(client, method)(url.format(pid=pid, fid=fid, eid=eid), **({"json": body} if body is not None else {}))
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "project_archived"
    assert snapshot(aenv, pid) == before
    aenv.enqueue.assert_not_awaited()


@pytest.mark.asyncio
async def test_archived_project_stays_readable_and_deletable(client, aenv):
    pid = seed_project(aenv)
    await archive(client, pid)
    assert (await client.get(f"/api/projects/{pid}")).json()["archived_at"] is not None
    assert (await client.get(f"/api/projects/{pid}/files")).status_code == 200
    assert (await client.delete(f"/api/projects/{pid}")).status_code == 204   # explicit delete still works
    assert row(aenv, pid) is None
    aenv.cancel_all.assert_awaited_once_with(pid)
    assert (aenv.import_root / "series" / "ep1.mkv").exists()                 # media untouched


@pytest.mark.asyncio
async def test_other_projects_are_isolated_from_an_archive(client, aenv):
    a = seed_project(aenv, source_directory="a")
    b = seed_project(aenv, source_directory="b")
    await archive(client, a)
    r = await client.post(f"/api/projects/{b}/watched-words", json={"word": "x", "word_type": "original"})
    assert r.status_code == 201
    assert (await client.post(f"/api/projects/{a}/watched-words",
                              json={"word": "x", "word_type": "original"})).status_code == 409
    assert row(aenv, b).archived_at is None


@pytest.mark.asyncio
async def test_job_manager_refuses_archived_projects(aenv):
    pid = seed_project(aenv, source_directory="m1")
    other = seed_project(aenv, source_directory="m2")
    manager = JobManager()
    assert await manager.enqueue(job_type="analyze_script", project_id=pid) is not None   # active: works
    with aenv.sync() as s:
        s.execute(update(JobRecord).values(status="completed"))
        s.execute(update(Project).where(Project.id == pid).values(archived_at=_now()))
        s.commit()

    assert await manager.enqueue(job_type="analyze_script", project_id=pid, dedupe_key="k2") is None
    with aenv.sync() as s:
        assert s.scalars(select(JobRecord).where(JobRecord.dedupe_key == "k2")).first() is None
    assert await manager.enqueue(job_type="analyze_script", project_id=other) is not None


@pytest.mark.asyncio
async def test_orchestrator_and_sweep_skip_archived_projects(aenv):
    from app.orchestrator.orchestrator import sweep_all_projects
    from app.orchestrator.project_orchestrator import orchestrate_project

    pid = seed_project(aenv)
    with aenv.sync() as s:
        s.execute(update(Project).where(Project.id == pid).values(status="processing", archived_at=_now()))
        s.execute(update(File).where(File.project_id == pid).values(status="ready", translation_requested_at=None))
        s.commit()
    enqueue = AsyncMock()
    await orchestrate_project(pid, enqueue)
    await sweep_all_projects(enqueue)
    enqueue.assert_not_awaited()
    assert row(aenv, pid).status == "processing"


# ---------------------------------------------------------------------------
# Lists
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_active_and_archived_lists(client, aenv):
    a = seed_project(aenv, n_files=3, source_directory="alpha")
    b = seed_project(aenv, n_files=1, source_directory="beta")
    c = seed_project(aenv, n_files=2, source_directory="gamma")
    d = seed_project(aenv, n_files=0, source_directory="delta")
    with aenv.sync() as s:
        s.execute(update(Project).where(Project.id == a).values(name_english="Alpha EN"))
        s.commit()
    for pid, stamp in ((a, "2026-01-01T10:00:00"), (b, "2026-03-01T10:00:00"), (c, "2026-03-01T10:00:00")):
        await archive(client, pid)
        with aenv.sync() as s:   # deterministic stamps incl. a tie (b, c)
            s.execute(update(Project).where(Project.id == pid).values(archived_at=stamp))
            s.commit()

    active = (await client.get("/api/projects")).json()
    assert [p["id"] for p in active] == [d]

    archived = (await client.get("/api/projects/archived")).json()
    assert [p["id"] for p in archived] == [c, b, a]                      # date desc, id desc on ties
    assert {p["id"]: p["file_count"] for p in archived} == {a: 3, b: 1, c: 2}
    first = archived[-1]
    assert set(first) == {"id", "name", "name_english", "source_directory", "archived_at", "file_count"}
    assert first["name_english"] == "Alpha EN" and first["source_directory"] == "alpha"
    assert (await client.get("/api/projects/archived")).status_code == 200  # not parsed as a project id


# ---------------------------------------------------------------------------
# English title metadata
# ---------------------------------------------------------------------------

_ANIDB_XML = """
<anime id="9">
  <type>TV Series</type><episodecount>1</episodecount>
  <titles>
    <title xml:lang="x-jat" type="main">Sousou no Frieren</title>
    <title xml:lang="en" type="syn">Fan Alias</title>
    <title xml:lang="en" type="official">Frieren: Beyond Journey's End</title>
    <title xml:lang="ja" type="official">葬送のフリーレン</title>
  </titles>
</anime>
"""


def _anidb_details(xml: str, titles_dump: list[dict] | None = None, cached: bool = False):
    import asyncio
    import xml.etree.ElementTree as ET

    p = AniDBProvider()
    parsed = p._parse_anime_xml(ET.fromstring(xml))
    if cached:   # an entry cached before title_english existed
        parsed["details"].pop("title_english")

    async def fetch(_id):
        return parsed

    async def dump():
        return titles_dump or []

    p._fetch_anime, p._load_titles = fetch, dump
    return asyncio.run(p.get_details("9"))


def test_anidb_english_title_is_the_official_english_one():
    d = _anidb_details(_ANIDB_XML)
    assert d.title == "Sousou no Frieren"
    assert d.title_english == "Frieren: Beyond Journey's End"        # not the "syn" alias


def test_anidb_without_official_english_title_is_none():
    xml = _ANIDB_XML.replace('type="official">Frieren', 'type="syn">Frieren')
    assert _anidb_details(xml).title_english is None


def test_anidb_cached_entry_without_english_falls_back_to_titles_dump():
    d = _anidb_details(_ANIDB_XML, titles_dump=[{"aid": "9", "english": "From Dump"}], cached=True)
    assert d.title_english == "From Dump"
    assert _anidb_details(_ANIDB_XML, titles_dump=[], cached=True).title_english is None


def _anilist_details(title: dict):
    import asyncio

    p = AniListProvider()
    data = p._serialize_media({"id": 5, "title": title, "format": "TV"})

    async def fetch(_id):
        return data

    p._fetch_media = fetch
    return asyncio.run(p.get_details("5"))


def test_anilist_english_title_and_absence():
    assert _anilist_details({"romaji": "Shingeki", "english": "Attack on Titan", "native": "x"}).title_english == "Attack on Titan"
    assert _anilist_details({"romaji": "Shingeki", "english": None, "native": "x"}).title_english is None
    assert _anilist_details({"romaji": "Shingeki", "english": "", "native": "x"}).title_english is None


@pytest.mark.asyncio
async def test_set_english_title_never_clears_or_blanks(aenv):
    pid = seed_project(aenv)
    async with aenv.asess() as s:
        assert await set_english_title(s, pid, "  First  ") is True
        await s.commit()
    assert row(aenv, pid).name_english == "First"
    for empty in (None, "", "   "):
        async with aenv.asess() as s:
            assert await set_english_title(s, pid, empty) is False
            await s.commit()
        assert row(aenv, pid).name_english == "First"
    async with aenv.asess() as s:
        assert await set_english_title(s, pid, "First") is False          # unchanged
        assert await set_english_title(s, pid, "Updated") is True
        await s.commit()
    assert row(aenv, pid).name_english == "Updated"
    assert row(aenv, pid).name == "P"                                      # romanized title untouched


class FakeProvider:
    def __init__(self, english):
        self.english = english

    async def get_details(self, pid):
        return SeriesDetails(provider_id=pid, title="Romaji", media_type=MediaType.TV, title_english=self.english)

    async def get_characters(self, pid):
        return [Character(name="Aria", provider_id="1")]

    async def get_episodes(self, pid):
        return [Episode(number=1, title="One")]


@pytest.mark.asyncio
async def test_refresh_metadata_fills_but_never_blanks_the_english_title(client, aenv, monkeypatch):
    pid = seed_project(aenv)
    provider = FakeProvider("English Title")
    monkeypatch.setattr("app.metadata.registry.get_provider", lambda name: provider)

    r = await client.post(f"/api/projects/{pid}/refresh-metadata")
    assert r.status_code == 200 and r.json()["title_english_updated"] is True
    assert row(aenv, pid).name_english == "English Title"

    provider.english = None   # provider no longer supplies one
    r = await client.post(f"/api/projects/{pid}/refresh-metadata")
    assert r.status_code == 200 and r.json()["title_english_updated"] is False
    assert row(aenv, pid).name_english == "English Title"
    assert row(aenv, pid).name == "P"


@pytest.mark.asyncio
async def test_refresh_without_any_english_title_leaves_null(client, aenv, monkeypatch):
    pid = seed_project(aenv)
    monkeypatch.setattr("app.metadata.registry.get_provider", lambda name: FakeProvider(None))
    assert (await client.post(f"/api/projects/{pid}/refresh-metadata")).status_code == 200
    assert row(aenv, pid).name_english is None


@pytest.mark.asyncio
@pytest.mark.parametrize("sent,provider_english,expected", [
    ("From Search", None, "From Search"),
    (None, "From Details", "From Details"),
    (None, None, None),
])
async def test_import_persists_provider_english_title(client, aenv, monkeypatch, sent, provider_english, expected):
    (aenv.import_root / "New Series").mkdir()
    monkeypatch.setattr("app.api.routes.import_.get_provider", lambda name: FakeProvider(provider_english))
    monkeypatch.setattr("app.api.routes.import_.settings.import_root", aenv.import_root)
    monkeypatch.setattr("app.api.routes.import_.orchestrate_project", AsyncMock())

    r = await client.post("/api/import", json={
        "directory_name": "New Series", "provider": "anilist", "provider_id": "7",
        "anime_title": "Romaji Title", "anime_title_english": sent,
    })
    assert r.status_code == 201
    created = row(aenv, r.json()["id"])
    assert created.name == "Romaji Title" and created.name_english == expected and created.archived_at is None


# ---------------------------------------------------------------------------
# Publish source availability
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_preflight_reports_each_publish_prerequisite(client, aenv):
    pid = seed_project(aenv, n_files=2)
    url = f"/api/projects/{pid}/publish-preflight"

    ok = (await client.get(url)).json()
    assert ok == {"available": True, "reason": None, "missing_files": []}

    (aenv.import_root / "series" / "ep2.mkv").unlink()
    one = (await client.get(url)).json()
    assert one["available"] is False and one["missing_files"] == ["ep2.mkv"]
    assert "missing" in one["reason"]

    for f in (aenv.import_root / "series").iterdir():
        f.unlink()
    (aenv.import_root / "series").rmdir()
    gone = (await client.get(url)).json()
    assert gone["available"] is False and "source folder" in gone["reason"] and gone["missing_files"] == []

    with aenv.sync() as s:
        s.execute(update(File).where(File.relative_path == "ep1.mkv").values(status="review_required"))
        s.commit()
    assert "accepted" in (await client.get(url)).json()["reason"]
    assert (await client.get(f"/api/projects/{pid + 50}/publish-preflight")).status_code == 404


@pytest.mark.asyncio
async def test_publish_request_rejected_without_source_media(client, aenv, mux):
    pid = seed_project(aenv, n_files=2)
    (aenv.import_root / "series" / "ep2.mkv").unlink()
    before = snapshot(aenv, pid)

    r = await client.post(f"/api/projects/{pid}/publish")
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["code"] == "publish_sources_missing" and detail["missing_files"] == ["ep2.mkv"]
    assert snapshot(aenv, pid) == before            # not claimed, nothing enqueued
    aenv.enqueue.assert_not_awaited()


@pytest.mark.asyncio
async def test_publish_request_rejected_without_source_directory(client, aenv):
    pid = seed_project(aenv)
    for f in (aenv.import_root / "series").iterdir():
        f.unlink()
    (aenv.import_root / "series").rmdir()
    r = await client.post(f"/api/projects/{pid}/publish")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "publish_sources_missing"
    assert row(aenv, pid).publish_state is None


@pytest.mark.asyncio
async def test_publish_with_media_present_still_works(client, aenv, mux):
    pid = seed_project(aenv)
    r = await client.post(f"/api/projects/{pid}/publish")
    assert r.status_code == 202 and r.json()["output"]["state"] == "publishing"
    assert run_job(aenv)["status"] == "succeeded"
    assert row(aenv, pid).publish_state == "published"


@pytest.mark.asyncio
async def test_stale_preflight_cannot_publish_vanished_media(client, aenv, mux):
    """Preflight said OK; the media vanishes; the request re-validates and the
    job (for a request that raced past it) fails without marking success."""
    pid = seed_project(aenv)
    assert (await client.get(f"/api/projects/{pid}/publish-preflight")).json()["available"] is True

    # (a) vanishes before the click → the request itself refuses
    (aenv.import_root / "series" / "ep1.mkv").rename(aenv.import_root / "series" / "moved.mkv")
    assert (await client.post(f"/api/projects/{pid}/publish")).status_code == 409
    (aenv.import_root / "series" / "moved.mkv").rename(aenv.import_root / "series" / "ep1.mkv")

    # (b) vanishes between the click and the job → the handler re-checks
    assert (await client.post(f"/api/projects/{pid}/publish")).status_code == 202
    (aenv.import_root / "series" / "ep2.mkv").unlink()
    result = run_job(aenv)
    assert result["status"] == "failed" and result["error_code"] == "SOURCE_MISSING"
    assert row(aenv, pid).publish_state == "failed" and row(aenv, pid).published_revision is None
    assert not (aenv.output_root / "series").exists() or not list((aenv.output_root / "series").glob("*"))


@pytest.mark.asyncio
async def test_archived_project_cannot_publish_and_preflight_says_why(client, aenv):
    pid = seed_project(aenv)
    await archive(client, pid)
    r = await client.post(f"/api/projects/{pid}/publish")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "project_archived"
    pre = (await client.get(f"/api/projects/{pid}/publish-preflight")).json()
    assert pre["available"] is False and "archived" in pre["reason"]
    assert row(aenv, pid).publish_state is None


# ---------------------------------------------------------------------------
# Translated ASS without source media
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_translated_ass_downloads_after_unarchive_without_the_mkv(client, aenv):
    pid = seed_project(aenv)
    with aenv.sync() as s:
        fid = s.scalar(select(File.id).where(File.project_id == pid))
        eid = s.scalar(select(SubtitleEvent.id).where(SubtitleEvent.file_id == fid))

    await archive(client, pid)
    for f in (aenv.import_root / "series").iterdir():     # the user deletes the source media
        f.unlink()
    (aenv.import_root / "series").rmdir()
    assert (await client.post(f"/api/projects/{pid}/unarchive")).status_code == 200

    url = f"/api/projects/{pid}/files/{fid}/subtitles/translated"
    r = await client.get(url)
    assert r.status_code == 200
    assert "Ahoj 0" in r.text
    assert 'filename*=UTF-8\'\'ep1.ass' in r.headers["content-disposition"]

    edit = await client.put(f"/api/projects/{pid}/files/{fid}/subtitle-events/{eid}",
                            json={"translated_text": "Upraveno"})
    assert edit.status_code == 200
    r = await client.get(url)
    assert "Upraveno" in r.text and "Ahoj 0" not in r.text

    assert row(aenv, pid).archived_at is None
