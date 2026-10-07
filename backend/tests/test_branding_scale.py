"""Optional branding scaling to the subtitle script PlayRes: pure tag transform,
PlayRes semantics, API/revision behaviour and the real template."""
from __future__ import annotations

import re
from pathlib import Path

import pysubs2
import pytest

from app.db.models import FileBranding
from app.subs import branding as br
from app.subs.ass_rendering import build_ass
from app.subs.branding import BrandingError, BrandingSpec, scale_branding_tags
from app.db.models import Subtitle, SubtitleEvent, SubtitleStyle
from tests.test_branding import (  # noqa: F401
    TEMPLATE, brand_dir, burl, client, db_rows, fenv, put,
)
from tests.test_publish import env, mux, publish_once  # noqa: F401
from tests.test_qc import base, revision, _mark_published, file_row

third = 1 / 3


def approx_tags(text: str) -> list[float]:
    return [float(x) for x in re.findall(r"-?\d+(?:\.\d+)?", text)]


# --- pure transform --------------------------------------------------------------

def test_pos_and_fsc_pinned_example_leaves_drawing_untouched():
    src = r"{\an7\pos(24,835)\fad(300,300)\fscx20.51834\fscy20.51834\bord0\p4}m 7848 1248 l 0 0 b 1 2 3 4 5 6"
    out = scale_branding_tags(src, third, third)
    head, _, drawing = out.partition("}")
    assert drawing == "m 7848 1248 l 0 0 b 1 2 3 4 5 6"
    assert r"\pos(8,278.333333)" in head
    assert r"\fscx6.839447" in head and r"\fscy6.839447" in head
    # everything else byte-identical and in order
    assert re.sub(r"\\pos\([^)]*\)|\\fscx[\d.]+|\\fscy[\d.]+", "", head) == \
        re.sub(r"\\pos\([^)]*\)|\\fscx[\d.]+|\\fscy[\d.]+", "", src.partition("}")[0])
    assert head.endswith(r"\bord0\p4")


def test_move_four_and_six_arguments():
    assert scale_branding_tags(r"{\move(30,60,90,120)}x", 0.5, 2) == r"{\move(15,120,45,240)}x"
    assert scale_branding_tags(r"{\move(30,60,90,120,100,2500)}x", 0.5, 2) == \
        r"{\move(15,120,45,240,100,2500)}x"            # timing arguments unchanged
    assert scale_branding_tags(r"{\move(1,2,3)}x", 2, 2) == r"{\move(1,2,3)}x"   # malformed: untouched


def test_org_decimals_and_spaces():
    assert scale_branding_tags(r"{\org(10.5, 20.25)}", 0.5, 0.5) == r"{\org(5.25,10.125)}"
    assert scale_branding_tags(r"{\pos( 10 , -20.5 )}", 2, 2) == r"{\pos(20,-41)}"


def test_fscx_fscy_use_their_own_axis_and_reset_forms_survive():
    assert scale_branding_tags(r"{\fscx100\fscy50}", 0.5, 2) == r"{\fscx50\fscy100}"
    assert scale_branding_tags(r"{\fscx\fscy\fsc}", 0.5, 2) == r"{\fscx\fscy\fsc}"     # no number: untouched
    assert scale_branding_tags(r"{\an7\fscx200\frz30}", 0.5, 0.5) == r"{\an7\fscx100\frz30}"


LENGTH_TAGS = {  # tag -> axis
    "fs": "y", "fsp": "x", "bord": "y", "xbord": "x", "ybord": "y",
    "shad": "y", "xshad": "x", "yshad": "y",
}


@pytest.mark.parametrize("tag,axis", LENGTH_TAGS.items())
def test_each_length_tag_uses_its_axis(tag, axis):
    out = scale_branding_tags("{\\" + tag + "40}x", 0.5, 0.25)       # sx=1/2, sy=1/4
    assert out == "{\\" + tag + ("20" if axis == "x" else "10") + "}x"


def test_length_tags_decimals_negative_and_multiple_in_one_block():
    src = r"{\fs70.5\fsp-2\bord1.6\xbord2\ybord3\shad.5\xshad4\yshad6\fscx100}"
    assert scale_branding_tags(src, 0.5, 2) == \
        r"{\fs141\fsp-1\bord3.2\xbord1\ybord6\shad1\xshad2\yshad12\fscx50}"


