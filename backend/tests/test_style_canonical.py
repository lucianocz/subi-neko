from __future__ import annotations

import importlib.util
from datetime import datetime
from pathlib import Path

import pysubs2
import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base
from app.db.models import (
    File,
    Project,
    Subtitle,
    SubtitleEvent,
    SubtitleStyle,
    file_subtitle_styles,
)
from app.subs.ass_rendering import build_ass
from app.subs.style_canonical import (
    compute_source_style_hash,
    find_or_create_style,
    link_file_styles,
    prune_orphan_styles,
)


def _now() -> str:
    return datetime.utcnow().isoformat()


@pytest.fixture
def session_factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)

    @event.listens_for(engine, "connect")
    def _fk(dbapi_conn, _rec):
        dbapi_conn.execute("PRAGMA foreign_keys = ON")

    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


def _style(name="Default", **over) -> dict:
    row = dict(
        style_name=name, font_name="Arial", font_size=42.0,
        primary_colour="&H00FFFFFF&", secondary_colour="&H000000FF&",
        outline_colour="&H00000000&", back_colour="&H00000000&",
        bold=0, italic=0, underline=0, strikeout=0,
        scale_x=100.0, scale_y=100.0, spacing=0.0, angle=0.0,
        border_style=1, outline=2.0, shadow=1.0, alignment=2,
        margin_l=10, margin_r=10, margin_v=20, encoding=1,
        created_at=_now(), updated_at=_now(),
    )
    row.update(over)
    return row


def _project(session, name="P") -> int:
    p = Project(name=name, source_directory=name, anime_provider="anidb",
                anime_external_id="1", created_at=_now(), updated_at=_now())
    session.add(p)
    session.flush()
    return p.id


def _file(session, project_id, name="e1.mkv") -> int:
    f = File(project_id=project_id, filename=name, relative_path=name,
             created_at=_now(), updated_at=_now())
    session.add(f)
    session.flush()
    return f.id


def _style_count(session, project_id=None) -> int:
    q = select(sa.func.count()).select_from(SubtitleStyle)
    if project_id is not None:
        q = q.where(SubtitleStyle.project_id == project_id)
    return session.scalar(q)


# --- A-D: sharing semantics ----------------------------------------------

def test_same_style_in_two_files_reuses_one_canonical_record(session_factory):  # A
    with session_factory() as s:
        pid = _project(s)
        f1, f2 = _file(s, pid, "e1.mkv"), _file(s, pid, "e2.mkv")
        ids1 = link_file_styles(s, f1, pid, [_style()])
        ids2 = link_file_styles(s, f2, pid, [_style()])
        s.commit()
        assert ids1 == ids2
        assert _style_count(s) == 1
        assert s.scalar(select(sa.func.count()).select_from(file_subtitle_styles)) == 2


def test_different_style_name_is_a_different_style(session_factory):  # B
    with session_factory() as s:
        pid = _project(s)
        a = find_or_create_style(s, pid, _style("Default"))
        b = find_or_create_style(s, pid, _style("Dialogue"))
        assert a != b


def test_same_name_different_properties_is_a_different_style(session_factory):  # C
    with session_factory() as s:
        pid = _project(s)
        a = find_or_create_style(s, pid, _style("Default", font_size=42.0))
        b = find_or_create_style(s, pid, _style("Default", font_size=40.0))
        c = find_or_create_style(s, pid, _style("Default", primary_colour="&H0000FFFF&"))
        assert len({a, b, c}) == 3


def test_identical_styles_in_different_projects_are_not_shared(session_factory):  # D
    with session_factory() as s:
        p1, p2 = _project(s, "A"), _project(s, "B")
        a = find_or_create_style(s, p1, _style())
        b = find_or_create_style(s, p2, _style())
        assert a != b
        assert s.get(SubtitleStyle, a).source_style_hash == s.get(SubtitleStyle, b).source_style_hash


# --- E-G: hash contract ---------------------------------------------------

