"""Final QC backend: availability, event list/detail, PATCH, hide/restore, manual
events, retranslate reset, translated/source ASS semantics and the preview.

Runs the real routers over httpx's ASGI transport against the file-backed DB of
``test_publish.env`` (sync + async engines on one SQLite file).
"""
from __future__ import annotations

from datetime import datetime

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import select

from app.api.routes import projects as projects_routes
from app.api.routes import qc as qc_routes
from app.db.models import (
    File,
    Project,
    ProjectWatchedWord,
    QaItem,
    Subtitle,
    SubtitleChunk,
    SubtitleEvent,
    SubtitleStyle,
    file_subtitle_styles,
)
from app.db import options as options_store
from app.db.qc_state import is_qc_available, qc_available_map
from app.jobs.handlers.utils import allows_ai_edit
from app.subs.ass_rendering import build_ass
from tests.test_publish import env, event_id, mux, output_of, seed_project  # noqa: F401


def _now() -> str:
    return datetime.utcnow().isoformat()


@pytest.fixture
def qc_env(env, monkeypatch):  # noqa: F811
    monkeypatch.setattr("app.api.routes.qc.AsyncSessionLocal", env.asess)
    return env


@pytest_asyncio.fixture
async def client(qc_env):
    app = FastAPI()
    app.include_router(projects_routes.router, prefix="/api")
    app.include_router(qc_routes.router, prefix="/api")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        yield c


def add_chunks(env, file_id: int, *statuses: str) -> None:
    with env.sync() as s:
        for i, st in enumerate(statuses):
            s.add(SubtitleChunk(file_id=file_id, chunk_index=i, translate_from_line=i,
                                translate_to_line=i, status=st))
        s.commit()


def add_event(env, file_id: int, line_index: int, **kw) -> int:
    values = dict(
        file_id=file_id, line_index=line_index, event_type="dialogue", content_type="dialogue",
        layer=0, start_ms=line_index * 1000, end_ms=line_index * 1000 + 800, style="Default",
        source_text=f"src {line_index}", translated_text=f"tr {line_index}",
        original_ai_translated_text=f"tr {line_index}", translation_status="translated",
        created_at=_now(), updated_at=_now(),
    )
    values.update(kw)
    values.setdefault("original_start_ms", values["start_ms"])
    values.setdefault("original_end_ms", values["end_ms"])
    with env.sync() as s:
        ev = SubtitleEvent(**values)
        s.add(ev)
        s.commit()
        return ev.id


def get_event(env, eid: int) -> SubtitleEvent:
    with env.sync() as s:
        return s.get(SubtitleEvent, eid)


def file_row(env, pid: int) -> File:
    with env.sync() as s:
        return s.scalars(select(File).where(File.project_id == pid)).first()


def revision(env, pid: int) -> int:
    with env.sync() as s:
        return s.get(Project, pid).output_revision


def base(pid: int, fid: int) -> str:
    return f"/api/projects/{pid}/files/{fid}/qc"


def build(env, fid: int, variant: str):
    with env.sync() as s:
        subtitle = s.scalar(select(Subtitle).where(Subtitle.file_id == fid))
        styles = s.scalars(
            select(SubtitleStyle)
            .join(file_subtitle_styles, file_subtitle_styles.c.subtitle_style_id == SubtitleStyle.id)
            .where(file_subtitle_styles.c.file_id == fid)).all()
        events = s.scalars(
            select(SubtitleEvent).where(SubtitleEvent.file_id == fid)
            .order_by(SubtitleEvent.line_index)).all()
        return build_ass(subtitle, styles, events, text_variant=variant, title="")


def texts(subs) -> list[str]:
    return [e.text for e in subs.events]


@pytest_asyncio.fixture
async def one(qc_env):
    """Published, accepted, single-file project with one extra event (line 1)."""
    pid = seed_project(qc_env, n_files=1)
    fid = file_row(qc_env, pid).id
    add_event(qc_env, fid, 1)
    return pid, fid


async def _mark_published(env, pid: int) -> None:
    from sqlalchemy import update
    with env.sync() as s:
        s.execute(update(Project).where(Project.id == pid).values(
            publish_state="published", published_revision=Project.output_revision))
        s.commit()
    assert (await output_of(pid)).state == "published"


# ---------------------------------------------------------------------------
# A-E: availability
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_qc_available_all_chunks_complete(qc_env):
    pid = seed_project(qc_env, n_files=1)
    fid = file_row(qc_env, pid).id
    add_chunks(qc_env, fid, "complete", "complete")
    async with qc_env.asess() as s:
        assert await is_qc_available(s, fid) is True


@pytest.mark.asyncio
async def test_qc_unavailable_with_pending_chunk(qc_env):
    pid = seed_project(qc_env, n_files=1)
    fid = file_row(qc_env, pid).id
    add_chunks(qc_env, fid, "complete", "pending")
    async with qc_env.asess() as s:
        assert await is_qc_available(s, fid) is False