def test_length_tags_nested_in_t_and_non_uniform():
    src = r"{\bord2\t(0,500,\fs40\fsp1\xshad2\ybord4)}x"
    assert scale_branding_tags(src, 3, 0.5) == r"{\bord1\t(0,500,\fs20\fsp3\xshad6\ybord2)}x"


def test_valueless_forms_and_untouched_tags_stay_byte_identical():
    src = (r"{\fscx50\fscy60\fs\bord\an7\frx10\fry20\frz30\fax0.1\fay0.2\1c&H00FF00&\1a&H20&"
           r"\fad(300,300)\fade(0,255,0,0,500,600,700)\blur2\be1\q2\b1\i1\p4}")
    out = scale_branding_tags(src, 0.5, 0.5)
    assert out == src.replace(r"\fscx50\fscy60", r"\fscx25\fscy30")


def test_drawing_payload_numbers_untouched_even_when_they_look_like_tags():
    drawing = r"m 7848 1248 l 40 40 b 1 2 3 4 5 6 \fs50 \bord9 \pos(10,10)"
    src = r"{\pos(24,835)\fs70\bord2\fscx100\p4}" + drawing
    out = scale_branding_tags(src, 0.25, 0.25)
    assert out.endswith("}" + drawing)
    assert out.startswith(r"{\pos(6,208.75)\fs17.5\bord0.5\fscx25\p4}")


def test_multiple_blocks_text_and_unrelated_tags_unchanged():
    src = r"Hi {\pos(10,10)\1c&H00FF00&\an7\b1\frz30\clip(1,2,3,4)}there {\fscx50}x \pos(99,99) y"
    out = scale_branding_tags(src, 2, 3)
    assert out == r"Hi {\pos(20,30)\1c&H00FF00&\an7\b1\frz30\clip(1,2,3,4)}there {\fscx100}x \pos(99,99) y"


def test_animated_t_block_is_scaled_consistently():
    assert scale_branding_tags(r"{\fscx100\t(0,500,\fscx200)}", 0.5, 0.5) == r"{\fscx50\t(0,500,\fscx100)}"


def test_identity_scale_is_byte_stable():
    src = r"{\pos(24,835)\fscx20.51834}m 1 2"
    assert scale_branding_tags(src, 1.0, 1.0) == r"{\pos(24,835)\fscx20.51834}m 1 2"


# --- PlayRes semantics through build_ass --------------------------------------

def _ass(spec, play=(640, 360), variant="translated"):
    subtitle = Subtitle(script_type="v4.00+", play_res_x=play[0], play_res_y=play[1])
    style = SubtitleStyle(style_name="Default", font_name="Arial", font_size=20.0)
    ev = SubtitleEvent(
        line_index=0, event_type="dialogue", layer=0, start_ms=100, end_ms=900,
        original_start_ms=100, original_end_ms=900, style="Default",
        source_text="src", translated_text="tr")
    return build_ass(subtitle, [style], [ev], text_variant=variant, branding=spec)


def _pos(subs, i=1):
    return re.search(r"\\pos\(([^)]*)\)", subs.events[i].text).group(1)


def test_scaling_disabled_is_unchanged(brand_dir):
    plain = _ass(BrandingSpec("final.ass", 0))
    assert _pos(plain) == "24,835"
    assert _ass(BrandingSpec("final.ass", 0, False)).to_string("ass") == plain.to_string("ass")


def test_one_third_and_two_thirds(brand_dir):
    assert _pos(_ass(BrandingSpec("final.ass", 0, True), (640, 360))) == "8,278.333333"
    assert _pos(_ass(BrandingSpec("final.ass", 0, True), (1280, 720))) == "16,556.666667"


