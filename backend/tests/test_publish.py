"""Explicit project publishing: output revision, derived output state, the
Publish entry point/job, safe replacement, failure handling and invalidation.

Uses a *file-backed* SQLite DB (tmp_path) opened by both an async engine (API /
orchestrator code) and a sync engine (job handler, listener tests), so the full
claim -> job -> handler -> state round trip runs against one real database.
mkvmerge is replaced by a fake that writes an output file embedding the muxed
subtitle, so output contents are assertable.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import create_engine, func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.db import options as options_store
from app.db.models import (
    File,
    JobRecord,
    Project,
    QaItem,
    Subtitle,
    SubtitleEvent,
    SubtitleStyle,
    file_subtitle_styles,
)
from app.db.options import AppOptions
from app.db.output_state import OutputState, derive_output_state, touch_output_sync
from app.jobs.context import JobContext
from app.jobs.handlers import publish_project as publish_handler
from app.jobs.manager import JobManager
from app.orchestrator import publish as publish_orch
from app.subs.ass_rendering import build_ass, save_ass


def _now() -> str:
    return datetime.utcnow().isoformat()


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def env(tmp_path, monkeypatch):
    db_path = tmp_path / "t.db"
    sync_engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(sync_engine)
    sync_factory = sessionmaker(bind=sync_engine, expire_on_commit=False)
    async_engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    async_factory = async_sessionmaker(bind=async_engine, expire_on_commit=False)

    for target in (
        "app.core.database.AsyncSessionLocal",
        "app.api.routes.projects.AsyncSessionLocal",
        "app.orchestrator.publish.AsyncSessionLocal",
        "app.orchestrator.file_orchestrator.AsyncSessionLocal",
        "app.orchestrator.project_orchestrator.AsyncSessionLocal",
        "app.orchestrator.chunk_orchestrator.AsyncSessionLocal",
        "app.orchestrator.context_status.AsyncSessionLocal",
        "app.orchestrator.orchestrator.AsyncSessionLocal",
        "app.jobs.manager.AsyncSessionLocal",
        "app.db.options.AsyncSessionLocal",
    ):
        monkeypatch.setattr(target, async_factory)
    for target in ("app.core.database.SyncSessionLocal", "app.jobs.handlers.publish_project.SyncSessionLocal",
                   "app.db.options.SyncSessionLocal"):
        monkeypatch.setattr(target, sync_factory)
    options_store.invalidate()

    import_root, output_root = tmp_path / "import", tmp_path / "output"
    import_root.mkdir()
    output_root.mkdir()

    enqueue = AsyncMock()
    fake_manager = SimpleNamespace(enqueue=enqueue, cancel_queued_project_jobs=AsyncMock())
    monkeypatch.setattr("app.api.routes.projects.job_manager", fake_manager)

    yield SimpleNamespace(
        sync=sync_factory, asess=async_factory, import_root=import_root, output_root=output_root,
        enqueue=enqueue, tmp=tmp_path,
    )

    options_store.invalidate()
    await async_engine.dispose()
    sync_engine.dispose()


class FakeMkvmerge:
    """Stands in for subprocess.run(['mkvmerge', ...]); embeds the muxed ASS."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.fail_on: str | None = None      # substring of the source path → exit 2
        self.returncode = 0
        self.stdout = b""
        self.hook = None                      # called with the source path before muxing

    def __call__(self, cmd, capture_output=True, **kw):
        self.calls.append(cmd)
        src = Path(cmd[3])
        if self.hook:
            self.hook(src)
        if self.fail_on and self.fail_on in str(src):
            return SimpleNamespace(returncode=2, stdout=b"Error: simulated mux failure\n", stderr=b"")
        out = Path(cmd[cmd.index("-o") + 1])
        out.write_bytes(src.read_bytes() + b"||" + Path(cmd[-1]).read_bytes())
        return SimpleNamespace(returncode=self.returncode, stdout=self.stdout, stderr=b"")


@pytest.fixture
def mux(monkeypatch):
    fake = FakeMkvmerge()
    monkeypatch.setattr(publish_handler.subprocess, "run", fake)
    return fake