@pytest.mark.asyncio
async def test_qc_available_with_zero_chunks(qc_env):
    pid = seed_project(qc_env, n_files=1)
    fid = file_row(qc_env, pid).id
    async with qc_env.asess() as s:
        assert await is_qc_available(s, fid) is True
        assert await qc_available_map(s, [fid]) == {fid: True}


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["job_failed", "validate_repair_failed", "needs_polish"])
async def test_every_non_complete_status_blocks(qc_env, terminal):
    pid = seed_project(qc_env, n_files=1)
    fid = file_row(qc_env, pid).id
    add_chunks(qc_env, fid, terminal)
    async with qc_env.asess() as s:
        assert await is_qc_available(s, fid) is False


@pytest.mark.asyncio
async def test_qc_availability_independent_of_acceptance(qc_env):
    for status in ("accepted", "review_required", "ready"):
        pid = seed_project(qc_env, n_files=1, status=status, source_directory=f"d-{status}")
        fid = file_row(qc_env, pid).id
        add_chunks(qc_env, fid, "complete")
        async with qc_env.asess() as s:
            assert await is_qc_available(s, fid) is True, status


@pytest.mark.asyncio
async def test_endpoints_reject_unavailable_file_and_dto_exposes_flag(qc_env, client):
    pid = seed_project(qc_env, n_files=1)
    fid = file_row(qc_env, pid).id
    eid = add_event(qc_env, fid, 1)
    add_chunks(qc_env, fid, "complete", "translated")
    b = base(pid, fid)
    assert (await client.get(f"{b}/events")).status_code == 409
    assert (await client.get(f"{b}/events/{eid}")).status_code == 409
    assert (await client.patch(f"{b}/events/{eid}", json={"translated_text": "x"})).status_code == 409
    assert (await client.post(f"{b}/events/{eid}/hide")).status_code == 409
    assert (await client.post(f"{b}/events/{eid}/restore")).status_code == 409
    assert (await client.post(f"{b}/events", json={
        "start_ms": 0, "end_ms": 100, "translated_text": "x", "style": "Default"})).status_code == 409
    assert (await client.get(f"{b}/preview.ass")).status_code == 409

    files = (await client.get(f"/api/projects/{pid}/files")).json()
    assert files[0]["qc_available"] is False
    with qc_env.sync() as s:
        s.query(SubtitleChunk).update({"status": "complete"})
        s.commit()
    files = (await client.get(f"/api/projects/{pid}/files")).json()
    assert files[0]["qc_available"] is True
    assert (await client.get(f"{b}/events")).status_code == 200


@pytest.mark.asyncio
async def test_unknown_file_or_wrong_project_is_404(qc_env, client, one):
    pid, fid = one
    assert (await client.get(f"{base(pid, 9999)}/events")).status_code == 404
    assert (await client.get(f"{base(pid + 1, fid)}/events")).status_code == 404
    other = seed_project(qc_env, n_files=1, source_directory="other")
    other_fid = file_row(qc_env, other).id
    eid = add_event(qc_env, other_fid, 5)
    assert (await client.get(f"{base(pid, fid)}/events/{eid}")).status_code == 404  # wrong file


# ---------------------------------------------------------------------------
# F-G: list
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_sorted_by_start_then_line_index_and_hides_hidden(qc_env, client):
    pid = seed_project(qc_env, n_files=1)
    fid = file_row(qc_env, pid).id           # existing line 0: 0..1500
    late = add_event(qc_env, fid, 1, start_ms=5000, end_ms=6000)
    tie_b = add_event(qc_env, fid, 2, start_ms=2000, end_ms=3000)
    tie_a = add_event(qc_env, fid, 3, start_ms=2000, end_ms=2500)   # same start, higher index
    hidden = add_event(qc_env, fid, 4, start_ms=100, end_ms=200, is_hidden=1)
    manual = add_event(qc_env, fid, 5, start_ms=1000, end_ms=1200, is_manual=1, source_text="")

    r = (await client.get(f"{base(pid, fid)}/events")).json()
    assert [e["line_index"] for e in r["events"]] == [0, 5, 2, 3, 1]   # manual placed by time
    assert hidden not in [e["id"] for e in r["events"]]
    assert (r["event_count"], r["total_count"], r["hidden_count"]) == (5, 6, 1)
    assert r["qc_available"] is True and r["filename"] == "ep1.mkv"
    assert r["output_revision"] == revision(qc_env, pid)

    r = (await client.get(f"{base(pid, fid)}/events", params={"show_hidden": "true"})).json()
    assert [e["line_index"] for e in r["events"]] == [0, 4, 5, 2, 3, 1]
    assert next(e for e in r["events"] if e["id"] == hidden)["is_hidden"] is True
    assert next(e for e in r["events"] if e["id"] == manual)["is_manual"] is True
    assert {late, tie_a, tie_b} <= {e["id"] for e in r["events"]}