def test_styles_are_scaled_too_and_alignment_kept(brand_dir):
    subs = _ass(BrandingSpec("final.ass", 0, True), (640, 360))
    st = subs.styles["__brand__SubiNeko"]
    assert st.fontsize == pytest.approx(70 / 3, abs=1e-5)
    assert st.outline == pytest.approx(1.6 / 3, abs=1e-5)
    assert (st.marginl, st.marginr, st.marginv) == (100, 0, 17)      # 300/3, 0, 50/3 -> 16.67
    assert st.alignment == 1 and st.fontname == "Trebuchet MS"
    other = subs.styles["__brand__Other"]
    assert other.fontsize == pytest.approx(40 / 3, abs=1e-5) and other.alignment == 2
    # unscaled / identity leave styles exactly as authored
    raw = _ass(BrandingSpec("final.ass", 0, False), (640, 360)).styles["__brand__SubiNeko"]
    assert raw.fontsize == 70 and raw.marginl == 300
    ident = _ass(BrandingSpec("final.ass", 0, True), (1920, 1080)).styles["__brand__SubiNeko"]
    assert ident.fontsize == 70 and ident.marginv == 50
    assert br.load_template("final.ass").styles["SubiNeko"].fontsize == 70   # cache untouched


def test_non_uniform_style_axes(brand_dir):
    st = _ass(BrandingSpec("final.ass", 0, True), (960, 1080)).styles["__brand__SubiNeko"]
    assert st.fontsize == 70 and st.marginl == 150 and st.marginv == 50


def test_equal_playres_changes_nothing(brand_dir):
    same = _ass(BrandingSpec("final.ass", 0, True), (1920, 1080))
    assert same.to_string("ass") == _ass(BrandingSpec("final.ass", 0, False), (1920, 1080)).to_string("ass")


def test_non_uniform_target_uses_independent_axes(brand_dir):
    (brand_dir / "fsc.ass").write_text(TEMPLATE.replace(
        r"{\an7\pos(24,835)\p4}", r"{\an7\pos(24,835)\fscx100\fscy100\p4}"), encoding="utf-8")
    subs = _ass(BrandingSpec("fsc.ass", 0, True), (960, 1080))        # x * 0.5, y * 1
    assert _pos(subs) == "12,835"
    assert r"\fscx50\fscy100" in subs.events[1].text


def test_missing_or_invalid_playres_fails_clearly(brand_dir):
    spec = BrandingSpec("final.ass", 0, True)
    for bad in [(0, 360), (640, -1)]:
        with pytest.raises(BrandingError, match="not positive"):
            _ass(spec, bad)
    with pytest.raises(BrandingError, match="subtitle script"):
        _ass(spec, (None, None))
    # unscaled branding never needs PlayRes
    assert _ass(BrandingSpec("final.ass", 0, False), (None, None))


def test_template_without_playres_fails_when_scaling(brand_dir):
    (brand_dir / "nores.ass").write_text(
        TEMPLATE.replace("PlayResX: 1920\n", "").replace("PlayResY: 1080\n", ""), encoding="utf-8")
    with pytest.raises(BrandingError, match="branding template nores.ass"):
        _ass(BrandingSpec("nores.ass", 0, True))
    assert _ass(BrandingSpec("nores.ass", 0, False))


def test_source_variant_still_never_branded(brand_dir):
    subs = _ass(BrandingSpec("final.ass", 0, True), variant="original")
    assert len(subs.events) == 1 and set(subs.styles) == {"Default"}


def test_template_cache_is_not_polluted_by_scaling(brand_dir):
    _ass(BrandingSpec("final.ass", 0, True))
    assert "pos(24,835)" in br.load_template("final.ass").events[0].text


# --- API / revision ----------------------------------------------------------------

@pytest.mark.asyncio
async def test_api_roundtrip_and_revision(client, fenv, brand_dir):
    assert (await client.get(burl(fenv))).json()["scale_to_script_playres"] is False
    await _mark_published(fenv, fenv.pid)
    r0 = revision(fenv, fenv.pid)
    r = await put(client, fenv, scale_to_script_playres=True)
    assert r.status_code == 200 and r.json()["scale_to_script_playres"] is True
    assert revision(fenv, fenv.pid) == r0 + 1                      # real change bumps
    await put(client, fenv, scale_to_script_playres=True)          # no-op
    assert revision(fenv, fenv.pid) == r0 + 1
    await put(client, fenv, scale_to_script_playres=False)
    assert revision(fenv, fenv.pid) == r0 + 2
    assert file_row(fenv, fenv.pid).status == "accepted"
    assert db_rows(fenv)[0].scale_to_script_playres == 0