def seed_project(env, *, n_files=2, status="accepted", source_directory="series") -> int:
    with env.sync() as s:
        p = Project(
            name="P", source_directory=source_directory, anime_provider="t", anime_external_id="1",
            status="completed", context_approved_at=_now(), created_at=_now(), updated_at=_now(),
        )
        s.add(p)
        s.flush()
        for i in range(n_files):
            rel = f"ep{i + 1}.mkv"
            f = File(project_id=p.id, filename=rel, relative_path=rel, status=status,
                     translation_requested_at=_now(), created_at=_now(), updated_at=_now())
            s.add(f)
            s.flush()
            s.add(Subtitle(file_id=f.id, script_type="v4.00+", play_res_x=1920, play_res_y=1080,
                           created_at=_now(), updated_at=_now()))
            style = SubtitleStyle(project_id=p.id, source_style_hash=f"h{i}", style_name="Default",
                                  font_name="Arial", font_size=20.0, created_at=_now(), updated_at=_now())
            s.add(style)
            s.flush()
            s.execute(file_subtitle_styles.insert().values(file_id=f.id, subtitle_style_id=style.id))
            s.add(SubtitleEvent(
                file_id=f.id, line_index=0, event_type="dialogue", content_type="dialogue", layer=0,
                start_ms=0, end_ms=1500, original_start_ms=0, original_end_ms=1500, style="Default", source_text=f"Hello {i}",
                translated_text=f"Ahoj {i}", original_ai_translated_text=f"Ahoj {i}",
                translation_status="translated", created_at=_now(), updated_at=_now()))
        s.commit()
        pid = p.id
    src_dir = env.import_root / source_directory
    src_dir.mkdir(exist_ok=True)
    for i in range(n_files):
        (src_dir / f"ep{i + 1}.mkv").write_bytes(f"MKV{i}".encode())
    return pid


def ctx_for(env, **opts) -> JobContext:
    return JobContext(
        import_root=env.import_root, output_root=env.output_root,
        options=AppOptions(target_lang_name="Czech", target_lang_code="cze", **opts),
    )


def project_row(env, pid) -> Project:
    with env.sync() as s:
        return s.get(Project, pid)


def file_statuses(env, pid) -> list[str]:
    with env.sync() as s:
        return list(s.scalars(select(File.status).where(File.project_id == pid).order_by(File.id)).all())


def event_id(env, pid, index=0) -> int:
    with env.sync() as s:
        return s.scalars(
            select(SubtitleEvent.id).join(File, File.id == SubtitleEvent.file_id)
            .where(File.project_id == pid).order_by(File.id)
        ).all()[index]


async def output_of(pid: int):
    from app.api.routes.projects import get_project
    return (await get_project(pid)).output


async def click_publish(env, pid: int):
    """What POST /publish does; returns the PublishStart-like response."""
    from app.api.routes.projects import publish_project_output
    before = env.enqueue.await_count
    out = await publish_project_output(pid)
    started = env.enqueue.await_count > before
    return out, started


def run_job(env, mux_fake=None, **opts):
    """Run the handler for the most recently enqueued publish job."""
    payload = env.enqueue.await_args.kwargs["payload"]
    return publish_handler.publish_project(payload, ctx_for(env, **opts), lambda *_: None)


async def publish_once(env, pid, **opts):
    await click_publish(env, pid)
    return run_job(env, **opts)


# ---------------------------------------------------------------------------
# derive_output_state (pure)
# ---------------------------------------------------------------------------

def _state(**kw):
    base = dict(accepted_files=2, total_files=2, publish_state=None, output_revision=3,
                published_revision=None, publish_target_revision=None)
    base.update(kw)
    return derive_output_state(**base)


def test_derive_state_table():
    assert _state(accepted_files=1) is OutputState.NOT_READY                       # C
    assert _state(total_files=0, accepted_files=0) is OutputState.NOT_READY
    assert _state(accepted_files=1, published_revision=3, publish_state="published") is OutputState.NOT_READY
    assert _state() is OutputState.READY                                           # B: never published
    assert _state(publish_state="publishing") is OutputState.PUBLISHING
    assert _state(publish_state="published", published_revision=3) is OutputState.PUBLISHED
    assert _state(publish_state="published", published_revision=2) is OutputState.READY
    assert _state(publish_state="failed", publish_target_revision=3) is OutputState.FAILED
    # obsolete failure (data changed since) must not mask the new READY state
    assert _state(publish_state="failed", publish_target_revision=2) is OutputState.READY
    # failed re-publish of the *current* revision is FAILED even though an
    # earlier publish of the same revision succeeded
    assert _state(publish_state="failed", publish_target_revision=3, published_revision=3) is OutputState.FAILED


# ---------------------------------------------------------------------------
# READY / NOT_READY / no automatic output  (A, B, C, T)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_not_ready_until_all_files_accepted(env):
    pid = seed_project(env, n_files=3)
    with env.sync() as s:
        s.execute(update(File).where(File.relative_path == "ep3.mkv").values(status="review_required"))
        s.commit()
    out = await output_of(pid)
    assert out.state == "not_ready"
    assert (out.accepted_files, out.total_files) == (2, 3)


@pytest.mark.asyncio
async def test_all_accepted_never_published_is_ready(env):
    pid = seed_project(env)
    out = await output_of(pid)
    assert out.state == "ready"
    assert out.published_revision is None and out.published_at is None


