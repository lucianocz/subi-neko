"""Optional translated-output style overrides (bold/italic/outline/shadow/colours).

NULL inherits the source value; ``False``/``0`` are real overrides; source output
never sees any override; one ``effective_style`` serves QC preview, downloads and
Publish.
"""
from __future__ import annotations

import pysubs2
import pytest
from sqlalchemy import select

from app.db.models import Subtitle, SubtitleEvent, SubtitleStyle
from app.subs.ass_rendering import build_ass, effective_style, save_ass
from app.subs.style_canonical import compute_source_style_hash
from tests.test_publish import click_publish, env, mux, publish_once, seed_project  # noqa: F401
from tests.test_qc import base, client, file_row, one, qc_env, revision  # noqa: F401

SRC = dict(
    bold=1, italic=0, outline=2.5, shadow=1.25,
    primary_colour="&H80112233&", outline_colour="&H00445566&", back_colour="&H7F778899&",
)
NEW_FIELDS = (
    "replacement_bold", "replacement_italic", "replacement_outline", "replacement_shadow",
    "replacement_primary_colour", "replacement_outline_colour", "replacement_back_colour",
)


def make_style(**over) -> SubtitleStyle:
    values = dict(
        project_id=1, source_style_hash="h", style_name="Default", font_name="Arial",
        font_size=42.0, secondary_colour="&H000000FF&", **SRC)
    values.update(over)
    return SubtitleStyle(**values)


def render(style, variant="translated", **kw) -> pysubs2.SSAStyle:
    ev = SubtitleEvent(
        file_id=1, line_index=0, event_type="dialogue", layer=0, start_ms=0, end_ms=1000,
        original_start_ms=0, original_end_ms=1000, style="Default", source_text="Hi",
        translated_text="Ahoj")
    return build_ass(Subtitle(file_id=1), [style], [ev], text_variant=variant, **kw).styles["Default"]


def colours(st: pysubs2.SSAStyle):
    return tuple(
        (c.r, c.g, c.b, c.a) for c in (st.primarycolor, st.outlinecolor, st.backcolor))


# --- defaults / inheritance -------------------------------------------------

def test_new_overrides_default_to_null():
    assert all(getattr(make_style(), f) is None for f in NEW_FIELDS)


def test_unset_overrides_inherit_the_source_exactly():
    st = render(make_style())
    assert (st.bold, st.italic, st.outline, st.shadow) == (True, False, 2.5, 1.25)
    assert colours(st) == ((0x33, 0x22, 0x11, 0x80), (0x66, 0x55, 0x44, 0), (0x99, 0x88, 0x77, 0x7F))


def test_each_override_applies_independently():
    st = render(make_style(replacement_outline=0.75))
    assert (st.bold, st.italic, st.outline, st.shadow) == (True, False, 0.75, 1.25)
    assert colours(st) == colours(render(make_style()))


# --- explicit falsy values ---------------------------------------------------

def test_explicit_false_disables_bold_and_italic():
    st = render(make_style(italic=1, replacement_bold=0, replacement_italic=0))
    assert (st.bold, st.italic) == (False, False)


def test_explicit_true_enables_when_source_is_off():
    assert render(make_style(replacement_italic=1)).italic is True


def test_explicit_zero_disables_outline_and_shadow():
    st = render(make_style(replacement_outline=0.0, replacement_shadow=0.0))
    assert (st.outline, st.shadow) == (0.0, 0.0)


@pytest.mark.parametrize("value", [0.5, 1.25, 2.75, 0.1])
def test_fractional_values_survive_ass_serialization(value, tmp_path):
    subs = build_ass(
        Subtitle(file_id=1), [make_style(replacement_outline=value, replacement_shadow=value)],
        [], text_variant="translated")
    path = tmp_path / "o.ass"
    save_ass(subs, path)
    st = pysubs2.load(str(path)).styles["Default"]
    assert (st.outline, st.shadow) == (value, value)


# --- colours / alpha ----------------------------------------------------------

