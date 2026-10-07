"""Per-file branding overlay: template merge, safe resolution, render
integration (QC preview / download / Publish), config API, output revision,
font manifest, metrics isolation and FK cascade."""
from __future__ import annotations

import pysubs2
import pytest
import pytest_asyncio
from fastapi import FastAPI
import httpx
from sqlalchemy import create_engine, event as sa_event, func, select
from sqlalchemy.orm import sessionmaker

from app.api.routes import media as media_routes
from app.api.routes import projects as projects_routes
from app.api.routes import qc as qc_routes
from app.core.config import settings
from app.core.database import Base
from app.db.models import (
    File, FileBranding, Project, QaItem, Subtitle, SubtitleChunk, SubtitleEvent, SubtitleStyle,
)
from app.subs import branding as br
from app.subs.ass_rendering import build_ass
from app.subs.branding import BrandingError, BrandingSpec
from app.subs.qc_fonts import collect_required_families
from tests.test_publish import (  # noqa: F401
    env, event_id, mux, output_of, publish_once, seed_project, _now,
)
from tests.test_qc import add_chunks, add_event, build, file_row, revision, base, _mark_published
from tests.test_qc_fonts import add_config_font, all_families, client, fenv, fonts_url, set_style  # noqa: F401

TEMPLATE = """[Script Info]
Title: brand
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
WrapStyle: 2

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: SubiNeko,Trebuchet MS,70,&H009F7FF8,&H00FFFFFF,&H002D0897,&HFF000000,0,0,0,0,100,100,0,0,1,1.6,0,1,300,0,50,1
Style: Other,Brand Sans,40,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,1,0,2,10,10,10,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:00.00,0:00:05.00,SubiNeko,logo,0,0,0,,{\\an7\\pos(24,835)\\p4}m 0 0 l 100 0 100 100{\\p0}
Dialogue: 2,0:00:01.20,0:00:04.50,Other,,5,6,7,fx,{\\fnInline Brand\\1c&H00FF00&}Hi{\\rSubiNeko}x
Dialogue: 1,0:00:03.00,0:00:06.00,SubiNeko,,0,0,0,,plain
"""


@pytest.fixture
def brand_dir(fenv):
    d = settings.brand_dir
    d.mkdir(parents=True, exist_ok=True)
    (d / "final.ass").write_text(TEMPLATE, encoding="utf-8")
    return d


def tpl_spec(offset=0, name="final.ass"):
    return BrandingSpec(name, offset)


def _plain_ass(brand=None, variant="translated"):
    subtitle = Subtitle(script_type="v4.00+", play_res_x=1920, play_res_y=1080)
    style = SubtitleStyle(style_name="Default", font_name="Arial", font_size=20.0)
    ev = SubtitleEvent(
        line_index=0, event_type="dialogue", layer=0, start_ms=100, end_ms=900,
        original_start_ms=100, original_end_ms=900, style="Default",
        source_text="src", translated_text="tr")
    return build_ass(subtitle, [style], [ev], text_variant=variant, branding=brand)


# --- template / timing / namespace --------------------------------------------

def test_offset_shifts_every_event_additively(brand_dir):
    subs = _plain_ass(tpl_spec(1_200_000))
    brand = subs.events[1:]
    assert [(e.start, e.end) for e in brand] == [
        (1_200_000, 1_205_000), (1_201_200, 1_204_500), (1_203_000, 1_206_000)]  # A, B (non-zero start), C
    assert (subs.events[0].start, subs.events[0].end) == (100, 900)  # main untouched


def test_zero_offset_keeps_template_timing_and_order(brand_dir):
    subs = _plain_ass(tpl_spec(0))
    brand = subs.events[1:]
    assert [(e.layer, e.start, e.end) for e in brand] == [(0, 0, 5000), (2, 1200, 4500), (1, 3000, 6000)]
    assert [e.text for e in brand][2] == "plain"


def test_style_namespace_and_properties_preserved(brand_dir):
    subs = _plain_ass(tpl_spec(10))
    orig = br.load_template("final.ass")
    assert set(subs.styles) == {"Default", "__brand__SubiNeko", "__brand__Other"}
    for name, style in orig.styles.items():
        assert subs.styles["__brand__" + name].as_dict() == style.as_dict() if hasattr(style, "as_dict") \
            else vars(subs.styles["__brand__" + name]) == vars(style)
    assert [e.style for e in subs.events[1:]] == ["__brand__SubiNeko", "__brand__Other", "__brand__SubiNeko"]
    assert subs.events[0].style == "Default"
    assert "SubiNeko" in orig.styles and not any(n.startswith("__brand__") for n in orig.styles)