@pytest.mark.asyncio
async def test_list_is_compact_and_summarizes_issues_and_watched(qc_env, client):
    pid = seed_project(qc_env, n_files=1)
    fid = file_row(qc_env, pid).id
    eid = add_event(qc_env, fid, 1, translated_text="Ahoj Naruto", source_text="Hello Naruto")
    with qc_env.sync() as s:
        for sev, resolved in (("warning", 0), ("blocker", 0), ("info", 0), ("blocker", 1)):
            s.add(QaItem(file_id=fid, subtitle_event_id=eid, severity=sev, qa_type="t",
                         message="m", details_json='{"big": "payload"}', is_resolved=resolved,
                         created_at=_now()))
        s.add(ProjectWatchedWord(project_id=pid, word="naruto", word_type="original",
                                 created_at=_now(), updated_at=_now()))
        s.add(ProjectWatchedWord(project_id=pid, word="Naruto", word_type="translated",
                                 created_at=_now(), updated_at=_now()))
        s.commit()
    row = next(e for e in (await client.get(f"{base(pid, fid)}/events")).json()["events"]
               if e["id"] == eid)
    assert row["issue_count"] == 3                 # resolved one excluded
    assert row["max_issue_severity"] == "blocker"
    assert row["watched_count"] == 2
    assert "issues" not in row and "details_json" not in row

    detail = (await client.get(f"{base(pid, fid)}/events/{eid}")).json()
    assert len(detail["issues"]) == 4              # full QA incl. resolved
    assert detail["issues"][0]["is_resolved"] is False
    assert {(m["word"], m["word_type"]) for m in detail["watched_matches"]} == {
        ("naruto", "original"), ("Naruto", "translated")}
    assert detail["original_ai_translated_text"] == "tr 1"


# ---------------------------------------------------------------------------
# H-O: PATCH
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_text_patch_recalculates_cps_and_preserves_timing_exactly(qc_env, client, one):
    pid, fid = one
    eid = add_event(qc_env, fid, 7, start_ms=12345, end_ms=14345)   # deliberately off-grid
    r = await client.patch(f"{base(pid, fid)}/events/{eid}", json={"translated_text": "a" * 20})
    assert r.status_code == 200
    body = r.json()
    assert body["translated_text"] == "a" * 20 and body["cps"] == 10.0
    assert (body["start_ms"], body["end_ms"]) == (12345, 14345)      # untouched, not quantized
    ev = get_event(qc_env, eid)
    assert (ev.start_ms, ev.end_ms) == (12345, 14345)
    assert ev.is_user_edited == 1 and ev.original_ai_translated_text == "tr 7"


@pytest.mark.asyncio
async def test_raw_ass_tags_are_preserved(qc_env, client, one):
    pid, fid = one
    eid = event_id(qc_env, pid, 0)
    text = r"{\an8\i1}Ahoj{\i0}\Nsvět {\fad(100,200)}"
    r = await client.patch(f"{base(pid, fid)}/events/{eid}", json={"translated_text": text})
    assert r.json()["translated_text"] == text
    assert get_event(qc_env, eid).translated_text == text
    assert text in texts(build(qc_env, fid, "translated"))[0].replace("\\N", "\\N")


@pytest.mark.asyncio
async def test_timing_patch_and_quantization(qc_env, client, one):
    pid, fid = one
    eid = event_id(qc_env, pid, 0)
    r = await client.patch(f"{base(pid, fid)}/events/{eid}", json={"start_ms": 1234, "end_ms": 5675})
    assert r.status_code == 200
    assert (r.json()["start_ms"], r.json()["end_ms"]) == (1230, 5680)   # nearest 10 ms, half up
    ev = get_event(qc_env, eid)
    assert (ev.start_ms, ev.end_ms) == (1230, 5680)
    assert ev.translated_text == "Ahoj 0" and ev.is_user_edited == 0   # text untouched

    # one bound only: the other keeps its exact stored value
    r = await client.patch(f"{base(pid, fid)}/events/{eid}", json={"end_ms": 9999})
    assert (r.json()["start_ms"], r.json()["end_ms"]) == (1230, 10000)


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    {"start_ms": 2000, "end_ms": 2000},
    {"start_ms": 3000, "end_ms": 1000},
    {"start_ms": 1000, "end_ms": 1004},      # equal after quantization
    {"start_ms": -10},
    {"end_ms": 0},                           # <= existing start (0)
    {"start_ms": 99999},                     # beyond existing end
    {"start_ms": None},
    {"translated_text": None},
    {},
])
async def test_invalid_patch_rejected_and_nothing_changes(qc_env, client, one, payload):
    pid, fid = one
    eid = event_id(qc_env, pid, 0)
    rev = revision(qc_env, pid)
    r = await client.patch(f"{base(pid, fid)}/events/{eid}", json=payload)
    assert r.status_code == 422
    ev = get_event(qc_env, eid)
    assert (ev.start_ms, ev.end_ms, ev.is_locked) == (0, 1500, 0)
    assert revision(qc_env, pid) == rev


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [
    ("style", "Other"), ("source_text", "x"), ("line_index", 99),
    ("event_type", "comment"), ("content_type", "sign"), ("layer", 3), ("is_hidden", True),
])
async def test_forbidden_patch_fields_rejected(qc_env, client, one, field, value):
    pid, fid = one
    eid = event_id(qc_env, pid, 0)
    r = await client.patch(f"{base(pid, fid)}/events/{eid}", json={field: value})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_edit_locks_event_keeps_acceptance_and_bumps_revision(qc_env, client, one):
    pid, fid = one
    eid = event_id(qc_env, pid, 0)
    await _mark_published(qc_env, pid)
    for payload in ({"translated_text": "Nové"}, {"start_ms": 100}, {"end_ms": 1700}):
        with qc_env.sync() as s:
            s.query(SubtitleEvent).update({"is_locked": 0})
            s.commit()
        await _mark_published(qc_env, pid)
        before = revision(qc_env, pid)
        assert (await client.patch(f"{base(pid, fid)}/events/{eid}", json=payload)).status_code == 200
        assert get_event(qc_env, eid).is_locked == 1
        assert revision(qc_env, pid) == before + 1            # exactly one bump
        assert (await output_of(pid)).state == "ready"
        assert file_row(qc_env, pid).status == "accepted"
        with qc_env.sync() as s:
            assert s.get(Project, pid).status == "completed"