@pytest.mark.asyncio
async def test_accepting_the_final_file_starts_no_output(env, monkeypatch):
    from app.orchestrator import file_orchestrator
    monkeypatch.setattr(file_orchestrator, "_populate_translation_memory_sync", lambda *_: 0)
    pid = seed_project(env, n_files=2)
    with env.sync() as s:
        s.execute(update(File).where(File.relative_path == "ep2.mkv").values(status="review_required"))
        s.commit()
        last = s.scalar(select(File.id).where(File.relative_path == "ep2.mkv"))

    enqueue = AsyncMock()
    await file_orchestrator.finalize_accepted_file(last, pid, enqueue)

    job_types = {c.kwargs["job_type"] for c in enqueue.await_args_list}
    assert job_types <= {"update_style_bible"}
    assert file_statuses(env, pid) == ["accepted", "accepted"]
    assert (await output_of(pid)).state == "ready"


@pytest.mark.asyncio
async def test_no_orchestrator_path_enqueues_output(env):
    """T: orchestrators and the sweep never start render/mux/publish — for READY,
    PUBLISHED, and even a stray legacy MUXING file."""
    from app.orchestrator.orchestrator import sweep_all_projects
    from app.orchestrator.file_orchestrator import orchestrate_file
    from app.orchestrator.project_orchestrator import orchestrate_project
    from app.jobs.registry import list_job_types
    import app.main  # noqa: F401  (registers handlers)

    assert "render_output_ass" not in list_job_types()
    assert "mux_output_mkv" not in list_job_types()
    assert "publish_project" in list_job_types()

    pid = seed_project(env, n_files=2)
    with env.sync() as s:
        s.execute(update(Project).where(Project.id == pid).values(status="processing"))
        s.execute(update(File).where(File.relative_path == "ep2.mkv").values(status="muxing"))
        s.commit()
        file_ids = list(s.scalars(select(File.id).where(File.project_id == pid)).all())

    enqueue = AsyncMock()
    for _ in range(2):  # two sweeps: the project walks to COMPLETED, still no output
        await sweep_all_projects(enqueue)
        await orchestrate_project(pid, enqueue)
        for fid in file_ids:
            await orchestrate_file(fid, enqueue)

    forbidden = {"render_output_ass", "mux_output_mkv", "publish_project"}
    assert not forbidden & {c.kwargs["job_type"] for c in enqueue.await_args_list}
    assert file_statuses(env, pid) == ["accepted", "accepted"]   # legacy MUXING healed
    assert project_row(env, pid).status == "completed"           # translation done ≠ output
    assert project_row(env, pid).publish_state is None


# ---------------------------------------------------------------------------
# Publish API / claim  (D, E, 409, 404)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_publish_from_ready_enqueues_exactly_one_run(env):
    pid = seed_project(env)
    out, started = await click_publish(env, pid)
    assert started and env.enqueue.await_count == 1
    assert out.output.state == "publishing"
    kw = env.enqueue.await_args.kwargs
    assert kw["job_type"] == "publish_project"
    assert kw["dedupe_key"] == f"publish_project:{pid}:1"
    assert kw["payload"] == {"project_id": pid, "publish_attempt": 1}
    assert kw["max_attempts"] == 1          # no automatic retry


@pytest.mark.asyncio
async def test_second_publish_while_publishing_is_a_noop(env):
    pid = seed_project(env)
    await click_publish(env, pid)
    out, started = await click_publish(env, pid)
    assert not started and env.enqueue.await_count == 1
    assert out.output.state == "publishing"
    assert project_row(env, pid).publish_attempt == 1


@pytest.mark.asyncio
async def test_concurrent_publish_clicks_start_a_single_run(env):
    import asyncio
    pid = seed_project(env)
    results = await asyncio.gather(*(publish_orch.start_publish(pid, env.enqueue) for _ in range(5)))
    assert sum(r.started for r in results) == 1
    assert env.enqueue.await_count == 1


@pytest.mark.asyncio
async def test_publish_rejected_when_not_all_accepted(env):
    from app.api.routes.projects import publish_project_output
    pid = seed_project(env, n_files=2)
    with env.sync() as s:
        s.execute(update(File).where(File.relative_path == "ep2.mkv").values(status="review_required"))
        s.commit()
    with pytest.raises(HTTPException) as exc:
        await publish_project_output(pid)
    assert exc.value.status_code == 409
    assert exc.value.detail["accepted_files"] == 1 and exc.value.detail["total_files"] == 2
    env.enqueue.assert_not_awaited()
    assert project_row(env, pid).publish_state is None

    with pytest.raises(HTTPException) as missing:
        await publish_project_output(9999)
    assert missing.value.status_code == 404