def test_raw_tags_drawings_and_event_fields_preserved(brand_dir):
    subs = _plain_ass(tpl_spec(0))
    logo, other = subs.events[1], subs.events[2]
    assert logo.text == r"{\an7\pos(24,835)\p4}m 0 0 l 100 0 100 100{\p0}"
    assert logo.name == "logo"
    assert (other.marginl, other.marginr, other.marginv, other.effect) == (5, 6, 7, "fx")
    # \r<style> resets are namespaced with the style they reference
    assert other.text == r"{\fnInline Brand\1c&H00FF00&}Hi{\r__brand__SubiNeko}x"


def test_script_info_stays_with_main_script(brand_dir):
    subs = _plain_ass(tpl_spec(0))
    # template WrapStyle/Title are NOT copied over the main script's info
    assert subs.info["PlayResX"] == "1920" and "WrapStyle" not in subs.info
    assert subs.info.get("Title") != "brand"


def test_template_file_not_mutated(brand_dir):
    before = (brand_dir / "final.ass").read_bytes()
    _plain_ass(tpl_spec(5000))
    _plain_ass(tpl_spec(9000))
    assert (brand_dir / "final.ass").read_bytes() == before
    assert br.load_template("final.ass").events[0].start == 0     # cache not polluted


def test_cache_detects_template_change(brand_dir):
    assert len(br.load_template("final.ass").events) == 3
    (brand_dir / "final.ass").write_text(
        TEMPLATE + "Dialogue: 0,0:00:07.00,0:00:08.00,SubiNeko,,0,0,0,,more\n", encoding="utf-8")
    assert len(br.load_template("final.ass").events) == 4


def test_source_variant_never_gets_branding(brand_dir):
    subs = _plain_ass(tpl_spec(0), variant="original")
    assert len(subs.events) == 1 and set(subs.styles) == {"Default"}


def test_disabled_or_absent_branding_changes_nothing(brand_dir):
    plain = _plain_ass(None).to_string("ass")
    assert br.spec_from_row(None) is None
    assert br.spec_from_row(FileBranding(enabled=0, template_filename="final.ass", start_offset_ms=5)) is None
    assert _plain_ass(br.spec_from_row(FileBranding(enabled=0, template_filename="final.ass"))
                      ).to_string("ass") == plain
    assert _plain_ass(tpl_spec(0)).to_string("ass") != plain


# --- discovery / safe resolution ---------------------------------------------

def test_discovery_sorted_filenames_only(brand_dir):
    (brand_dir / "b.ass").write_text(TEMPLATE, encoding="utf-8")
    (brand_dir / "a.ASS").write_text(TEMPLATE, encoding="utf-8")
    (brand_dir / "notes.txt").write_text("x")
    (brand_dir / "sub").mkdir()
    (brand_dir / "sub" / "deep.ass").write_text(TEMPLATE, encoding="utf-8")
    assert br.list_templates() == ["a.ASS", "b.ass", "final.ass"]


@pytest.mark.parametrize("name", [
    "../final.ass", "..\\final.ass", "sub/final.ass", "/etc/passwd.ass", "C:\\x.ass",
    "final.txt", "", "..", "missing.ass", "final.ass\0"])
def test_unsafe_or_missing_templates_rejected(brand_dir, name):
    with pytest.raises(BrandingError):
        br.resolve_template(name)


def test_symlink_escape_rejected(brand_dir, tmp_path):
    outside = tmp_path / "outside.ass"
    outside.write_text(TEMPLATE, encoding="utf-8")
    try:
        (brand_dir / "link.ass").symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    assert "link.ass" not in br.list_templates()
    with pytest.raises(BrandingError):
        br.resolve_template("link.ass")


# --- API ------------------------------------------------------------------------

def burl(env) -> str:
    return f"/api/projects/{env.pid}/files/{env.fid}/qc/branding"


def put(client, env, **body):
    body.setdefault("enabled", True)
    body.setdefault("template_filename", "final.ass")
    body.setdefault("start_offset_ms", 0)
    return client.put(burl(env), json=body)


def db_rows(env):
    with env.sync() as s:
        return list(s.scalars(select(FileBranding)).all())


@pytest.mark.asyncio
async def test_get_defaults_without_creating_row(client, fenv, brand_dir):
    r = await client.get(burl(fenv))
    assert r.status_code == 200
    body = r.json()
    assert (body["enabled"], body["template_filename"], body["start_offset_ms"]) == (False, "final.ass", 0)
    (brand_dir / "second.ass").write_text(TEMPLATE, encoding="utf-8")
    assert (await client.get(burl(fenv))).json()["template_filename"] is None   # not exactly one
    assert db_rows(fenv) == []


