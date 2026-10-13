"""NEW / UPDATED review markers (`is_new`) on glossary terms and T/V address pairs."""
from __future__ import annotations

import itertools
import os
import sqlite3
import subprocess
import sys
import unittest.mock as mock
from datetime import datetime
from pathlib import Path

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import create_engine, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.db.models import File, Project, ProjectAddressPair, ProjectGlossaryTerm, SubtitleChunk
from app.jobs.handlers.style_store import insert_new_glossary_terms, upsert_address_pairs
from app.llm.schemas import AddressPairOut, GlossaryTermOut


def _now() -> str:
    return datetime.utcnow().isoformat()


# ---------------------------------------------------------------------------
# Writers (sync session, like the job handlers)
# ---------------------------------------------------------------------------

@pytest.fixture
def sync_session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as session:
        for n in ("P", "Q"):
            session.add(Project(name=n, source_directory=n, anime_provider="t",
                                anime_external_id=n, created_at=_now(), updated_at=_now()))
        session.commit()
        yield session
    engine.dispose()


def _term(source="Aria", target="Arie", **kw) -> GlossaryTermOut:
    return GlossaryTermOut(source=source, target=target, category="name", **kw)


def _glossary(session, source="Aria", project_id=1) -> ProjectGlossaryTerm:
    session.expire_all()
    return session.scalar(select(ProjectGlossaryTerm).where(
        ProjectGlossaryTerm.project_id == project_id, ProjectGlossaryTerm.source_term == source))


def _seed_term(session, *, is_new=False, locked=0, **kw) -> ProjectGlossaryTerm:
    row = ProjectGlossaryTerm(project_id=1, source_term="Aria", target_term="Arie", category="name",
                              origin="llm", locked=locked, is_active=1, is_new=is_new,
                              created_at=_now(), updated_at=_now(), **kw)
    session.add(row)
    session.commit()
    return row


def test_glossary_insert_is_new_and_column_default_is_true(sync_session):
    insert_new_glossary_terms(sync_session, 1, [_term()], "llm", _now())
    sync_session.commit()
    assert _glossary(sync_session).is_new is True
    # An INSERT that omits the column entirely also gets TRUE (DB default).
    sync_session.execute(text(
        "INSERT INTO project_glossary_terms (project_id, source_term, target_term, created_at, updated_at)"
        " VALUES (1, 'Raw', 'Raw', 'x', 'x')"))
    assert _glossary(sync_session, "Raw").is_new is True


def test_glossary_fill_missing_field_marks_updated(sync_session):
    _seed_term(sync_session)
    insert_new_glossary_terms(sync_session, 1, [_term(vocative="Arie!")], "llm", _now())
    sync_session.commit()
    row = _glossary(sync_session)
    assert row.vocative == "Arie!" and row.is_new is True


@pytest.mark.parametrize("is_new", [False, True])
def test_glossary_noop_upsert_keeps_flag(sync_session, is_new):
    _seed_term(sync_session, is_new=is_new, vocative="V", note="N", gender="f")
    insert_new_glossary_terms(
        sync_session, 1, [_term(target="Other", vocative="X", note="Y", gender="m")], "llm", _now())
    sync_session.commit()
    row = _glossary(sync_session)
    assert row.is_new is is_new
    assert (row.target_term, row.vocative, row.note, row.gender) == ("Arie", "V", "N", "f")


def test_glossary_update_does_not_clear_existing_flag(sync_session):
    _seed_term(sync_session, is_new=True, vocative="V")
    insert_new_glossary_terms(sync_session, 1, [_term(note="n")], "llm", _now())
    sync_session.commit()
    assert _glossary(sync_session).is_new is True


def test_glossary_locked_row_unchanged(sync_session):
    _seed_term(sync_session, locked=1)
    insert_new_glossary_terms(sync_session, 1, [_term(vocative="Arie!", note="n")], "llm", _now())
    sync_session.commit()
    row = _glossary(sync_session)
    assert row.is_new is False and row.vocative is None and row.note is None