def test_colour_override_keeps_alpha_and_rgb_channels():
    st = render(make_style(replacement_primary_colour="&HFF0A0B0C&",   # fully transparent
                           replacement_back_colour="&H00FFFFFF&"))
    assert (st.primarycolor.r, st.primarycolor.g, st.primarycolor.b, st.primarycolor.a) == (0x0C, 0x0B, 0x0A, 0xFF)
    assert (st.backcolor.r, st.backcolor.a) == (0xFF, 0)
    assert colours(st)[1] == colours(render(make_style()))[1]   # outline inherited


def test_colour_override_roundtrips_through_saved_ass(tmp_path):
    style = make_style(replacement_outline_colour="&H3C1020FE&")
    path = tmp_path / "c.ass"
    save_ass(build_ass(Subtitle(file_id=1), [style], [], text_variant="translated"), path)
    c = pysubs2.load(str(path)).styles["Default"].outlinecolor
    assert (c.a, c.b, c.g, c.r) == (0x3C, 0x10, 0x20, 0xFE)


# --- source vs translated separation -------------------------------------------

def test_source_output_ignores_every_override():
    style = make_style(
        replacement_font_name="Noto Sans", replacement_font_size=30.0, replacement_bold=0,
        replacement_italic=1, replacement_outline=0.0, replacement_shadow=9.0,
        replacement_primary_colour="&H00000000&", replacement_outline_colour="&H00000000&",
        replacement_back_colour="&H00000000&")
    assert render(style, "original") == render(make_style(), "original")
    eff = effective_style(style, text_variant="original")
    assert (eff.font_name, eff.font_size, eff.bold, eff.outline, eff.primary_colour) == (
        "Arial", 42.0, True, 2.5, "&H80112233&")


def test_translated_output_differs_from_source_and_stored_values_untouched():
    style = make_style(replacement_bold=0, replacement_shadow=3.0)
    tr, orig = render(style), render(style, "original")
    assert (tr.bold, tr.shadow) == (False, 3.0) and (orig.bold, orig.shadow) == (True, 1.25)
    assert (style.bold, style.shadow) == (1, 1.25)
    assert (style.replacement_bold, style.replacement_shadow) == (0, 3.0)


def test_new_overrides_do_not_depend_on_replace_incompatible_fonts():
    style = make_style(replacement_font_name="Noto Sans", replacement_outline=0.0)
    off = render(style, use_font_replacements=False)
    assert (off.fontname, off.outline) == ("Arial", 0.0)


def test_event_level_override_tags_are_untouched():
    text = r"{\b0\i1\bord4.5\shad0\1c&H0000FF&\3c&H00FF00&\4c&HFF0000&\alpha&H80&}Ahoj"
    ev = SubtitleEvent(
        file_id=1, line_index=0, event_type="dialogue", layer=0, start_ms=0, end_ms=10,
        original_start_ms=0, original_end_ms=10, style="Default", source_text="x", translated_text=text)
    subs = build_ass(Subtitle(file_id=1), [make_style(replacement_bold=1, replacement_outline=0.0)],
                     [ev], text_variant="translated")
    assert subs.events[0].text == text


def test_source_style_hash_ignores_replacement_overrides():
    values = dict(
        style_name="Default", font_name="Arial", font_size=42.0, **SRC)
    assert compute_source_style_hash(values) == compute_source_style_hash(
        {**values, "replacement_bold": 0, "replacement_outline": 0.0, "replacement_primary_colour": "&H00000000&"})


# --- API: persistence ----------------------------------------------------------

def _styles_url(pid, sid=None):
    return f"/api/projects/{pid}/styles" + (f"/{sid}" if sid else "")


async def _style_id(client, pid) -> int:
    return (await client.get(_styles_url(pid))).json()[0]["id"]


@pytest.mark.asyncio
async def test_api_lists_source_and_null_replacements(client, one):
    pid, _ = one
    body = (await client.get(_styles_url(pid))).json()[0]
    assert all(body[f] is None for f in NEW_FIELDS)
    assert (body["bold"], body["italic"], body["outline"], body["shadow"]) == (False, False, 2.0, 0.0)