@pytest.mark.asyncio
async def test_templates_endpoint(client, fenv, brand_dir):
    r = await client.get(burl(fenv) + "/templates")
    assert r.json() == {"templates": ["final.ass"]}


@pytest.mark.asyncio
async def test_save_update_and_disable_keeps_config(client, fenv, brand_dir):
    (brand_dir / "second.ass").write_text(TEMPLATE, encoding="utf-8")
    r = await put(client, fenv, start_offset_ms=1_230_005)
    assert r.status_code == 200
    assert r.json()["start_offset_ms"] == 1_230_010          # quantized like QC timing
    assert (await put(client, fenv, template_filename="second.ass", start_offset_ms=1_230_010)
            ).json()["template_filename"] == "second.ass"
    off = (await put(client, fenv, enabled=False, template_filename="second.ass",
                     start_offset_ms=1_230_010)).json()
    assert off["enabled"] is False and off["template_filename"] == "second.ass" and off["start_offset_ms"] == 1_230_010
    assert (await client.get(burl(fenv))).json() == off
    assert len(db_rows(fenv)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [
    {"start_offset_ms": -1},
    {"template_filename": "nope.ass"},
    {"template_filename": "../final.ass"},
    {"template_filename": "/abs/final.ass"},
    {"template_filename": None},
    {"enabled": False, "template_filename": "../final.ass"},
])
async def test_invalid_saves_rejected_and_write_nothing(client, fenv, brand_dir, body):
    before = revision(fenv, fenv.pid)
    assert (await put(client, fenv, **body)).status_code == 422
    assert db_rows(fenv) == [] and revision(fenv, fenv.pid) == before


@pytest.mark.asyncio
async def test_unparseable_template_rejected(client, fenv, brand_dir, monkeypatch):
    monkeypatch.setattr(qc_routes, "load_template",
                        lambda n: (_ for _ in ()).throw(BrandingError("bad")))
    assert (await put(client, fenv)).status_code == 422


@pytest.mark.asyncio
async def test_output_revision_semantics_and_file_stays_accepted(client, fenv, brand_dir):
    await _mark_published(fenv, fenv.pid)
    r0 = revision(fenv, fenv.pid)

    r = await put(client, fenv)                                   # enable
    assert revision(fenv, fenv.pid) == r0 + 1 and r.json()["output_revision"] == r0 + 1
    assert r.headers["x-output-revision"] == str(r0 + 1)
    assert (await output_of(fenv.pid)).state == "ready"
    assert file_row(fenv, fenv.pid).status == "accepted"

    await put(client, fenv)                                       # no-op: nothing bumps
    assert revision(fenv, fenv.pid) == r0 + 1

    await put(client, fenv, start_offset_ms=500)                  # start change
    assert revision(fenv, fenv.pid) == r0 + 2
    (brand_dir / "second.ass").write_text(TEMPLATE, encoding="utf-8")
    await put(client, fenv, template_filename="second.ass", start_offset_ms=500)   # template change
    assert revision(fenv, fenv.pid) == r0 + 3
    await put(client, fenv, enabled=False, template_filename="second.ass", start_offset_ms=500)
    assert revision(fenv, fenv.pid) == r0 + 4                     # enabled change
    assert file_row(fenv, fenv.pid).status == "accepted"
    # GET never bumps
    await client.get(burl(fenv))
    assert revision(fenv, fenv.pid) == r0 + 4


@pytest.mark.asyncio
async def test_noop_default_save_creates_no_row(client, fenv, brand_dir):
    r0 = revision(fenv, fenv.pid)
    await put(client, fenv, enabled=False, start_offset_ms=0)     # == defaults (single template)
    assert db_rows(fenv) == [] and revision(fenv, fenv.pid) == r0


@pytest.mark.asyncio
async def test_qc_gate_and_file_scoping(client, fenv, brand_dir):
    assert (await client.get(f"/api/projects/{fenv.pid + 9}/files/{fenv.fid}/qc/branding")).status_code == 404
    with fenv.sync() as s:
        s.execute(SubtitleChunk.__table__.update().values(status="pending"))
        s.commit()
    assert (await client.get(burl(fenv))).status_code == 409
    assert (await put(client, fenv)).status_code == 409


# --- render integration -----------------------------------------------------------