# ---------------------------------------------------------------------------
# Handler: success, republish, rendering, atomicity, failure  (F, G, H, N, O, P, R, S)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_successful_publish_records_captured_revision(env, mux):
    pid = seed_project(env)
    revision = project_row(env, pid).output_revision
    result = await publish_once(env, pid)

    assert result["status"] == "succeeded", result
    row = project_row(env, pid)
    assert row.publish_state == "published"
    assert row.published_revision == revision == row.output_revision
    assert row.published_at and row.publish_error is None
    assert (await output_of(pid)).state == "published"
    for rel in ("ep1", "ep2"):
        assert (env.output_root / "series" / f"{rel}.mkv").exists()
        assert (env.output_root / "series" / f"{rel}.ass").exists()
    assert not list((env.output_root / "series").glob("*publish-tmp*"))      # staging cleaned up
    assert file_statuses(env, pid) == ["accepted", "accepted"]               # S


@pytest.mark.asyncio
async def test_files_stay_accepted_during_publish(env, mux):
    pid = seed_project(env)
    seen = []
    mux.hook = lambda src: seen.append(file_statuses(env, pid))
    await publish_once(env, pid)
    assert seen and all(s == ["accepted", "accepted"] for s in seen)


@pytest.mark.asyncio
async def test_published_ass_uses_the_existing_rendering_path(env, mux):
    """R: output ASS == build_ass(translated) + save_ass, same as the download path."""
    pid = seed_project(env, n_files=1)
    await publish_once(env, pid)

    with env.sync() as s:
        f = s.scalar(select(File))
        subs = build_ass(
            s.scalar(select(Subtitle)), s.scalars(select(SubtitleStyle)).all(),
            s.scalars(select(SubtitleEvent)).all(), text_variant="translated", title="Czech",
            use_font_replacements=True)
    expected = env.tmp / "expected.ass"
    save_ass(subs, expected)
    published = (env.output_root / "series" / "ep1.ass").read_bytes()
    assert published == expected.read_bytes()
    assert b"Ahoj 0" in published

    cmd = mux.calls[0]
    assert cmd[0] == "mkvmerge" and "--track-name" in cmd and "0:Czech" in cmd
    assert "0:cze" in cmd and "--default-track" in cmd


@pytest.mark.asyncio
async def test_publish_again_while_published_runs_a_fresh_job(env, mux):
    """G + H: no confirmation, a new attempt, output replaced."""
    pid = seed_project(env, n_files=1)
    await publish_once(env, pid)
    assert (await output_of(pid)).state == "published"
    first_calls = len(mux.calls)
    out_path = env.output_root / "series" / "ep1.mkv"
    out_path.write_bytes(b"STALE-OUTPUT")             # something to overwrite

    out, started = await click_publish(env, pid)
    assert started and out.output.state == "publishing"
    assert env.enqueue.await_args.kwargs["dedupe_key"] == f"publish_project:{pid}:2"
    result = run_job(env)

    assert result["status"] == "succeeded"
    assert len(mux.calls) == first_calls + 1          # actually muxed again
    assert out_path.read_bytes() != b"STALE-OUTPUT"
    assert (await output_of(pid)).state == "published"


@pytest.mark.asyncio
async def test_job_framework_dedupe_never_blocks_a_republish(env, monkeypatch):
    """Each click has its own dedupe key, so a *completed* run (which the real
    JobManager would otherwise treat as a stale duplicate) never swallows the next."""
    pid = seed_project(env)
    manager = JobManager()
    for attempt in (1, 2, 3):
        await publish_orch.start_publish(pid, manager.enqueue)
        with env.sync() as s:    # run "finished": job completed + project published
            s.execute(update(JobRecord).where(JobRecord.status == "queued").values(status="completed"))
            s.execute(update(Project).where(Project.id == pid).values(publish_state="published"))
            s.commit()
    with env.sync() as s:
        keys = sorted(s.scalars(select(JobRecord.dedupe_key).where(JobRecord.job_type == "publish_project")).all())
        max_attempts = set(s.scalars(select(JobRecord.max_attempts)).all())
    assert keys == [f"publish_project:{pid}:{n}" for n in (1, 2, 3)]
    assert max_attempts == {1}


@pytest.mark.asyncio
async def test_failure_sets_failed_with_error_and_retries_manually(env, mux):
    """N + O."""
    pid = seed_project(env)
    mux.fail_on = "ep2"
    result = await publish_once(env, pid)

    assert result["status"] == "failed" and result["error_code"] == "MKVMERGE_FAILED"
    row = project_row(env, pid)
    assert row.publish_state == "failed"
    assert "simulated mux failure" in row.publish_error and "ep2.mkv" in row.publish_error
    out = await output_of(pid)
    assert out.state == "failed" and "simulated mux failure" in out.error
    assert file_statuses(env, pid) == ["accepted", "accepted"]       # not stuck in a mux state

    # nothing retries on its own: orchestrators/sweep/reconcile enqueue nothing
    from app.orchestrator.orchestrator import sweep_all_projects
    enqueue = AsyncMock()
    await sweep_all_projects(enqueue)
    enqueue.assert_not_awaited()
    assert env.enqueue.await_count == 1

    # user clicks Publish again → fresh attempt, now succeeds
    mux.fail_on = None
    out, started = await click_publish(env, pid)
    assert started and project_row(env, pid).publish_attempt == 2
    assert run_job(env)["status"] == "succeeded"
    assert (await output_of(pid)).state == "published"
    assert (await output_of(pid)).error is None