def test_hash_ignores_replacement_and_derived_fields():  # E
    base = _style()
    with_repl = {**base, "replacement_font_name": "Noto Sans", "replacement_font_size": 30.0,
                 "font_check_status": "replaced", "project_id": 99, "id": 5}
    assert compute_source_style_hash(base) == compute_source_style_hash(with_repl)
    assert compute_source_style_hash(base) != compute_source_style_hash({**base, "style_name": "X"})


def test_float_normalization_is_stable():
    a = compute_source_style_hash(_style(spacing=0.0))
    assert a == compute_source_style_hash(_style(spacing=-0.0))
    assert a == compute_source_style_hash(_style(spacing=0))


def test_editing_replacement_never_changes_hash_and_later_import_reuses(session_factory):  # F, G
    with session_factory() as s:
        pid = _project(s)
        f1, f2 = _file(s, pid, "e1.mkv"), _file(s, pid, "e2.mkv")
        (sid,) = link_file_styles(s, f1, pid, [_style()])
        s.commit()
        before = s.get(SubtitleStyle, sid).source_style_hash

        style = s.get(SubtitleStyle, sid)
        style.replacement_font_name = "Noto Sans"
        style.replacement_font_size = 38.0
        s.commit()
        assert s.get(SubtitleStyle, sid).source_style_hash == before  # F

        (sid2,) = link_file_styles(s, f2, pid, [_style()])  # G
        s.commit()
        assert sid2 == sid
        reused = s.get(SubtitleStyle, sid2)
        assert (reused.replacement_font_name, reused.replacement_font_size) == ("Noto Sans", 38.0)
        assert _style_count(s) == 1


# --- H-I: export ------------------------------------------------------------

def _build(text_variant, **style_over):
    row = {k: v for k, v in _style(**style_over).items() if k not in ("created_at", "updated_at")}
    style = SubtitleStyle(project_id=1, source_style_hash="h", **row)
    subtitle = Subtitle(file_id=1)
    ev = SubtitleEvent(file_id=1, line_index=0, event_type="dialogue", layer=0, start_ms=0,
                       end_ms=1000, style="Default", source_text="Hi", translated_text="Ahoj")
    return build_ass(subtitle, [style], [ev], text_variant=text_variant).styles["Default"]


def test_source_ass_uses_original_font_even_with_replacement():  # H
    st = _build("original", replacement_font_name="Noto Sans", replacement_font_size=30.0)
    assert (st.fontname, st.fontsize) == ("Arial", 42.0)


def test_translated_ass_uses_replacement_with_fallback():  # I
    st = _build("translated", replacement_font_name="Noto Sans", replacement_font_size=30.0)
    assert (st.fontname, st.fontsize) == ("Noto Sans", 30.0)
    st = _build("translated")
    assert (st.fontname, st.fontsize) == ("Arial", 42.0)
    st = _build("translated", replacement_font_name="Noto Sans")  # size falls back independently
    assert (st.fontname, st.fontsize) == ("Noto Sans", 42.0)
    st = _build("translated", replacement_font_size=30.0)
    assert (st.fontname, st.fontsize) == ("Arial", 30.0)


# --- REPLACE_INCOMPATIBLE_FONTS option ----------------------------------------

def _opts(**d):
    from app.db.options import AppOptions
    return AppOptions.from_dict(d)


def _repl_style(**over) -> SubtitleStyle:
    row = {k: v for k, v in _style(**over).items() if k not in ("created_at", "updated_at")}
    return SubtitleStyle(project_id=1, source_style_hash="h", **row)


def _render(style, variant, enabled):
    subtitle = Subtitle(file_id=1)
    ev = SubtitleEvent(file_id=1, line_index=0, event_type="dialogue", layer=0, start_ms=0,
                       end_ms=1000, style="Default", source_text="Hi", translated_text="Ahoj")
    st = build_ass(subtitle, [style], [ev], text_variant=variant,
                   use_font_replacements=enabled).styles["Default"]
    return st.fontname, st.fontsize