@pytest.mark.asyncio
async def test_noop_patch_neither_locks_nor_invalidates(qc_env, client, one):
    pid, fid = one
    eid = event_id(qc_env, pid, 0)
    await _mark_published(qc_env, pid)
    before = revision(qc_env, pid)
    r = await client.patch(f"{base(pid, fid)}/events/{eid}",
                           json={"translated_text": "Ahoj 0", "start_ms": 0, "end_ms": 1500})
    assert r.status_code == 200
    assert get_event(qc_env, eid).is_locked == 0
    assert revision(qc_env, pid) == before
    assert (await output_of(pid)).state == "published"


@pytest.mark.asyncio
async def test_qa_items_are_not_rerun_or_modified_by_edit(qc_env, client, one):
    pid, fid = one
    eid = event_id(qc_env, pid, 0)
    with qc_env.sync() as s:
        s.add(QaItem(file_id=fid, subtitle_event_id=eid, severity="warning", qa_type="t",
                     message="m", is_resolved=0, created_at=_now()))
        s.commit()
    r = await client.patch(f"{base(pid, fid)}/events/{eid}", json={"translated_text": "Jiný"})
    assert r.json()["issue_count"] == 1                      # stale-but-kept, by design
    qc_env.enqueue.assert_not_awaited()                      # no orchestration side effect


@pytest.mark.asyncio
async def test_locked_qc_edit_blocks_all_ai_edit_paths(qc_env, client, one):
    pid, fid = one
    eid = event_id(qc_env, pid, 0)
    await client.patch(f"{base(pid, fid)}/events/{eid}", json={"start_ms": 50})   # timing only
    ev = get_event(qc_env, eid)
    assert ev.is_user_edited == 0 and ev.is_locked == 1
    # Every translate/repair/polish/validate path gates on allows_ai_edit(...)
    # (review_chunk_final on is_user_edited or is_locked): the lock alone suffices.
    assert allows_ai_edit(ev.is_user_edited, ev.is_locked) is False


# ---------------------------------------------------------------------------
# P-S: hide / restore + source/translated semantics
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_hide_and_restore_semantics(qc_env, client, one):
    pid, fid = one
    eid = event_id(qc_env, pid, 0)
    await _mark_published(qc_env, pid)
    rev = revision(qc_env, pid)

    r = await client.post(f"{base(pid, fid)}/events/{eid}/hide")
    assert r.status_code == 200 and r.json()["is_hidden"] is True
    assert revision(qc_env, pid) == rev + 1
    assert (await output_of(pid)).state == "ready"
    assert file_row(qc_env, pid).status == "accepted"
    assert get_event(qc_env, eid) is not None                 # row kept

    assert "Ahoj 0" not in texts(build(qc_env, fid, "translated"))
    assert "Hello 0" in texts(build(qc_env, fid, "original"))  # source untouched

    # idempotent: hiding again changes nothing
    assert (await client.post(f"{base(pid, fid)}/events/{eid}/hide")).status_code == 200
    assert revision(qc_env, pid) == rev + 1

    listed = (await client.get(f"{base(pid, fid)}/events")).json()
    assert eid not in [e["id"] for e in listed["events"]]

    await _mark_published(qc_env, pid)
    rev = revision(qc_env, pid)
    r = await client.post(f"{base(pid, fid)}/events/{eid}/restore")
    assert r.json()["is_hidden"] is False
    assert revision(qc_env, pid) == rev + 1
    assert (await output_of(pid)).state == "ready"
    assert "Ahoj 0" in texts(build(qc_env, fid, "translated"))


@pytest.mark.asyncio
async def test_normal_events_render_unchanged(qc_env, one):
    pid, fid = one
    assert texts(build(qc_env, fid, "translated")) == ["Ahoj 0", "tr 1"]
    assert texts(build(qc_env, fid, "original")) == ["Hello 0", "src 1"]


@pytest.mark.asyncio
async def test_qc_edits_do_not_alter_original_source_text(qc_env, client, one):
    """Source ASS keeps the extracted text. NOTE: timing has a single stored
    value, so a QC timing edit is visible in the source variant too (documented)."""
    pid, fid = one
    eid = event_id(qc_env, pid, 0)
    await client.patch(f"{base(pid, fid)}/events/{eid}", json={"translated_text": "Změna"})
    assert texts(build(qc_env, fid, "original"))[0] == "Hello 0"
    assert texts(build(qc_env, fid, "translated"))[0] == "Změna"