def _pair_row(session) -> ProjectAddressPair:
    session.expire_all()
    return session.scalar(select(ProjectAddressPair).where(ProjectAddressPair.project_id == 1))


def _seed_pair(session, *, mode="tykani", is_new=False, locked=0, origin="llm"):
    session.add(ProjectAddressPair(project_id=1, speaker_name="A", addressee_name="B", mode=mode,
                                   origin=origin, locked=locked, is_new=is_new,
                                   created_at=_now(), updated_at=_now()))
    session.commit()


def _up(session, mode, **kw):
    n = upsert_address_pairs(session, 1, [AddressPairOut(speaker="A", addressee="B", mode=mode)],
                             "llm", _now(), **kw)
    session.commit()
    return n


def test_pair_insert_is_new(sync_session):
    assert _up(sync_session, "vykani") == 1
    assert _pair_row(sync_session).is_new is True


def test_pair_mode_change_marks_updated(sync_session):
    _seed_pair(sync_session)
    assert _up(sync_session, "vykani") == 1
    row = _pair_row(sync_session)
    assert row.mode == "vykani" and row.is_new is True


def test_pair_identical_mode_keeps_flag(sync_session):
    _seed_pair(sync_session)
    assert _up(sync_session, "tykani") == 0
    assert _pair_row(sync_session).is_new is False


def test_pair_update_existing_mode_false_prevents_change(sync_session):
    _seed_pair(sync_session)
    assert _up(sync_session, "vykani", update_existing_mode=False) == 0
    row = _pair_row(sync_session)
    assert row.mode == "tykani" and row.is_new is False


def test_pair_locked_unchanged(sync_session):
    _seed_pair(sync_session, locked=1, origin="manual")
    assert _up(sync_session, "vykani") == 0
    row = _pair_row(sync_session)
    assert row.mode == "tykani" and row.is_new is False


# ---------------------------------------------------------------------------
# Routes + Translate lifecycle (async session, like test_orchestrator)
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr("app.api.routes.projects.AsyncSessionLocal", factory)
    async with factory() as session:
        yield session
    await engine.dispose()


_dirs = itertools.count()


async def _project(db, approved=True) -> Project:
    p = Project(name="P", source_directory=f"d{next(_dirs)}", anime_provider="t", anime_external_id="1",
                status="processing", speaker_mapping_status="complete",
                context_approved_at=_now() if approved else None,
                created_at=_now(), updated_at=_now())
    db.add(p)
    await db.commit()
    return p


async def _file(db, pid, status="ready", requested=False, blocking_reason=None) -> File:
    f = File(project_id=pid, filename="a.mkv", relative_path="a.mkv", status=status,
             blocking_reason=blocking_reason,
             translation_requested_at=_now() if requested else None,
             created_at=_now(), updated_at=_now())
    db.add(f)
    await db.commit()
    return f


async def _markers(db, pid):
    db.expire_all()
    terms = (await db.scalars(
        select(ProjectGlossaryTerm).where(ProjectGlossaryTerm.project_id == pid))).all()
    pairs = (await db.scalars(
        select(ProjectAddressPair).where(ProjectAddressPair.project_id == pid))).all()
    return [t.is_new for t in terms], [p.is_new for p in pairs]


async def _seed_flagged(db, pid, name="T"):
    db.add(ProjectGlossaryTerm(project_id=pid, source_term=name, target_term="x", category="other",
                               is_new=True, created_at=_now(), updated_at=_now()))
    db.add(ProjectAddressPair(project_id=pid, speaker_name=name, addressee_name="Z", mode="tykani",
                              is_new=True, created_at=_now(), updated_at=_now()))
    await db.commit()


async def _translate(pid, fid):
    from app.api.routes.projects import translate_file
    with mock.patch("app.api.routes.projects.orchestrate_file"), \
         mock.patch("app.api.routes.projects.job_manager") as manager:
        manager.enqueue = mock.AsyncMock()
        return await translate_file(pid, fid)