async def _enable(client, env, start=60_000):
    assert (await put(client, env, start_offset_ms=start)).status_code == 200


@pytest.mark.asyncio
async def test_preview_download_and_source_semantics(client, fenv, brand_dir):
    pid, fid = fenv.pid, fenv.fid
    pre = (await client.get(f"{base(pid, fid)}/preview.ass")).text
    assert "__brand__" not in pre

    await _enable(client, fenv, 60_000)
    prev = await client.get(f"{base(pid, fid)}/preview.ass")
    tr = (await client.get(f"/api/projects/{pid}/files/{fid}/subtitles/translated")).text
    orig = (await client.get(f"/api/projects/{pid}/files/{fid}/subtitles/original")).text
    assert "__brand__SubiNeko" in prev.text and "0:01:00.00" in prev.text
    assert prev.text == tr.replace(tr.split("\n", 1)[0], tr.split("\n", 1)[0]) or "__brand__SubiNeko" in tr
    assert "__brand__" not in orig and r"\p4" not in orig
    assert "Hello 0" in orig

    # preview == authoritative renderer (what Publish uses)
    with fenv.sync() as s:
        row = s.scalar(select(FileBranding))
    expected = build_ass(*_parts(fenv, fid), text_variant="translated", title="",
                         branding=br.spec_from_row(row))
    assert prev.text == expected.to_string("ass", header_notice=qc_routes.HEADER_NOTICE)

    # changing Start shifts every branding event by the same amount
    await _enable(client, fenv, 120_000)
    p2 = pysubs2.SSAFile.from_string((await client.get(f"{base(pid, fid)}/preview.ass")).text)
    shifted = [(e.start, e.end) for e in p2.events if e.style.startswith("__brand__")]
    assert shifted == [(120_000, 125_000), (121_200, 124_500), (123_000, 126_000)]

    # disabled -> back to plain
    await put(client, fenv, enabled=False, start_offset_ms=120_000)
    assert "__brand__" not in (await client.get(f"{base(pid, fid)}/preview.ass")).text


def _parts(env, fid):
    with env.sync() as s:
        subtitle = s.scalar(select(Subtitle).where(Subtitle.file_id == fid))
        from app.db.models import file_subtitle_styles
        styles = s.scalars(
            select(SubtitleStyle)
            .join(file_subtitle_styles, file_subtitle_styles.c.subtitle_style_id == SubtitleStyle.id)
            .where(file_subtitle_styles.c.file_id == fid).order_by(SubtitleStyle.id)).all()
        events = s.scalars(select(SubtitleEvent).where(SubtitleEvent.file_id == fid)
                           .order_by(SubtitleEvent.line_index)).all()
        s.expunge_all()
        return subtitle, styles, events


@pytest.mark.asyncio
async def test_missing_template_at_render_time_is_a_clear_409(client, fenv, brand_dir):
    await _enable(client, fenv)
    (brand_dir / "final.ass").unlink()
    r = await client.get(f"{base(fenv.pid, fenv.fid)}/preview.ass")
    assert r.status_code == 409 and "final.ass" in r.json()["detail"]


@pytest.mark.asyncio
async def test_publish_includes_branding(client, fenv, brand_dir, mux):
    await _enable(client, fenv, 90_000)
    result = await publish_once(fenv, fenv.pid)
    assert result["status"] == "succeeded"
    out_ass = (fenv.output_root / "series" / "ep1.ass").read_text(encoding="utf-8")
    assert "__brand__SubiNeko" in out_ass and "0:01:30.00" in out_ass
    muxed = (fenv.output_root / "series" / "ep1.mkv").read_bytes()
    assert b"__brand__SubiNeko" in muxed


@pytest.mark.asyncio
async def test_publish_without_branding_unchanged(client, fenv, brand_dir, mux):
    await publish_once(fenv, fenv.pid)
    assert "__brand__" not in (fenv.output_root / "series" / "ep1.ass").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_branding_is_not_events_metrics_or_qa(client, fenv, brand_dir):
    def counts():
        with fenv.sync() as s:
            return (s.scalar(select(func.count()).select_from(SubtitleEvent)),
                    s.scalar(select(func.count()).select_from(QaItem)))
    before = counts()
    await _enable(client, fenv)
    assert counts() == before
    listing = (await client.get(f"{base(fenv.pid, fenv.fid)}/events")).json()
    assert listing["total_count"] == before[0] and len(listing["events"]) == before[0]
    assert not any("__brand__" in e["style"] for e in listing["events"])
    assert not any(s.startswith("__brand__") for s in listing["styles"])