@pytest.mark.asyncio
async def test_failed_republish_keeps_previous_output_intact(env, mux):
    """P: staging + atomic replace — a failed run never half-writes the finals."""
    pid = seed_project(env)
    await publish_once(env, pid)
    out_dir = env.output_root / "series"
    before = {p.name: p.read_bytes() for p in out_dir.iterdir()}
    assert {"ep1.mkv", "ep1.ass", "ep2.mkv", "ep2.ass"} <= set(before)

    with env.sync() as s:   # change the translation so a successful run would differ
        s.execute(update(SubtitleEvent).values(translated_text="Zmeneno"))
        s.commit()
    mux.fail_on = "ep2"     # ep1 stages fine, ep2 fails mid-run
    result = await publish_once(env, pid)

    assert result["status"] == "failed"
    after = {p.name: p.read_bytes() for p in out_dir.iterdir()}
    assert after == before                       # finals untouched, no tmp files left behind
    assert b"Zmeneno" not in b"".join(after.values())


@pytest.mark.asyncio
async def test_mkvmerge_exit_code_1_is_success_with_warnings(env, mux):
    pid = seed_project(env, n_files=1)
    mux.returncode = 1
    mux.stdout = b"Warning: 'ep1.mkv': something minor\n"
    result = await publish_once(env, pid)
    assert result["status"] == "succeeded"
    assert any("something minor" in w for w in result["result"]["warnings"])
    assert (await output_of(pid)).state == "published"


@pytest.mark.asyncio
async def test_missing_source_or_unaccepted_file_fails_the_run(env, mux):
    pid = seed_project(env)
    (env.import_root / "series" / "ep2.mkv").unlink()
    result = await publish_once(env, pid)
    assert result["error_code"] == "SOURCE_MISSING"
    assert project_row(env, pid).publish_state == "failed"
    assert not list((env.output_root / "series").glob("*.mkv"))    # nothing promoted

    pid2 = seed_project(env, source_directory="other")
    await click_publish(env, pid2)
    with env.sync() as s:   # a file leaves ACCEPTED between the click and the job start
        s.execute(update(File).where(File.project_id == pid2, File.relative_path == "ep2.mkv")
                  .values(status="processing"))
        s.commit()
    result = run_job(env)
    assert result["error_code"] == "FILES_NOT_ACCEPTED"
    assert project_row(env, pid2).publish_state == "failed"


@pytest.mark.asyncio
async def test_superseded_run_does_not_touch_state(env, mux):
    pid = seed_project(env)
    await click_publish(env, pid)
    stale_payload = dict(env.enqueue.await_args.kwargs["payload"])
    with env.sync() as s:   # a newer attempt owns the project
        s.execute(update(Project).where(Project.id == pid).values(publish_attempt=5))
        s.commit()
    result = publish_handler.publish_project(stale_payload, ctx_for(env), lambda *_: None)
    assert result["result"] == {"skipped": "superseded"}
    assert not mux.calls
    assert project_row(env, pid).publish_state == "publishing"


# ---------------------------------------------------------------------------
# Invalidation after publish  (I, J, K, L, M)
# ---------------------------------------------------------------------------

async def _published(env, mux, **kw) -> int:
    pid = seed_project(env, **kw)
    result = await publish_once(env, pid)
    assert result["status"] == "succeeded", result
    assert (await output_of(pid)).state == "published"
    return pid


@pytest.mark.asyncio
async def test_subtitle_edit_after_published_makes_ready_and_keeps_files_accepted(env, mux):
    from app.api.routes.projects import SubtitleEventUpdateIn, update_file_subtitle_event
    pid = await _published(env, mux)
    eid = event_id(env, pid)
    with env.sync() as s:
        fid = s.get(SubtitleEvent, eid).file_id
    before = project_row(env, pid).output_revision

    await update_file_subtitle_event(pid, fid, eid, SubtitleEventUpdateIn(translated_text="Nove"))

    assert project_row(env, pid).output_revision == before + 1
    out = await output_of(pid)
    assert out.state == "ready" and out.published_revision == before   # last published rev is kept
    assert file_statuses(env, pid) == ["accepted", "accepted"]
    # the existing published files are not deleted by the edit
    assert (env.output_root / "series" / "ep1.mkv").exists()