# ---------------------------------------------------------------------------
# T-Y: manual events
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_manual_event_creation(qc_env, client, one):
    pid, fid = one
    await _mark_published(qc_env, pid)
    rev = revision(qc_env, pid)
    r = await client.post(f"{base(pid, fid)}/events", json={
        "start_ms": 2503, "end_ms": 4000, "translated_text": r"{\an8}Fansub sign",
        "style": "Default", "speaker": " Tomáš "})
    assert r.status_code == 201
    body = r.json()
    assert body["is_manual"] is True and body["is_locked"] is True and body["is_hidden"] is False
    assert (body["start_ms"], body["end_ms"]) == (2500, 4000)
    assert body["source_text"] == "" and body["speaker"] == "Tomáš"
    assert body["line_index"] == 2                      # max(0, 1) + 1
    assert (body["event_type"], body["content_type"]) == ("dialogue", "sign")
    ev = get_event(qc_env, body["id"])
    assert ev.translation_status == "translated" and ev.original_ai_translated_text is None
    assert revision(qc_env, pid) == rev + 1
    assert (await output_of(pid)).state == "ready"
    assert file_row(qc_env, pid).status == "accepted"

    assert r"{\an8}Fansub sign" in texts(build(qc_env, fid, "translated"))
    assert all("Fansub" not in t for t in texts(build(qc_env, fid, "original")))
    assert len(build(qc_env, fid, "original").events) == 2

    # listed by time between existing rows? start 2500 > line 1 (1000) -> last
    listed = (await client.get(f"{base(pid, fid)}/events")).json()["events"]
    assert [e["id"] for e in listed][-1] == body["id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    {"end_ms": 100, "translated_text": "x", "style": "Default"},            # no start
    {"start_ms": 0, "translated_text": "x", "style": "Default"},            # no end
    {"start_ms": 0, "end_ms": 100, "style": "Default"},                     # no text
    {"start_ms": 0, "end_ms": 100, "translated_text": "x"},                 # no style
    {"start_ms": 0, "end_ms": 100, "translated_text": "", "style": "Default"},
    {"start_ms": 100, "end_ms": 100, "translated_text": "x", "style": "Default"},
    {"start_ms": 0, "end_ms": 100, "translated_text": "x", "style": "Nope"},  # unknown style
    {"start_ms": 0, "end_ms": 100, "translated_text": "x", "style": "Default", "is_manual": 0},
])
async def test_manual_event_validation(qc_env, client, one, payload):
    pid, fid = one
    rev = revision(qc_env, pid)
    r = await client.post(f"{base(pid, fid)}/events", json=payload)
    assert r.status_code == 422
    assert revision(qc_env, pid) == rev
    with qc_env.sync() as s:
        assert s.query(SubtitleEvent).filter_by(file_id=fid).count() == 2


@pytest.mark.asyncio
async def test_manual_style_must_belong_to_this_file(qc_env, client):
    a = seed_project(qc_env, n_files=1, source_directory="a")
    fid = file_row(qc_env, a).id
    with qc_env.sync() as s:   # a style that exists in the project but is not linked to the file
        s.add(SubtitleStyle(project_id=a, source_style_hash="zz", style_name="Unlinked",
                            font_name="Arial", font_size=20.0, created_at=_now(), updated_at=_now()))
        s.commit()
    r = await client.post(f"{base(a, fid)}/events", json={
        "start_ms": 0, "end_ms": 100, "translated_text": "x", "style": "Unlinked"})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_hidden_manual_event_is_excluded_from_translated_output(qc_env, client, one):
    pid, fid = one
    body = (await client.post(f"{base(pid, fid)}/events", json={
        "start_ms": 0, "end_ms": 100, "translated_text": "Sign", "style": "Default"})).json()
    assert "Sign" in texts(build(qc_env, fid, "translated"))
    await client.post(f"{base(pid, fid)}/events/{body['id']}/hide")
    assert "Sign" not in texts(build(qc_env, fid, "translated"))
    assert "Sign" not in texts(build(qc_env, fid, "original"))


# ---------------------------------------------------------------------------
# Z: retranslate reset
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_full_retranslate_removes_manual_events_and_resets_hidden(qc_env, client, one):
    pid, fid = one
    add_chunks(qc_env, fid, "complete")
    eid = event_id(qc_env, pid, 0)
    manual = (await client.post(f"{base(pid, fid)}/events", json={
        "start_ms": 0, "end_ms": 100, "translated_text": "Sign", "style": "Default"})).json()["id"]
    await client.post(f"{base(pid, fid)}/events/{eid}/hide")
    await client.patch(f"{base(pid, fid)}/events/{eid}", json={"translated_text": "Ručně"})

    r = await client.post(f"/api/projects/{pid}/files/{fid}/retranslate")
    assert r.status_code == 200, r.text
    with qc_env.sync() as s:
        rows = s.scalars(select(SubtitleEvent).where(SubtitleEvent.file_id == fid)).all()
        assert manual not in [e.id for e in rows]
        assert len(rows) == 2                                   # both originals survive
        assert all(e.is_manual == 0 and e.is_hidden == 0 and e.is_locked == 0 for e in rows)
        assert all(e.translated_text is None for e in rows)