@pytest.mark.asyncio
async def test_retranslate_keeps_branding(client, fenv, brand_dir):
    await _enable(client, fenv, 7000)
    with fenv.sync() as s:
        s.execute(File.__table__.update().where(File.id == fenv.fid).values(status="accepted"))
        s.commit()
    await projects_routes.retranslate_file(fenv.pid, fenv.fid)
    row = db_rows(fenv)[0]
    assert (row.enabled, row.template_filename, row.start_offset_ms) == (1, "final.ass", 7000)


# --- fonts -----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_branding_fonts_in_manifest_only_while_enabled(client, fenv, brand_dir):
    add_config_font(fenv, "tb.ttf", "Trebuchet MS")
    add_config_font(fenv, "ib.ttf", "Inline Brand")
    assert "Trebuchet MS" not in (await client.get(fonts_url(fenv))).json()["required_families"]

    await _enable(client, fenv)
    body = (await client.get(fonts_url(fenv))).json()
    assert {"Trebuchet MS", "Brand Sans", "Inline Brand"} <= set(body["required_families"])  # style + \fn
    assert {"Trebuchet MS", "Inline Brand"} <= all_families(body)
    assert "Brand Sans" in body["missing_families"]                          # existing warning path
    assert not {"Trebuchet MS", "Inline Brand"} & set(body["missing_families"])
    ids = [f["id"] for f in body["fonts"]]
    assert len(ids) == len(set(ids))

    await put(client, fenv, enabled=False)
    off = (await client.get(fonts_url(fenv))).json()
    assert not {"Trebuchet MS", "Brand Sans", "Inline Brand"} & set(off["required_families"])
    assert "Brand Sans" not in off["missing_families"]


@pytest.mark.asyncio
async def test_changing_template_changes_requirements(client, fenv, brand_dir):
    (brand_dir / "alt.ass").write_text(
        TEMPLATE.replace("Trebuchet MS", "Alt Face").replace("Brand Sans", "Alt Two")
        .replace("Inline Brand", "Alt Inline"), encoding="utf-8")
    await put(client, fenv, template_filename="final.ass")
    assert "Trebuchet MS" in (await client.get(fonts_url(fenv))).json()["required_families"]
    await put(client, fenv, template_filename="alt.ass")
    req = (await client.get(fonts_url(fenv))).json()["required_families"]
    assert "Alt Face" in req and "Trebuchet MS" not in req


@pytest.mark.asyncio
async def test_shared_font_not_duplicated(client, fenv, brand_dir):
    add_config_font(fenv, "tb.ttf", "Trebuchet MS")
    set_style(fenv, font_name="Trebuchet MS", replacement_font_name=None)
    await _enable(client, fenv)
    body = (await client.get(fonts_url(fenv))).json()
    assert body["required_families"].count("Trebuchet MS") == 1
    assert [f["url"] for f in body["fonts"]].count(
        next(f["url"] for f in body["fonts"] if "Trebuchet MS" in f["family_names"])) == 1


def test_extra_families_merge_case_insensitively():
    style = SubtitleStyle(style_name="Default", font_name="Arial", font_size=20.0)
    out = collect_required_families([style], [("Default", "x", False)], use_font_replacements=False,
                                    extra_families=["arial", "Zed"])
    assert len(out) == 2 and out[1] == "Zed" and out[0].lower() == "arial"


# --- DB: constraints and FK cascade --------------------------------------------

def _fk_session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'fk.db'}")
    sa_event.listen(engine, "connect", lambda c, _r: c.execute("PRAGMA foreign_keys = ON"))
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def test_file_delete_cascades_branding_and_file_id_unique(tmp_path):
    factory = _fk_session(tmp_path)
    with factory() as s:
        p = Project(name="P", source_directory="d", anime_provider="t", anime_external_id="1",
                    status="new", created_at=_now(), updated_at=_now())
        s.add(p)
        s.flush()
        f = File(project_id=p.id, filename="a", relative_path="a", status="new",
                 created_at=_now(), updated_at=_now())
        s.add(f)
        s.flush()
        s.add(FileBranding(file_id=f.id, enabled=1, template_filename="final.ass", start_offset_ms=5))
        s.commit()
        s.add(FileBranding(file_id=f.id))
        with pytest.raises(Exception):
            s.commit()
        s.rollback()
        assert s.scalar(select(func.count()).select_from(FileBranding)) == 1
        s.delete(s.get(File, f.id))
        s.commit()
        assert s.scalar(select(func.count()).select_from(FileBranding)) == 0