@pytest.mark.asyncio
async def test_omitted_flag_defaults_false_and_preview_scales(client, fenv, brand_dir):
    await put(client, fenv)                                         # flag omitted
    assert db_rows(fenv)[0].scale_to_script_playres == 0
    off = (await client.get(f"{base(fenv.pid, fenv.fid)}/preview.ass")).text
    assert "pos(24,835)" in off
    await put(client, fenv, scale_to_script_playres=True)
    on = (await client.get(f"{base(fenv.pid, fenv.fid)}/preview.ass")).text
    # seeded script is 1920x1080 -> identity, so geometry is unchanged
    assert "pos(24,835)" in on


@pytest.mark.asyncio
async def test_scaled_preview_and_download_and_publish(client, fenv, brand_dir, mux):
    from sqlalchemy import update
    with fenv.sync() as s:
        s.execute(update(Subtitle).values(play_res_x=640, play_res_y=360))
        s.commit()
    await put(client, fenv, scale_to_script_playres=True)
    prev = (await client.get(f"{base(fenv.pid, fenv.fid)}/preview.ass")).text
    dl = (await client.get(f"/api/projects/{fenv.pid}/files/{fenv.fid}/subtitles/translated")).text
    orig = (await client.get(f"/api/projects/{fenv.pid}/files/{fenv.fid}/subtitles/original")).text
    assert "pos(8,278.333333)" in prev and "pos(8,278.333333)" in dl
    assert "__brand__" not in orig and "278.33" not in orig
    result = await publish_once(fenv, fenv.pid)
    assert result["status"] == "succeeded"
    assert "pos(8,278.333333)" in (fenv.output_root / "series" / "ep1.ass").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_invalid_playres_surfaces_as_409_and_publish_failure(client, fenv, brand_dir, mux):
    from sqlalchemy import update
    with fenv.sync() as s:
        s.execute(update(Subtitle).values(play_res_x=0, play_res_y=0))
        s.commit()
    await put(client, fenv, scale_to_script_playres=True)
    r = await client.get(f"{base(fenv.pid, fenv.fid)}/preview.ass")
    assert r.status_code == 409 and "PlayRes" in r.json()["detail"]
    assert (await client.get(f"/api/projects/{fenv.pid}/files/{fenv.fid}/subtitles/translated")).status_code == 409
    result = await publish_once(fenv, fenv.pid)
    assert result["status"] != "succeeded"
    assert not (fenv.output_root / "series" / "ep1.ass").exists()


# --- the real template -------------------------------------------------------------

REAL = Path(__file__).resolve().parents[2] / "config" / "brand" / "final.ass"


@pytest.mark.skipif(not REAL.is_file(), reason="config/brand/final.ass not present")
def test_real_template_scaled_1080p_to_360p(tmp_path, monkeypatch):
    from app.core.config import settings
    monkeypatch.setattr(settings, "config_root", tmp_path)
    (tmp_path / "brand").mkdir()
    (tmp_path / "brand" / "final.ass").write_bytes(REAL.read_bytes())

    off = _ass(BrandingSpec("final.ass", 5000, False), (640, 360))
    on = _ass(BrandingSpec("final.ass", 5000, True), (640, 360))
    a, b = off.events[1:], on.events[1:]
    assert len(a) == len(b) > 0
    changed_tags = 0
    for x, y in zip(a, b):
        assert (x.layer, x.start, x.end, x.style, x.name, x.effect) == (y.layer, y.start, y.end, y.style, y.name, y.effect)
        hx, _, dx = x.text.partition("}")
        hy, _, dy = y.text.partition("}")
        if r"\pos(" not in hx:                                      # the plain-text event: untouched
            assert x.text == y.text
            continue
        assert dx == dy                                            # drawing payload byte-identical
        assert re.sub(r"\\(pos|fscx|fscy)[^\\}]*", "", hx) == re.sub(r"\\(pos|fscx|fscy)[^\\}]*", "", hy)
        px, py = approx_tags(re.search(r"\\pos\(([^)]*)\)", hx).group(1)), approx_tags(re.search(r"\\pos\(([^)]*)\)", hy).group(1))
        assert py == pytest.approx([px[0] / 3, px[1] / 3], abs=1e-5)
        fx = float(re.search(r"\\fscx([\d.]+)", hx).group(1))
        fy = float(re.search(r"\\fscy([\d.]+)", hy).group(1))
        assert fy == pytest.approx(fx / 3, abs=1e-5)
        changed_tags += 1
    assert changed_tags >= 80
    assert set(off.styles) == set(on.styles)