@pytest.mark.asyncio
async def test_retranslate_affected_does_not_delete_manual_or_reset_qc_state(qc_env, one):
    """Speaker retranslate resets chunk status only; events keep QC state, and
    locked (QC-edited) lines survive because translate/polish skip them."""
    from app.db.models import ProjectSpeaker
    pid, fid = one
    add_chunks(qc_env, fid, "complete")
    eid = event_id(qc_env, pid, 0)
    manual = add_event(qc_env, fid, 9, is_manual=1, is_locked=1, source_text="", name="Tom")
    with qc_env.sync() as s:
        s.query(SubtitleEvent).filter_by(id=eid).update({"is_hidden": 1, "is_locked": 1, "name": "Tom"})
        sp = ProjectSpeaker(project_id=pid, name="Tom", created_at=_now(), updated_at=_now())
        s.add(sp)
        s.commit()
        spid = sp.id
    await projects_routes.retranslate_affected_chunks(pid, spid)
    ev, man = get_event(qc_env, eid), get_event(qc_env, manual)
    assert (ev.is_hidden, ev.is_locked) == (1, 1)
    assert man is not None and man.is_manual == 1


# ---------------------------------------------------------------------------
# AA-AC: preview
# ---------------------------------------------------------------------------

def _preview_text(subs_resp) -> str:
    return subs_resp.content.decode("utf-8")


@pytest.mark.asyncio
async def test_preview_matches_build_ass_translated(qc_env, client, one):
    pid, fid = one
    await client.post(f"{base(pid, fid)}/events/{event_id(qc_env, pid, 0)}/hide")
    await client.post(f"{base(pid, fid)}/events", json={
        "start_ms": 0, "end_ms": 100, "translated_text": "Sign", "style": "Default"})
    r = await client.get(f"{base(pid, fid)}/preview.ass")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/x-ssa")
    expected = build(qc_env, fid, "translated").to_string("ass", header_notice=qc_routes.HEADER_NOTICE)
    assert _preview_text(r) == expected
    assert "Ahoj 0" not in expected and "Sign" in expected and "tr 1" in expected


@pytest.mark.asyncio
async def test_preview_respects_replace_incompatible_fonts(qc_env, client, one):
    pid, fid = one
    with qc_env.sync() as s:
        s.query(SubtitleStyle).update({"replacement_font_name": "Replacement Sans",
                                       "replacement_font_size": 33.0})
        s.commit()
    await options_store.aset("REPLACE_INCOMPATIBLE_FONTS", "true")
    on = (await client.get(f"{base(pid, fid)}/preview.ass")).text
    assert "Replacement Sans" in on
    await options_store.aset("REPLACE_INCOMPATIBLE_FONTS", "false")
    off = (await client.get(f"{base(pid, fid)}/preview.ass")).text
    assert "Replacement Sans" not in off and "Arial" in off


@pytest.mark.asyncio
async def test_preview_cache_headers_etag_and_freshness_after_edit(qc_env, client, one):
    pid, fid = one
    eid = event_id(qc_env, pid, 0)
    first = await client.get(f"{base(pid, fid)}/preview.ass")
    assert first.headers["cache-control"] == "no-cache, must-revalidate"
    etag = first.headers["etag"]
    assert first.headers["x-output-revision"] == str(revision(qc_env, pid))

    again = await client.get(f"{base(pid, fid)}/preview.ass", headers={"If-None-Match": etag})
    assert again.status_code == 304 and again.headers["etag"] == etag

    await client.patch(f"{base(pid, fid)}/events/{eid}", json={"translated_text": "Čerstvé"})
    stale_probe = await client.get(f"{base(pid, fid)}/preview.ass", headers={"If-None-Match": etag})
    assert stale_probe.status_code == 200 and "Čerstvé" in stale_probe.text
    assert stale_probe.headers["etag"] != etag
    listing = (await client.get(f"{base(pid, fid)}/events")).json()
    assert listing["output_revision"] == int(stale_probe.headers["x-output-revision"])


@pytest.mark.asyncio
async def test_preview_does_not_change_state(qc_env, client, one):
    pid, fid = one
    await _mark_published(qc_env, pid)
    await client.get(f"{base(pid, fid)}/preview.ass")
    await client.get(f"{base(pid, fid)}/events")
    assert (await output_of(pid)).state == "published"


# ---------------------------------------------------------------------------
# Download route + metrics keep working with manual/hidden rows
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_download_routes_follow_build_ass_semantics(qc_env, client, one):
    pid, fid = one
    await client.post(f"{base(pid, fid)}/events/{event_id(qc_env, pid, 0)}/hide")
    await client.post(f"{base(pid, fid)}/events", json={
        "start_ms": 0, "end_ms": 100, "translated_text": "Sign", "style": "Default"})
    tr = (await client.get(f"/api/projects/{pid}/files/{fid}/subtitles/translated")).text
    orig = (await client.get(f"/api/projects/{pid}/files/{fid}/subtitles/original")).text
    assert "Ahoj 0" not in tr and "Sign" in tr
    assert "Hello 0" in orig and "Sign" not in orig