def test_option_defaults_to_enabled_when_missing():  # A
    assert _opts().replace_incompatible_fonts is True
    assert _opts(REPLACE_INCOMPATIBLE_FONTS="0").replace_incompatible_fonts is False
    assert _opts(REPLACE_INCOMPATIBLE_FONTS="1").replace_incompatible_fonts is True
    # build_ass itself defaults to the legacy (enabled) behavior
    style = _repl_style(replacement_font_name="Noto Sans", replacement_font_size=30.0)
    ev = SubtitleEvent(file_id=1, line_index=0, event_type="dialogue", layer=0, start_ms=0,
                       end_ms=1000, style="Default", source_text="Hi")
    st = build_ass(Subtitle(file_id=1), [style], [ev], text_variant="translated").styles["Default"]
    assert (st.fontname, st.fontsize) == ("Noto Sans", 30.0)


def test_enabled_uses_replacement_name_and_size():  # B
    style = _repl_style(replacement_font_name="Noto Sans", replacement_font_size=30.0)
    assert _render(style, "translated", True) == ("Noto Sans", 30.0)


def test_enabled_fallback_is_independent_per_field():  # C
    assert _render(_repl_style(replacement_font_name="Noto Sans"), "translated", True) == ("Noto Sans", 42.0)
    assert _render(_repl_style(replacement_font_size=30.0), "translated", True) == ("Arial", 30.0)


def test_disabled_uses_source_font():  # D
    style = _repl_style(replacement_font_name="Noto Sans", replacement_font_size=30.0)
    assert _render(style, "translated", False) == ("Arial", 42.0)


def test_disabling_keeps_replacements_and_reenabling_restores_them():  # E, F
    style = _repl_style(replacement_font_name="Noto Sans", replacement_font_size=30.0)
    assert _render(style, "translated", False) == ("Arial", 42.0)
    assert (style.replacement_font_name, style.replacement_font_size) == ("Noto Sans", 30.0)  # E
    assert _render(style, "translated", True) == ("Noto Sans", 30.0)  # F


@pytest.mark.parametrize("enabled", [True, False])
def test_source_ass_ignores_option(enabled):  # G
    style = _repl_style(replacement_font_name="Noto Sans", replacement_font_size=30.0)
    assert _render(style, "original", enabled) == ("Arial", 42.0)


def test_both_translated_paths_pass_the_option():
    """Download route and render job must both forward the option to build_ass."""
    import inspect
    from app.api.routes import projects
    from app.jobs.handlers import render_output_ass
    for mod in (projects, render_output_ass):
        assert "use_font_replacements=" in inspect.getsource(mod)


# --- J: event -> style references -------------------------------------------

def test_event_style_references_are_unchanged_by_sharing(session_factory):  # J
    with session_factory() as s:
        pid = _project(s)
        f1, f2 = _file(s, pid, "e1.mkv"), _file(s, pid, "e2.mkv")
        # f2 defines Default identically but also a Sign style f1 lacks.
        link_file_styles(s, f1, pid, [_style("Default")])
        link_file_styles(s, f2, pid, [_style("Default"), _style("Sign", font_size=30.0)])
        for fid, names in ((f1, ["Default", "Default"]), (f2, ["Default", "Sign"])):
            subtitle = Subtitle(file_id=fid, created_at=_now(), updated_at=_now())
            s.add(subtitle)
            for i, name in enumerate(names):
                s.add(SubtitleEvent(file_id=fid, line_index=i, event_type="dialogue", layer=0,
                                    start_ms=i * 1000, end_ms=i * 1000 + 900, style=name,
                                    source_text="x", created_at=_now(), updated_at=_now()))
        s.commit()

        for fid, expected in ((f1, ["Default", "Default"]), (f2, ["Default", "Sign"])):
            styles = s.scalars(
                select(SubtitleStyle)
                .join(file_subtitle_styles, file_subtitle_styles.c.subtitle_style_id == SubtitleStyle.id)
                .where(file_subtitle_styles.c.file_id == fid).order_by(SubtitleStyle.id)
            ).all()
            events = s.scalars(select(SubtitleEvent).where(SubtitleEvent.file_id == fid)
                               .order_by(SubtitleEvent.line_index)).all()
            subs = build_ass(s.scalar(select(Subtitle).where(Subtitle.file_id == fid)),
                             styles, events, text_variant="original")
            assert [e.style for e in subs] == expected
            assert all(e.style in subs.styles for e in subs)
        assert _style_count(s) == 2