@pytest.mark.asyncio
async def test_revert_after_published_makes_ready(env, mux):
    from app.api.routes.projects import SubtitleEventUpdateIn, revert_file_subtitle_event, update_file_subtitle_event
    pid = seed_project(env)
    eid = event_id(env, pid)
    with env.sync() as s:
        fid = s.get(SubtitleEvent, eid).file_id
    await update_file_subtitle_event(pid, fid, eid, SubtitleEventUpdateIn(translated_text="Rucni"))
    assert (await publish_once(env, pid))["status"] == "succeeded"
    assert (await output_of(pid)).state == "published"

    await revert_file_subtitle_event(pid, fid, eid)
    assert (await output_of(pid)).state == "ready"


@pytest.mark.asyncio
async def test_non_output_changes_do_not_invalidate(env, mux):
    from app.api.routes.projects import resolve_file_qa_issue
    pid = await _published(env, mux)
    eid = event_id(env, pid)
    with env.sync() as s:
        fid = s.get(SubtitleEvent, eid).file_id
        qa = QaItem(file_id=fid, subtitle_event_id=eid, severity="warning", qa_type="x",
                    message="m", is_resolved=0, created_at=_now())
        s.add(qa)
        s.commit()
        qa_id = qa.id
    before = project_row(env, pid).output_revision

    await resolve_file_qa_issue(pid, fid, qa_id)                       # resolving a QA issue
    with env.sync() as s:                                              # QA/status/flag-only event changes
        ev = s.get(SubtitleEvent, eid)
        ev.is_approved = 1
        ev.is_locked = 1
        ev.translation_confidence = 0.4
        ev.translation_status = "translated"
        ev.translated_text = ev.translated_text                        # same value → no change
        s.commit()

    assert project_row(env, pid).output_revision == before
    assert (await output_of(pid)).state == "published"


@pytest.mark.asyncio
async def test_replacement_font_edit_after_published_makes_ready(env, mux):
    from app.api.routes.projects import SubtitleStyleUpdateIn, update_project_style
    pid = await _published(env, mux)
    with env.sync() as s:
        style_id = s.scalars(select(SubtitleStyle.id).where(SubtitleStyle.project_id == pid)).first()
    await update_project_style(pid, style_id, SubtitleStyleUpdateIn(
        replacement_font_name="Noto Sans", replacement_font_size=24.0))
    assert (await output_of(pid)).state == "ready"
    assert file_statuses(env, pid) == ["accepted", "accepted"]


@pytest.mark.asyncio
async def test_font_resolution_rewrite_invalidates(env, mux):
    """resolve_style_fonts writes replacement_* through the ORM (listener path)."""
    pid = await _published(env, mux)
    with env.sync() as s:
        style = s.scalars(select(SubtitleStyle).where(SubtitleStyle.project_id == pid)).first()
        style.font_check_status = "replaced"
        style.replacement_font_name = "Noto Sans"
        style.replacement_font_size = 22.0
        s.commit()
    assert (await output_of(pid)).state == "ready"


@pytest.mark.asyncio
async def test_replace_incompatible_fonts_option_invalidates_all_projects(env, mux):
    pid = await _published(env, mux)
    pid2 = seed_project(env, source_directory="second")
    before = {p: project_row(env, p).output_revision for p in (pid, pid2)}

    await options_store.aset("REPLACE_INCOMPATIBLE_FONTS", "0")
    after = {p: project_row(env, p).output_revision for p in (pid, pid2)}
    assert all(after[p] == before[p] + 1 for p in before)
    assert (await output_of(pid)).state == "ready"

    # same value again → no further invalidation; unrelated options never invalidate
    await options_store.aset("REPLACE_INCOMPATIBLE_FONTS", "0")
    await options_store.aset("CPS_LIMIT", "25")
    await options_store.aset("OPENAI_MODEL_CHEAP", "x")
    assert project_row(env, pid).output_revision == after[pid]

    # sync setter and target-language options too
    options_store.set("TARGET_LANG_NAME", "Czech")
    assert project_row(env, pid).output_revision == after[pid] + 1