def test_migration_defaults_are_false(qc_env):
    pid = seed_project(qc_env, n_files=1)
    ev = get_event(qc_env, event_id(qc_env, pid, 0))
    assert (ev.is_hidden, ev.is_manual) == (0, 0)


# ---------------------------------------------------------------------------
# Phase 2.1: immutable source timing vs editable QC timing
# ---------------------------------------------------------------------------

def _timings(ev: SubtitleEvent):
    return (ev.start_ms, ev.end_ms, ev.original_start_ms, ev.original_end_ms)


def _ass_times(subs) -> dict[str, tuple[int, int]]:
    return {e.text: (e.start, e.end) for e in subs.events}


def test_migration_backfills_original_timing_from_current():  # A
    import importlib.util
    from pathlib import Path

    import sqlalchemy as sa
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy.pool import StaticPool

    path = Path(__file__).parent.parent / "alembic" / "versions" / "d7e8f9a0b1c2_event_original_timing.py"
    spec = importlib.util.spec_from_file_location("mig_orig_timing", path)
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)

    engine = sa.create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE subtitle_events (id INTEGER PRIMARY KEY, start_ms INTEGER NOT NULL, "
            "end_ms INTEGER NOT NULL, is_hidden INTEGER NOT NULL DEFAULT 0, is_manual INTEGER NOT NULL DEFAULT 0)")
        conn.exec_driver_sql(
            "INSERT INTO subtitle_events VALUES (1, 0, 1000, 0, 0), (2, 1230, 5680, 0, 0), "
            "(3, 100, 200, 1, 0), (4, 2500, 4000, 0, 1)")
        with Operations.context(MigrationContext.configure(conn)):
            mig.upgrade()
        rows = conn.exec_driver_sql(
            "SELECT id, start_ms, end_ms, original_start_ms, original_end_ms FROM subtitle_events ORDER BY id").all()
        notnull = {r[1]: r[3] for r in conn.exec_driver_sql("PRAGMA table_info(subtitle_events)").all()}
        assert all(r[1] == r[3] and r[2] == r[4] for r in rows) and len(rows) == 4
        assert notnull["original_start_ms"] == 1 and notnull["original_end_ms"] == 1
        with Operations.context(MigrationContext.configure(conn)):
            mig.downgrade()
        cols = {r[1] for r in conn.exec_driver_sql("PRAGMA table_info(subtitle_events)").all()}
        assert "original_start_ms" not in cols and "start_ms" in cols


def test_import_rows_store_original_equal_to_current():  # B, N
    import pysubs2
    from app.jobs.handlers.extract_subtitles import _build_event_rows

    subs = pysubs2.SSAFile.from_string(
        "1\n00:00:09,009 --> 00:00:10,552\nAgain?\n\n", format_="srt")
    (row,) = _build_event_rows(subs, 7, "now")
    assert (row["start_ms"], row["end_ms"]) == (9009, 10552)
    assert (row["original_start_ms"], row["original_end_ms"]) == (9009, 10552)


@pytest.mark.asyncio
async def test_timing_edit_splits_translated_and_source_output(qc_env, client, one):  # C-G, O, P
    pid, fid = one
    eid = event_id(qc_env, pid, 0)
    await _mark_published(qc_env, pid)
    rev = revision(qc_env, pid)
    before = _timings(get_event(qc_env, eid))

    r = await client.patch(f"{base(pid, fid)}/events/{eid}", json={"start_ms": 123, "end_ms": 1504})
    assert r.status_code == 200
    body = r.json()
    assert (body["start_ms"], body["end_ms"]) == (120, 1500)
    assert "original_start_ms" not in body                            # compact DTO: current timing only
    detail = (await client.get(f"{base(pid, fid)}/events/{eid}")).json()
    assert (detail["original_start_ms"], detail["original_end_ms"]) == (before[2], before[3])
    assert (detail["start_ms"], detail["end_ms"]) == (120, 1500)

    ev = get_event(qc_env, eid)
    assert _timings(ev) == (120, 1500, before[2], before[3])          # C, D
    assert _ass_times(build(qc_env, fid, "original"))["Hello 0"] == (before[2], before[3])   # E
    assert _ass_times(build(qc_env, fid, "translated"))["Ahoj 0"] == (120, 1500)             # F

    prev = (await client.get(f"{base(pid, fid)}/preview.ass")).text                          # G
    assert "0:00:00.12,0:00:01.50" in prev
    orig_dl = (await client.get(f"/api/projects/{pid}/files/{fid}/subtitles/original")).text
    tr_dl = (await client.get(f"/api/projects/{pid}/files/{fid}/subtitles/translated")).text
    assert "0:00:00.12,0:00:01.50" in tr_dl and "0:00:00.12,0:00:01.50" not in orig_dl

    assert revision(qc_env, pid) == rev + 1                                                  # O
    assert (await output_of(pid)).state == "ready"
    assert file_row(qc_env, pid).status == "accepted"                                        # P