# --- K: lifecycle -------------------------------------------------------------

def test_deleting_one_file_keeps_shared_style(session_factory):  # K
    with session_factory() as s:
        pid = _project(s)
        f1, f2 = _file(s, pid, "e1.mkv"), _file(s, pid, "e2.mkv")
        (sid,) = link_file_styles(s, f1, pid, [_style()])
        link_file_styles(s, f2, pid, [_style()])
        s.commit()

        s.delete(s.get(File, f1))
        s.commit()
        assert s.get(SubtitleStyle, sid) is not None
        links = s.execute(select(file_subtitle_styles)).all()
        assert [(r.file_id, r.subtitle_style_id) for r in links] == [(f2, sid)]


def test_project_delete_removes_its_styles(session_factory):
    with session_factory() as s:
        pid = _project(s)
        link_file_styles(s, _file(s, pid), pid, [_style()])
        s.commit()
        s.delete(s.get(Project, pid))
        s.commit()
        assert _style_count(s) == 0
        assert s.scalar(select(sa.func.count()).select_from(file_subtitle_styles)) == 0


def test_reextraction_relinks_then_prunes_orphans_keeping_edits(session_factory):
    with session_factory() as s:
        pid = _project(s)
        f1 = _file(s, pid)
        (keep,) = link_file_styles(s, f1, pid, [_style("Default"), _style("Old")])[:1]
        s.get(SubtitleStyle, keep).replacement_font_name = "Noto Sans"
        s.commit()

        # Re-extraction: Default unchanged, "Old" gone.
        link_file_styles(s, f1, pid, [_style("Default")])
        assert prune_orphan_styles(s, pid) == 1
        s.commit()
        assert [x.style_name for x in s.scalars(select(SubtitleStyle)).all()] == ["Default"]
        assert s.get(SubtitleStyle, keep).replacement_font_name == "Noto Sans"


def test_prune_does_not_touch_other_projects(session_factory):
    with session_factory() as s:
        p1, p2 = _project(s, "A"), _project(s, "B")
        find_or_create_style(s, p2, _style())  # unreferenced, but another project's
        link_file_styles(s, _file(s, p1), p1, [_style()])
        assert prune_orphan_styles(s, p1) == 0
        assert _style_count(s, p2) == 1


# --- L: migration ---------------------------------------------------------------

_MIGRATION = Path(__file__).parent.parent / "alembic" / "versions" / "e7f8a9b0c1d2_project_level_styles.py"