@pytest.mark.asyncio
async def test_retranslate_and_reextraction_invalidate(env, mux, monkeypatch):
    from unittest.mock import patch
    from app.api.routes.projects import retranslate_file
    from app.db.models import SubtitleChunk

    pid = await _published(env, mux)
    with env.sync() as s:
        fid = s.scalars(select(File.id).where(File.project_id == pid)).first()
        s.add(SubtitleChunk(file_id=fid, chunk_index=0, translate_from_line=0, translate_to_line=0,
                            content_type="dialogue", status="complete", created_at=_now(), updated_at=_now()))
        s.commit()
    before = project_row(env, pid).output_revision

    with patch("app.api.routes.projects.orchestrate_file"):
        await retranslate_file(pid, fid)
    assert project_row(env, pid).output_revision > before
    # the reset file left ACCEPTED, so the project can't be READY/PUBLISHED now
    assert (await output_of(pid)).state == "not_ready"
    # re-accepting without a new publish gives READY — never the stale PUBLISHED
    with env.sync() as s:
        s.execute(update(File).where(File.id == fid).values(status="accepted"))
        s.commit()
    assert (await output_of(pid)).state == "ready"

    # re-extraction: Core delete/insert → explicit touch
    rev = project_row(env, pid).output_revision
    with env.sync() as s:
        from sqlalchemy import delete
        s.execute(delete(SubtitleEvent).where(SubtitleEvent.file_id == fid))
        touch_output_sync(s, pid)
        s.commit()
    assert project_row(env, pid).output_revision == rev + 1


@pytest.mark.asyncio
async def test_retranslate_affected_invalidates_and_reopens_completed_project(env, mux):
    from unittest.mock import patch
    from app.api.routes.projects import retranslate_affected_chunks
    from app.db.models import ProjectSpeaker, SubtitleChunk

    pid = await _published(env, mux)
    with env.sync() as s:
        fid = s.scalars(select(File.id).where(File.project_id == pid)).first()
        s.execute(update(SubtitleEvent).where(SubtitleEvent.file_id == fid).values(name="Alice"))
        s.add(SubtitleChunk(file_id=fid, chunk_index=0, translate_from_line=0, translate_to_line=5,
                            content_type="dialogue", status="complete", created_at=_now(), updated_at=_now()))
        sp = ProjectSpeaker(project_id=pid, name="Alice", created_at=_now(), updated_at=_now())
        s.add(sp)
        s.execute(update(Project).where(Project.id == pid).values(status="completed"))
        s.commit()
        sp_id = sp.id
    before = project_row(env, pid).output_revision

    with patch("app.api.routes.projects.orchestrate_project"):
        out = await retranslate_affected_chunks(pid, sp_id)

    assert out.affected_chunk_count == 1
    row = project_row(env, pid)
    assert row.output_revision > before
    assert row.status == "processing"            # the orchestrator would otherwise skip a COMPLETED project
    assert "processing" in file_statuses(env, pid)


@pytest.mark.asyncio
async def test_one_revision_bump_per_transaction(env):
    pid = seed_project(env)
    before = project_row(env, pid).output_revision
    with env.sync() as s:
        for ev in s.scalars(select(SubtitleEvent)).all():
            ev.translated_text = "x"
        s.flush()
        for ev in s.scalars(select(SubtitleEvent)).all():
            ev.translated_text = "y"
        touch_output_sync(s, pid)
        s.commit()
    assert project_row(env, pid).output_revision == before + 1


@pytest.mark.asyncio
async def test_edit_during_publishing_is_never_reported_as_current(env, mux):
    """M: the run publishes the revision it captured; an edit that lands while it
    runs leaves the project READY (not falsely PUBLISHED) once it finishes."""
    pid = seed_project(env)
    captured = project_row(env, pid).output_revision

    fired: list[int] = []

    def concurrent_edit(_src):
        if fired:       # edit once, while the first file is being muxed
            return
        fired.append(1)
        with env.sync() as s:
            ev = s.scalars(select(SubtitleEvent)).first()
            ev.translated_text = "Pozdni uprava"      # ORM path -> listener bump
            s.commit()

    mux.hook = concurrent_edit
    result = await publish_once(env, pid)

    assert result["status"] == "succeeded"
    row = project_row(env, pid)
    assert row.published_revision == captured          # exactly what was captured
    assert row.output_revision > captured
    out = await output_of(pid)
    assert out.state == "ready"
    assert file_statuses(env, pid) == ["accepted", "accepted"]


@pytest.mark.asyncio
async def test_obsolete_failure_does_not_mask_ready(env, mux):
    """A run that fails for an old revision, then data changes → READY, not FAILED."""
    from app.api.routes.projects import SubtitleEventUpdateIn, update_file_subtitle_event
    pid = seed_project(env)
    mux.fail_on = "ep1"
    await publish_once(env, pid)
    assert (await output_of(pid)).state == "failed"

    eid = event_id(env, pid)
    with env.sync() as s:
        fid = s.get(SubtitleEvent, eid).file_id
    await update_file_subtitle_event(pid, fid, eid, SubtitleEventUpdateIn(translated_text="Jine"))
    out = await output_of(pid)
    assert out.state == "ready" and out.error is None