@pytest.mark.asyncio
async def test_publish_uses_qc_timing(qc_env, one, mux):  # H
    from tests.test_publish import publish_once
    pid, fid = one
    eid = event_id(qc_env, pid, 0)
    with qc_env.sync() as s:
        s.query(SubtitleEvent).filter_by(id=eid).update({"start_ms": 120, "end_ms": 1500})
        s.commit()
    await publish_once(qc_env, pid)
    published = (qc_env.output_root / "series" / "ep1.ass").read_text(encoding="utf-8")
    assert "0:00:00.12,0:00:01.50" in published and "Ahoj 0" in published


@pytest.mark.asyncio
async def test_text_edit_and_hide_restore_leave_timing_untouched(qc_env, client, one):  # I, J
    pid, fid = one
    eid = event_id(qc_env, pid, 0)
    before = _timings(get_event(qc_env, eid))
    await client.patch(f"{base(pid, fid)}/events/{eid}", json={"translated_text": "Jiný text"})
    assert _timings(get_event(qc_env, eid)) == before
    await client.post(f"{base(pid, fid)}/events/{eid}/hide")
    assert _timings(get_event(qc_env, eid)) == before
    await client.post(f"{base(pid, fid)}/events/{eid}/restore")
    assert _timings(get_event(qc_env, eid)) == before


@pytest.mark.asyncio
async def test_manual_event_original_equals_current_and_not_in_source(qc_env, client, one):  # K
    pid, fid = one
    r = await client.post(f"{base(pid, fid)}/events", json={
        "start_ms": 2503, "end_ms": 4000, "translated_text": "Sign", "style": "Default"})
    ev = get_event(qc_env, r.json()["id"])
    assert _timings(ev) == (2500, 4000, 2500, 4000)
    assert "Sign" not in texts(build(qc_env, fid, "original"))
    assert _ass_times(build(qc_env, fid, "translated"))["Sign"] == (2500, 4000)


@pytest.mark.asyncio
async def test_full_retranslate_resets_current_timing_to_original(qc_env, client, one):  # L, M
    pid, fid = one
    add_chunks(qc_env, fid, "complete")
    eid = event_id(qc_env, pid, 0)
    before = _timings(get_event(qc_env, eid))
    await client.patch(f"{base(pid, fid)}/events/{eid}", json={"start_ms": 120, "end_ms": 1500})
    assert get_event(qc_env, eid).start_ms == 120

    r = await client.post(f"/api/projects/{pid}/files/{fid}/retranslate")
    assert r.status_code == 200, r.text
    assert _timings(get_event(qc_env, eid)) == (before[2], before[3], before[2], before[3])


@pytest.mark.asyncio
async def test_retranslate_affected_preserves_qc_timing(qc_env, one):
    """Speaker retranslate only resets chunk status; QC timing is not touched."""
    from app.db.models import ProjectSpeaker
    pid, fid = one
    add_chunks(qc_env, fid, "complete")
    eid = event_id(qc_env, pid, 0)
    with qc_env.sync() as s:
        s.query(SubtitleEvent).filter_by(id=eid).update({"start_ms": 120, "end_ms": 1500, "name": "Tom"})
        sp = ProjectSpeaker(project_id=pid, name="Tom", created_at=_now(), updated_at=_now())
        s.add(sp)
        s.commit()
        spid = sp.id
    before_orig = get_event(qc_env, eid).original_start_ms
    await projects_routes.retranslate_affected_chunks(pid, spid)
    ev = get_event(qc_env, eid)
    assert (ev.start_ms, ev.end_ms, ev.original_start_ms) == (120, 1500, before_orig)


# ---------------------------------------------------------------------------
# Phase 5: revision header on mutations + style names in the list
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_mutations_stamp_output_revision_and_noop_keeps_it(qc_env, client, one):
    pid, fid = one
    eid = event_id(qc_env, pid, 0)
    rev = revision(qc_env, pid)

    r = await client.patch(f"{base(pid, fid)}/events/{eid}", json={"translated_text": "Nové"})
    assert int(r.headers["x-output-revision"]) == rev + 1 == revision(qc_env, pid)

    r = await client.patch(f"{base(pid, fid)}/events/{eid}", json={"translated_text": "Nové"})   # no-op
    assert int(r.headers["x-output-revision"]) == rev + 1

    r = await client.post(f"{base(pid, fid)}/events/{eid}/hide")
    assert int(r.headers["x-output-revision"]) == rev + 2
    r = await client.post(f"{base(pid, fid)}/events/{eid}/restore")
    assert int(r.headers["x-output-revision"]) == rev + 3
    r = await client.post(f"{base(pid, fid)}/events", json={
        "start_ms": 100, "end_ms": 900, "translated_text": "x", "style": "Default"})
    assert int(r.headers["x-output-revision"]) == rev + 4 == revision(qc_env, pid)


@pytest.mark.asyncio
async def test_list_exposes_file_style_names(qc_env, client, one):
    pid, fid = one
    r = (await client.get(f"{base(pid, fid)}/events")).json()
    assert "Default" in r["styles"] and len(r["styles"]) == len(set(r["styles"]))