@pytest.mark.asyncio
async def test_api_preserves_false_zero_and_fractions(client, one, qc_env):
    pid, _ = one
    sid = await _style_id(client, pid)
    r = await client.put(_styles_url(pid, sid), json={
        "replacement_bold": False, "replacement_italic": True, "replacement_outline": 0,
        "replacement_shadow": 1.25, "replacement_primary_colour": "&h80aabbcc&",
        "replacement_outline_colour": "&H00000000&", "replacement_back_colour": "&HFF000000&"})
    assert r.status_code == 200
    out = r.json()
    assert (out["replacement_bold"], out["replacement_italic"]) == (False, True)
    assert (out["replacement_outline"], out["replacement_shadow"]) == (0.0, 1.25)
    assert out["replacement_primary_colour"] == "&H80AABBCC&"  # canonicalised
    with qc_env.sync() as s:
        row = s.get(SubtitleStyle, sid)
        assert (row.replacement_bold, row.replacement_outline, row.replacement_shadow) == (0, 0.0, 1.25)
        assert row.replacement_back_colour == "&HFF000000&"


@pytest.mark.asyncio
async def test_api_individual_reset_only_clears_that_property(client, one, qc_env):
    pid, _ = one
    sid = await _style_id(client, pid)
    await client.put(_styles_url(pid, sid), json={
        "replacement_font_name": "Noto Sans", "replacement_font_size": 30,
        "replacement_outline": 0, "replacement_bold": False})
    out = (await client.put(_styles_url(pid, sid), json={
        "replacement_font_name": "Noto Sans", "replacement_font_size": 30,
        "replacement_outline": None, "replacement_bold": False})).json()
    assert out["replacement_outline"] is None and out["replacement_bold"] is False
    assert (out["replacement_font_name"], out["replacement_font_size"]) == ("Noto Sans", 30.0)
    assert out["outline"] == 2.0  # source value unchanged


@pytest.mark.asyncio
async def test_api_legacy_body_does_not_wipe_new_overrides(client, one):
    pid, _ = one
    sid = await _style_id(client, pid)
    await client.put(_styles_url(pid, sid), json={"replacement_shadow": 2.0, "replacement_bold": True})
    out = (await client.put(_styles_url(pid, sid), json={"replacement_font_name": "Noto Sans"})).json()
    assert (out["replacement_shadow"], out["replacement_bold"]) == (2.0, True)
    assert out["replacement_font_name"] == "Noto Sans"


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    {"replacement_outline": -0.1}, {"replacement_shadow": -1},
    {"replacement_outline": "x"}, {"replacement_bold": "maybe"},
    {"replacement_primary_colour": "#ffffff"}, {"replacement_back_colour": "&H123&"},
    {"replacement_outline_colour": "&HGG000000&"}, {"replacement_shadow": 1e9},
])
async def test_api_rejects_invalid_values(client, one, payload):
    pid, _ = one
    sid = await _style_id(client, pid)
    assert (await client.put(_styles_url(pid, sid), json=payload)).status_code == 422


@pytest.mark.asyncio
async def test_noop_save_does_not_bump_revision_but_real_change_does(client, one, qc_env):
    pid, _ = one
    sid = await _style_id(client, pid)
    payload = {"replacement_outline": 1.5, "replacement_bold": False,
               "replacement_primary_colour": "&H00FFFFFF&"}
    await client.put(_styles_url(pid, sid), json=payload)
    before = revision(qc_env, pid)
    assert (await client.put(_styles_url(pid, sid), json=payload)).status_code == 200
    assert revision(qc_env, pid) == before
    await client.put(_styles_url(pid, sid), json={**payload, "replacement_outline": 1.75})
    assert revision(qc_env, pid) == before + 1
    # clearing an override is a real change too
    await client.put(_styles_url(pid, sid), json={**payload, "replacement_outline": None})
    assert revision(qc_env, pid) == before + 2