# ---------------------------------------------------------------------------
# Reconciliation of dead runs (no automatic retry)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reconcile_fails_a_run_whose_job_is_gone_but_not_a_live_one(env):
    pid = seed_project(env)
    # claimed but never enqueued / job cancelled → reconciled to FAILED
    with env.sync() as s:
        s.execute(update(Project).where(Project.id == pid).values(
            publish_state="publishing", publish_attempt=1, publish_target_revision=0))
        s.commit()
    assert await publish_orch.reconcile_publish_state() == [pid]
    assert project_row(env, pid).publish_state == "failed"
    assert "interrupted" in project_row(env, pid).publish_error

    # a queued/running job keeps the run alive
    out, started = await click_publish(env, pid)
    assert started
    with env.sync() as s:
        s.add(JobRecord(project_id=pid, job_type="publish_project", status="queued",
                        dedupe_key=f"publish_project:{pid}:2", created_at=_now(), updated_at=_now()))
        s.commit()
    assert await publish_orch.reconcile_publish_state() == []
    assert project_row(env, pid).publish_state == "publishing"


@pytest.mark.asyncio
async def test_unexpected_handler_crash_is_recorded_as_failed(env, monkeypatch):
    pid = seed_project(env)
    await click_publish(env, pid)

    def boom(*a, **k):
        raise RuntimeError("disk exploded")
    monkeypatch.setattr(publish_handler, "build_ass", boom)
    result = run_job(env)
    assert result["status"] == "failed" and result["error_code"] == "UNEXPECTED_ERROR"
    assert "disk exploded" in project_row(env, pid).publish_error
    assert (await output_of(pid)).state == "failed"


# ---------------------------------------------------------------------------
# Migration / backfill (Q) — runs alembic in a subprocess against a scratch DB
# ---------------------------------------------------------------------------

def test_migration_backfill_preserves_sensible_state(tmp_path):
    import os
    backend = Path(__file__).resolve().parents[1]
    live_db = backend.parent / "config" / "subi-neko.db"
    live_mtime = live_db.stat().st_mtime if live_db.exists() else None

    scratch = tmp_path / "cfg"
    scratch.mkdir()
    env_vars = dict(os.environ, CONFIG_ROOT=str(scratch),
                    IMPORT_ROOT=str(tmp_path / "i"), OUTPUT_ROOT=str(tmp_path / "o"))

    def alembic(*args):
        r = subprocess.run([sys.executable, "-m", "alembic", *args], cwd=backend, env=env_vars,
                           capture_output=True, text=True)
        assert r.returncode == 0, r.stdout + r.stderr
        return r.stdout

    alembic("upgrade", "e7f8a9b0c1d2")
    db = scratch / "subi-neko.db"
    assert db.exists()
    con = sqlite3.connect(db)
    now = "2026-01-01T00:00:00"

    def proj(pid, status):
        con.execute(
            "INSERT INTO projects (id,name,source_directory,anime_provider,anime_external_id,status,is_paused,"
            "speaker_mapping_status,created_at,updated_at) VALUES (?,?,?,?,?,?,0,'complete',?,?)",
            (pid, f"P{pid}", f"d{pid}", "t", "1", status, now, now))

    def file(fid, pid, status, completed_at=None):
        con.execute(
            "INSERT INTO files (id,project_id,filename,relative_path,status,retry_count,created_at,updated_at,completed_at)"
            " VALUES (?,?,?,?,?,0,?,?,?)", (fid, pid, f"f{fid}", f"f{fid}", status, now, now, completed_at))

    proj(1, "completed"); file(11, 1, "completed", "2026-02-01T10:00:00"); file(12, 1, "completed", "2026-02-02T10:00:00")
    proj(2, "completed"); file(21, 2, "completed", "2026-02-01T10:00:00"); file(22, 2, "failed")
    proj(3, "processing"); file(31, 3, "muxing"); file(32, 3, "accepted")
    proj(4, "processing"); file(41, 4, "completed", "2026-02-01T10:00:00"); file(42, 4, "review_required")
    proj(5, "new")
    con.commit()
    con.close()

    alembic("upgrade", "head")
    assert "No new upgrade operations" in alembic("check")
    con = sqlite3.connect(db)
    rows = {r[0]: r[1:] for r in con.execute(
        "SELECT id, publish_state, published_revision, output_revision, publish_attempt, published_at FROM projects")}
    assert rows[1] == ("published", 0, 0, 0, "2026-02-02T10:00:00")      # fully muxed → PUBLISHED, consistent
    for pid in (2, 3, 4, 5):                                               # everything else: not published
        assert rows[pid][:4] == (None, None, 0, 0), (pid, rows[pid])
    statuses = dict(con.execute("SELECT id, status FROM files"))
    assert statuses == {11: "accepted", 12: "accepted", 21: "accepted", 22: "failed",
                        31: "accepted", 32: "accepted", 41: "accepted", 42: "review_required"}
    con.close()

    alembic("downgrade", "-1")
    alembic("upgrade", "head")
    assert live_mtime == (live_db.stat().st_mtime if live_db.exists() else None)   # live DB untouched