def _load_migration():
    spec = importlib.util.spec_from_file_location("mig_styles", _MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _legacy_db():
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    return engine


_LEGACY_COLS = ("style_name", "font_name", "font_size", "primary_colour", "bold", "scale_x",
                "replacement_font_name", "replacement_font_size", "font_check_status")


def test_migration_preserves_data_dedupes_and_matches_runtime_hash():  # L
    mig = _load_migration()
    engine = _legacy_db()
    with engine.begin() as conn:
        conn.exec_driver_sql("PRAGMA foreign_keys = ON")
        conn.exec_driver_sql("CREATE TABLE projects (id INTEGER PRIMARY KEY)")
        conn.exec_driver_sql("CREATE TABLE files (id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL "
                             "REFERENCES projects(id) ON DELETE CASCADE)")
        conn.exec_driver_sql(
            "CREATE TABLE subtitle_styles (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE, style_name TEXT NOT NULL, "
            "font_name TEXT NOT NULL, font_size FLOAT NOT NULL, primary_colour TEXT, secondary_colour TEXT, "
            "outline_colour TEXT, back_colour TEXT, bold INTEGER, italic INTEGER, underline INTEGER, "
            "strikeout INTEGER, scale_x FLOAT, scale_y FLOAT, spacing FLOAT, angle FLOAT, border_style INTEGER, "
            "outline FLOAT, shadow FLOAT, alignment INTEGER, margin_l INTEGER, margin_r INTEGER, "
            "margin_v INTEGER, encoding INTEGER, replacement_font_name TEXT, replacement_font_size FLOAT, "
            "font_check_status TEXT NOT NULL DEFAULT 'unchecked', "
            "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
            "UNIQUE(file_id, style_name))"
        )
        conn.exec_driver_sql("INSERT INTO projects VALUES (1), (2)")
        conn.exec_driver_sql("INSERT INTO files VALUES (10, 1), (11, 1), (20, 2)")
        ins = ("INSERT INTO subtitle_styles (file_id, style_name, font_name, font_size, primary_colour, bold, "
               "scale_x, replacement_font_name, replacement_font_size, font_check_status) VALUES (?,?,?,?,?,?,?,?,?,?)")
        rows = [
            (10, "Default", "Arial", 42.0, "&H00FFFFFF&", 0, 100.0, None, None, "unchecked"),  # id1 keeper
            (10, "Sign", "Impact", 30.0, "&H00FFFFFF&", 1, 100.0, "Arial", 25.0, "replaced"),  # id2
            (11, "Default", "Arial", 42.0, "&H00FFFFFF&", 0, 100.0, "Noto Sans", 38.0, "replaced"),  # id3 dup of 1
            (11, "Default2", "Arial", 40.0, "&H00FFFFFF&", 0, 100.0, None, None, "supported"),  # id4
            (20, "Default", "Arial", 42.0, "&H00FFFFFF&", 0, 100.0, None, None, "supported"),  # id5: other project
        ]
        for r in rows:
            conn.exec_driver_sql(ins, r)

        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            mig.upgrade()

        styles = conn.exec_driver_sql(
            "SELECT id, project_id, style_name, source_style_hash, replacement_font_name, "
            "replacement_font_size, font_check_status FROM subtitle_styles ORDER BY id").all()
        links = conn.exec_driver_sql(
            "SELECT file_id, subtitle_style_id FROM file_subtitle_styles ORDER BY 1, 2").all()
        indexes = {r[1] for r in conn.exec_driver_sql("PRAGMA index_list(subtitle_styles)").all()}
        cols = {r[1] for r in conn.exec_driver_sql("PRAGMA table_info(subtitle_styles)").all()}

    assert "file_id" not in cols and {"project_id", "source_style_hash"} <= cols
    assert "idx_subtitle_styles_project_font_check_status" in indexes
    # id3 merged into id1 (adopting its checked replacement); other projects are never merged.
    assert [s[0] for s in styles] == [1, 2, 4, 5]
    merged = styles[0]
    assert (merged[4], merged[5], merged[6]) == ("Noto Sans", 38.0, "replaced")
    assert (styles[1][4], styles[1][5]) == ("Arial", 25.0)
    # Every legacy file<->style association survives.
    assert links == [(10, 1), (10, 2), (11, 1), (11, 4), (20, 5)]
    # Migration hash == runtime hash for the same definition.
    runtime = compute_source_style_hash(dict(
        style_name="Default", font_name="Arial", font_size=42.0, primary_colour="&H00FFFFFF&",
        bold=0, scale_x=100.0))
    assert merged[3] == runtime
    assert len({s[3] for s in styles}) == 3  # ids 1 and 5 share a hash but live in different projects

    # Round-trip back out of the new schema keeps one row per (file, style).
    with engine.begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            mig.downgrade()
        assert conn.exec_driver_sql("SELECT count(*) FROM subtitle_styles").scalar() == 5
    engine.dispose()