@pytest.mark.asyncio
async def test_translate_resets_flags_of_that_project_only(db):
    pid, other = (await _project(db)).id, (await _project(db)).id
    await _seed_flagged(db, pid)
    await _seed_flagged(db, other)
    fid = (await _file(db, pid)).id
    await _translate(pid, fid)
    assert await _markers(db, pid) == ([False], [False])
    assert await _markers(db, other) == ([True], [True])


@pytest.mark.asyncio
async def test_rejected_translate_does_not_reset(db):
    unapproved = (await _project(db, approved=False)).id
    await _seed_flagged(db, unapproved)
    fid = (await _file(db, unapproved)).id
    with pytest.raises(HTTPException):
        await _translate(unapproved, fid)

    approved = (await _project(db)).id
    await _seed_flagged(db, approved)
    wrong_state = (await _file(db, approved, status="discovering")).id
    with pytest.raises(HTTPException):
        await _translate(approved, wrong_state)

    assert await _markers(db, unapproved) == ([True], [True])
    assert await _markers(db, approved) == ([True], [True])


@pytest.mark.asyncio
async def test_idempotent_reclick_does_not_reset(db):
    p = await _project(db)
    f = await _file(db, p.id, status="processing", requested=True)
    await _seed_flagged(db, p.id)
    await _translate(p.id, f.id)
    assert await _markers(db, p.id) == ([True], [True])


@pytest.mark.asyncio
async def test_analysis_retry_resets(db):
    p = await _project(db)
    f = await _file(db, p.id, status="waiting", requested=True, blocking_reason="analysis_failed")
    await _seed_flagged(db, p.id)
    await _translate(p.id, f.id)
    assert await _markers(db, p.id) == ([False], [False])


@pytest.mark.asyncio
async def test_writes_after_reset_are_new_and_later_writes_do_not_reset(db):
    p = await _project(db)
    pid = p.id
    await _seed_flagged(db, pid, "Old")
    f = await _file(db, pid)
    await _translate(pid, f.id)

    def analyze_script_writes(s):
        insert_new_glossary_terms(s, pid, [_term("Fresh", "Čerstvý")], "llm", _now())
        upsert_address_pairs(s, pid, [AddressPairOut(speaker="X", addressee="Y", mode="vykani")],
                             "llm", _now(), update_existing_mode=False)
        s.commit()

    def style_bible_update_writes(s):
        insert_new_glossary_terms(s, pid, [_term("Later", "L")], "llm", _now())
        s.commit()

    async def flags():
        db.expire_all()
        return {t.source_term: t.is_new for t in (await db.scalars(select(ProjectGlossaryTerm))).all()}

    await db.run_sync(analyze_script_writes)
    assert await flags() == {"Old": False, "Fresh": True}
    await db.run_sync(style_bible_update_writes)
    assert await flags() == {"Old": False, "Fresh": True, "Later": True}
    assert await _markers(db, pid) == ([False, True, True], [False, True])


@pytest.mark.asyncio
async def test_retranslate_resets(db):
    from app.api.routes.projects import retranslate_file
    p = await _project(db)
    f = await _file(db, p.id, status="accepted", requested=True)
    db.add(SubtitleChunk(file_id=f.id, chunk_index=0, translate_from_line=0, translate_to_line=0,
                         status="complete", created_at=_now(), updated_at=_now()))
    await _seed_flagged(db, p.id)
    with mock.patch("app.api.routes.projects.orchestrate_file"), \
         mock.patch("app.api.routes.projects.job_manager"):
        await retranslate_file(p.id, f.id)
    assert await _markers(db, p.id) == ([False], [False])