@pytest.mark.asyncio
async def test_style_is_shared_project_wide(client, qc_env):
    pid = seed_project(qc_env, n_files=2)
    with qc_env.sync() as s:  # the two seeded files get distinct canonical styles; share one
        from app.db.models import file_subtitle_styles
        ids = s.scalars(select(SubtitleStyle.id).where(SubtitleStyle.project_id == pid)).all()
        s.execute(file_subtitle_styles.update().where(
            file_subtitle_styles.c.subtitle_style_id == ids[1]).values(subtitle_style_id=ids[0]))
        s.commit()
    sid = ids[0]
    await client.put(_styles_url(pid, sid), json={"replacement_outline": 0.5})
    listed = (await client.get(_styles_url(pid))).json()
    shared = next(x for x in listed if x["id"] == sid)
    assert shared["file_count"] == 2 and shared["replacement_outline"] == 0.5


# --- QC preview / download / publish use the same effective style ------------------

@pytest.mark.asyncio
async def test_qc_preview_download_and_publish_share_effective_styles(client, one, qc_env, mux):  # noqa: F811
    pid, fid = one
    sid = await _style_id(client, pid)
    with qc_env.sync() as s:
        s.get(SubtitleStyle, sid).primary_colour = "&H00FFFFFF&"
        s.commit()
    await client.put(_styles_url(pid, sid), json={
        "replacement_bold": True, "replacement_outline": 0.5, "replacement_shadow": 0,
        "replacement_primary_colour": "&H40102030&"})

    def style_line(text: str) -> str:
        return next(l for l in text.splitlines() if l.startswith("Style: Default,"))

    preview = (await client.get(f"{base(pid, fid)}/preview.ass")).text
    download = (await client.get(f"/api/projects/{pid}/files/{fid}/subtitles/translated")).text
    original = (await client.get(f"/api/projects/{pid}/files/{fid}/subtitles/original")).text
    assert style_line(preview) == style_line(download)
    fields = style_line(preview).split(",")
    assert fields[3] == "&H40102030" and fields[7] == "-1" and fields[16] == "0.5" and fields[17] == "0"
    assert style_line(original) != style_line(preview)
    assert style_line(original).split(",")[3] == "&H00FFFFFF"

    await publish_once(qc_env, pid)
    published = (qc_env.output_root / "series" / "ep1.ass").read_text(encoding="utf-8")
    assert style_line(published) == style_line(preview)


BRAND_TEMPLATE = """[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Logo,Arial,40,&H00112233,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,3.5,0.5,5,0,0,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:00.00,0:00:02.00,Logo,,0,0,0,,Brand
"""


def test_branding_styles_are_not_touched_by_overrides(tmp_path, monkeypatch):
    from app.core.config import settings
    from app.subs.branding import BRAND_STYLE_PREFIX, BrandingSpec

    brand = tmp_path / "brand"
    brand.mkdir()
    (brand / "t.ass").write_text(BRAND_TEMPLATE, encoding="utf-8")
    monkeypatch.setattr(settings, "config_root", tmp_path)
    monkeypatch.setattr(type(settings), "brand_dir", property(lambda self: brand), raising=False)
    style = make_style(replacement_bold=0, replacement_outline=0.0, replacement_shadow=9.0,
                       replacement_primary_colour="&H00FFFFFF&")
    subs = build_ass(Subtitle(file_id=1, play_res_x=1920, play_res_y=1080), [style], [],
                     text_variant="translated", branding=BrandingSpec("t.ass", 0))
    logo = subs.styles[BRAND_STYLE_PREFIX + "Logo"]
    assert (logo.outline, logo.shadow, logo.bold) == (3.5, 0.5, False)
    assert (logo.primarycolor.r, logo.primarycolor.g, logo.primarycolor.b) == (0x33, 0x22, 0x11)
    assert (subs.styles["Default"].outline, subs.styles["Default"].shadow) == (0.0, 9.0)
