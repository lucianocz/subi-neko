"""Options (join/soft-CPS), soft-vs-hard CPS semantics and the Final QC polish
endpoints (restore-AI, QA resolve reuse).

Fixtures come from ``tests.test_qc`` (real routers, file-backed DB).
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.api.routes import options as options_routes
from app.api.routes import projects as projects_routes
from app.api.routes import qc as qc_routes
from app.core.database import Base
from app.db import options as options_store
from app.db.models import File, Project, QaItem, SubtitleChunk, SubtitleEvent
from app.db.options import AppOptions, validate_option_changes
from app.jobs.context import JobContext
from app.jobs.handlers import review_chunk_final as rcf
from app.jobs.handlers.utils import allows_ai_edit
from app.subs.czech_checks import check_readability
from tests.test_publish import output_of
from tests.test_qc import (  # noqa: F401
    _mark_published, base, event_id, file_row, get_event, one, qc_env, revision,
)
from tests.test_publish import env, mux, seed_project  # noqa: F401


@pytest_asyncio.fixture
async def client(qc_env):
    app = FastAPI()
    app.include_router(projects_routes.router, prefix="/api")
    app.include_router(qc_routes.router, prefix="/api")
    app.include_router(options_routes.router, prefix="/api")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        yield c


# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------

def test_new_option_defaults():
    o = AppOptions.from_dict({})
    assert o.auto_join_short_lines is True
    assert o.join_lines_under == 45
    assert o.soft_cps_limit == 18.0
    assert o.cps_limit == 20.0           # hard limit untouched
    assert o.max_row_chars == 42         # row limit untouched
    assert o.auto_line_break is True


def test_stored_values_round_trip_through_from_dict():
    o = AppOptions.from_dict({
        "AUTO_JOIN_SHORT_LINES": "0", "JOIN_LINES_UNDER": "38", "SOFT_CPS_LIMIT": "15",
        "CPS_LIMIT": "22",
    })
    assert (o.auto_join_short_lines, o.join_lines_under, o.soft_cps_limit, o.cps_limit) == (
        False, 38, 15.0, 22.0)


def test_soft_at_or_above_hard_collapses_the_band_without_raising():
    # An old DB with a low hard limit and no stored soft limit must not break.
    assert AppOptions.from_dict({"CPS_LIMIT": "15"}).soft_cps_limit == 15.0
    assert AppOptions.from_dict({"CPS_LIMIT": "20", "SOFT_CPS_LIMIT": "20"}).soft_cps_limit == 20.0


@pytest.mark.parametrize("changes,current,fragment", [
    ({"SOFT_CPS_LIMIT": "20"}, {"CPS_LIMIT": "20"}, "lower than the hard"),
    ({"SOFT_CPS_LIMIT": "25"}, {}, "lower than the hard"),
    ({"CPS_LIMIT": "15"}, {}, "lower than the hard"),          # default soft is 18
    ({"CPS_LIMIT": "10", "SOFT_CPS_LIMIT": "10"}, {}, "lower than the hard"),
    ({"SOFT_CPS_LIMIT": "0"}, {}, "positive"),
    ({"CPS_LIMIT": "abc"}, {}, "positive"),
    ({"JOIN_LINES_UNDER": "-3"}, {}, "positive"),
    ({"JOIN_LINES_UNDER": "40.5"}, {}, "whole number"),
])
def test_validation_rejects(changes, current, fragment):
    problems = validate_option_changes(changes, current)
    assert problems and fragment in " ".join(problems)


@pytest.mark.parametrize("changes,current", [
    ({"SOFT_CPS_LIMIT": "15"}, {"CPS_LIMIT": "20"}),
    ({"CPS_LIMIT": "12", "SOFT_CPS_LIMIT": "10"}, {}),        # judged together
    ({"JOIN_LINES_UNDER": "50"}, {}),
    ({"OPENAI_API_BASE": "x"}, {"CPS_LIMIT": "15"}),           # unrelated key never trips it
])
def test_validation_accepts(changes, current):
    assert validate_option_changes(changes, current) == []


@pytest.mark.asyncio
async def test_options_api_exposes_and_persists_new_keys(qc_env, client):
    got = (await client.get("/api/options")).json()
    assert got["AUTO_JOIN_SHORT_LINES"] == "1"
    assert got["JOIN_LINES_UNDER"] == "45"
    assert got["SOFT_CPS_LIMIT"] == "18.0"
    assert got["CPS_LIMIT"] == "20.0"
    r = await client.patch("/api/options", json={
        "AUTO_JOIN_SHORT_LINES": "0", "JOIN_LINES_UNDER": "40", "SOFT_CPS_LIMIT": "16"})
    assert r.status_code == 204
    again = (await client.get("/api/options")).json()
    assert (again["AUTO_JOIN_SHORT_LINES"], again["JOIN_LINES_UNDER"], again["SOFT_CPS_LIMIT"]) == (
        "0", "40", "16.0")


@pytest.mark.asyncio
async def test_options_api_rejects_soft_ge_hard_and_writes_nothing(qc_env, client):
    r = await client.patch("/api/options", json={
        "JOIN_LINES_UNDER": "41", "SOFT_CPS_LIMIT": "20", "CPS_LIMIT": "20"})
    assert r.status_code == 422
    assert "lower than the hard" in r.json()["detail"]
    got = (await client.get("/api/options")).json()
    assert got["JOIN_LINES_UNDER"] == "45"           # the valid sibling key was not applied
    assert got["CPS_LIMIT"] == "20.0"


# ---------------------------------------------------------------------------
# Soft vs hard CPS: QA uses the HARD limit only
# ---------------------------------------------------------------------------

def test_readability_signature_has_no_soft_limit():
    import inspect
    assert "soft" not in " ".join(inspect.signature(check_readability).parameters)


def _qa_types(text: str, source: str, *, soft: float, duration_ms: int = 1000, hard: float = 20.0):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    Session = sessionmaker(engine, expire_on_commit=False)
    original = rcf.SyncSessionLocal
    rcf.SyncSessionLocal = Session
    try:
        with Session() as s:
            p = Project(name="P", source_directory="p", anime_provider="a", anime_external_id="1")
            s.add(p)
            s.flush()
            f = File(project_id=p.id, filename="a.mkv", relative_path="a.mkv")
            s.add(f)
            s.flush()
            s.add(SubtitleChunk(file_id=f.id, chunk_index=0, translate_from_line=0,
                                translate_to_line=0, status="polished", content_type="dialogue"))
            s.add(SubtitleEvent(
                file_id=f.id, line_index=0, event_type="dialogue", content_type="dialogue",
                layer=0, start_ms=0, end_ms=duration_ms, original_start_ms=0,
                original_end_ms=duration_ms, style="Default", source_text=source,
                translated_text=text, original_ai_translated_text=text,
                translation_status="validated"))
            s.commit()
            fid = f.id
        ctx = JobContext(Path("."), Path("."), AppOptions(
            cps_limit=hard, soft_cps_limit=soft, max_row_chars=100, auto_line_break=False,
            auto_join_short_lines=False))
        rcf.review_chunk_final({"file_id": fid, "chunk_index": 0}, ctx, lambda *a, **k: None)
        with Session() as s:
            return sorted(q.qa_type for q in s.scalars(select(QaItem).where(QaItem.file_id == fid)))
    finally:
        rcf.SyncSessionLocal = original


def test_soft_only_exceed_creates_no_qa_and_soft_never_changes_findings():
    nineteen = "x" * 19                                 # 19 CPS: soft(18) < 19 <= hard(20)
    for soft in (1.0, 18.0, 19.5):
        assert "high_cps" not in _qa_types(nineteen, "y" * 5, soft=soft)
    assert _qa_types(nineteen, "y" * 5, soft=1.0) == _qa_types(nineteen, "y" * 5, soft=19.5)


def test_exactly_hard_is_not_flagged_strict_greater_than():
    assert "high_cps" not in _qa_types("x" * 20, "y" * 5, soft=18.0)


def test_hard_exceed_still_flags_high_cps():
    for soft in (1.0, 18.0):
        assert "high_cps" in _qa_types("x" * 30, "y" * 5, soft=soft)


def test_hard_exceed_still_honours_source_floor():
    # 30 chars in 1 s is over the hard limit, but the English source needed
    # 40 chars in the same slot: protected, exactly as before.
    for soft in (1.0, 18.0):
        assert "high_cps" not in _qa_types("x" * 30, "y" * 40, soft=soft)


@pytest.mark.asyncio
async def test_qc_list_exposes_both_thresholds(qc_env, client, one):
    pid, fid = one
    await client.patch("/api/options", json={"CPS_LIMIT": "22", "SOFT_CPS_LIMIT": "17"})
    data = (await client.get(f"{base(pid, fid)}/events")).json()
    assert data["cps_limit"] == 22.0 and data["soft_cps_limit"] == 17.0


@pytest.mark.asyncio
async def test_editor_summary_exposes_both_thresholds(qc_env, client, one):
    pid, fid = one
    r = await client.get(f"/api/projects/{pid}/files/{fid}/subtitle-events?page_size=10")
    summary = r.json()["summary"]
    assert summary["cps_limit"] == 20.0 and summary["soft_cps_limit"] == 18.0


# ---------------------------------------------------------------------------
# Final QC: restore AI translation
# ---------------------------------------------------------------------------

def _edit(env, eid, **cols):
    with env.sync() as s:
        s.query(SubtitleEvent).filter(SubtitleEvent.id == eid).update(cols)
        s.commit()


@pytest.mark.asyncio
async def test_restore_ai_returns_exact_baseline_and_locks(qc_env, client, one):
    pid, fid = one
    eid = event_id(qc_env, pid, 0)
    _edit(qc_env, eid, translated_text="Úplně jiný, mnohem delší text, který se mění", is_user_edited=1)
    await _mark_published(qc_env, pid)
    before = revision(qc_env, pid)

    r = await client.post(f"{base(pid, fid)}/events/{eid}/restore-ai")
    assert r.status_code == 200
    out = r.json()
    assert out["translated_text"] == "Ahoj 0"
    assert out["is_user_edited"] is False and out["is_locked"] is True
    assert out["cps"] == pytest.approx(len("Ahoj 0") / 1.5)       # authoritative, recomputed
    assert r.headers["x-output-revision"] == str(before + 1)
    assert revision(qc_env, pid) == before + 1
    assert (await output_of(pid)).state == "ready"
    assert file_row(qc_env, pid).status == "accepted"
    ev = get_event(qc_env, eid)
    assert ev.translated_text == ev.original_ai_translated_text == "Ahoj 0"
    # QC-protected: no later pipeline step may overwrite the restored line.
    assert allows_ai_edit(ev.is_user_edited, ev.is_locked) is False


@pytest.mark.asyncio
async def test_restore_ai_noop_when_already_baseline_does_not_invalidate(qc_env, client, one):
    pid, fid = one
    eid = event_id(qc_env, pid, 0)
    await _mark_published(qc_env, pid)
    before = revision(qc_env, pid)
    r = await client.post(f"{base(pid, fid)}/events/{eid}/restore-ai")
    assert r.status_code == 200
    assert revision(qc_env, pid) == before
    assert (await output_of(pid)).state == "published"
    assert get_event(qc_env, eid).is_locked == 1


@pytest.mark.asyncio
async def test_restore_ai_without_baseline_is_409(qc_env, client, one):
    pid, fid = one
    eid = event_id(qc_env, pid, 0)
    _edit(qc_env, eid, original_ai_translated_text=None, translated_text="Ručně")
    r = await client.post(f"{base(pid, fid)}/events/{eid}/restore-ai")
    assert r.status_code == 409
    assert get_event(qc_env, eid).translated_text == "Ručně"


@pytest.mark.asyncio
async def test_restore_ai_requires_qc_availability(qc_env, client, one):
    pid, fid = one
    from tests.test_qc import add_chunks
    add_chunks(qc_env, fid, "pending")
    eid = event_id(qc_env, pid, 0)
    assert (await client.post(f"{base(pid, fid)}/events/{eid}/restore-ai")).status_code == 409


@pytest.mark.asyncio
async def test_legacy_revert_is_unchanged_and_does_not_lock(qc_env, client, one):
    pid, fid = one
    eid = event_id(qc_env, pid, 0)
    _edit(qc_env, eid, translated_text="Jiný", is_user_edited=1, is_locked=0)
    r = await client.post(f"/api/projects/{pid}/files/{fid}/subtitle-events/{eid}/revert")
    assert r.status_code == 200
    ev = get_event(qc_env, eid)
    assert ev.translated_text == "Ahoj 0" and ev.is_user_edited == 0 and ev.is_locked == 0


# ---------------------------------------------------------------------------
# Final QC: resolving a QA issue reuses the editor endpoint, no output change
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resolving_qa_issue_is_review_state_only(qc_env, client, one, monkeypatch):
    monkeypatch.setattr(projects_routes, "orchestrate_file", AsyncMock())
    pid, fid = one
    eid = event_id(qc_env, pid, 0)
    with qc_env.sync() as s:
        qa = QaItem(file_id=fid, subtitle_event_id=eid, severity="warning", qa_type="high_cps",
                    message="m", is_resolved=0, created_at="2026-01-01T00:00:00")
        s.add(qa)
        s.commit()
        qid = qa.id
    await _mark_published(qc_env, pid)
    before = revision(qc_env, pid)
    ev_before = get_event(qc_env, eid)
    snapshot = (ev_before.translated_text, ev_before.start_ms, ev_before.end_ms)

    r = await client.post(f"/api/projects/{pid}/files/{fid}/qa-issues/{qid}/resolve")
    assert r.status_code == 200
    assert [i["is_resolved"] for i in r.json()["issues"]] == [True]
    assert revision(qc_env, pid) == before
    assert (await output_of(pid)).state == "published"
    ev = get_event(qc_env, eid)
    assert (ev.translated_text, ev.start_ms, ev.end_ms) == snapshot

    detail = (await client.get(f"{base(pid, fid)}/events/{eid}")).json()
    assert detail["issue_count"] == 0 and detail["max_issue_severity"] is None
    assert detail["issues"][0]["is_resolved"] is True       # resolved issues stay visible