@pytest.mark.asyncio
async def test_api_manual_create_edit_and_read(db):
    from app.api.routes import projects as routes
    p = await _project(db)
    created = await routes.create_glossary_term(
        p.id, routes.GlossaryTermCreateIn(source_term="Man", target_term="Muž"))
    assert created.is_new is False

    await _seed_flagged(db, p.id, "Auto")
    term = (await db.scalars(select(ProjectGlossaryTerm).where(
        ProjectGlossaryTerm.source_term == "Auto"))).one()
    pair = (await db.scalars(select(ProjectAddressPair))).one()

    out = await routes.update_glossary_term(p.id, term.id, routes.GlossaryTermUpdateIn(note="n"))
    assert out.is_new is True and out.locked      # manual edit preserves TRUE
    out = await routes.update_glossary_term(p.id, created.id, routes.GlossaryTermUpdateIn(note="n"))
    assert out.is_new is False                            # ...and FALSE
    pair_out = await routes.update_address_pair(p.id, pair.id, routes.AddressPairUpdateIn(mode="vykani"))
    assert pair_out.is_new is True and pair_out.mode == "vykani"

    listed = await routes.list_glossary_terms(p.id)
    assert {t.source_term: t.is_new for t in listed} == {"Man": False, "Auto": True}
    guide = await routes.get_style_guide(p.id)
    assert [x.is_new for x in guide.address_pairs] == [True]

    # Public write payloads cannot carry is_new.
    for schema in (routes.GlossaryTermCreateIn, routes.GlossaryTermUpdateIn, routes.AddressPairUpdateIn):
        assert "is_new" not in schema.model_fields


# ---------------------------------------------------------------------------
# Migration (alembic in a subprocess against a scratch DB)
# ---------------------------------------------------------------------------

def test_migration_backfills_false_and_defaults_true(tmp_path):
    backend = Path(__file__).resolve().parents[1]
    scratch = tmp_path / "cfg"
    scratch.mkdir()
    env = dict(os.environ, CONFIG_ROOT=str(scratch),
               IMPORT_ROOT=str(tmp_path / "i"), OUTPUT_ROOT=str(tmp_path / "o"))

    def alembic(*args):
        r = subprocess.run([sys.executable, "-m", "alembic", *args], cwd=backend, env=env,
                           capture_output=True, text=True)
        assert r.returncode == 0, r.stdout + r.stderr
        return r.stdout

    alembic("upgrade", "a4b5c6d7e8f9")
    db_path = scratch / "subi-neko.db"
    con = sqlite3.connect(db_path)
    con.execute("INSERT INTO projects (id,name,source_directory,anime_provider,anime_external_id,status,"
                "is_paused,speaker_mapping_status,created_at,updated_at)"
                " VALUES (1,'P','d','t','1','new',0,'complete','x','x')")
    con.execute("INSERT INTO project_glossary_terms (project_id,source_term,target_term,created_at,updated_at)"
                " VALUES (1,'Old','Old','x','x')")
    con.execute("INSERT INTO project_address_pairs (project_id,speaker_name,addressee_name,mode,"
                "created_at,updated_at) VALUES (1,'A','B','tykani','x','x')")
    con.commit()
    con.close()

    alembic("upgrade", "head")
    assert "No new upgrade operations" in alembic("check")
    con = sqlite3.connect(db_path)
    assert con.execute("SELECT is_new FROM project_glossary_terms").fetchall() == [(0,)]
    assert con.execute("SELECT is_new FROM project_address_pairs").fetchall() == [(0,)]
    con.execute("INSERT INTO project_glossary_terms (project_id,source_term,target_term,created_at,updated_at)"
                " VALUES (1,'New','New','x','x')")
    con.execute("INSERT INTO project_address_pairs (project_id,speaker_name,addressee_name,mode,"
                "created_at,updated_at) VALUES (1,'C','D','tykani','x','x')")
    con.commit()
    assert con.execute("SELECT is_new FROM project_glossary_terms WHERE source_term='New'").fetchall() == [(1,)]
    assert con.execute("SELECT is_new FROM project_address_pairs WHERE speaker_name='C'").fetchall() == [(1,)]
    con.close()

    alembic("downgrade", "-1")
    alembic("upgrade", "head")
