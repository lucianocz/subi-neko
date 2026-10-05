"""Font metadata parser, registry, configured scan and family matching."""
from __future__ import annotations

import pytest

from app.subs.font_registry import (
    FontContext,
    FontParseError,
    FontRegistry,
    find_font_by_family,
    normalize_family,
    parse_font_file,
    refresh_configured_registry,
    scan_font_directory,
    set_configured_registry,
)
from tests.font_fixtures import make_font


@pytest.mark.parametrize("fmt,ctype", [
    ("ttf", "font/ttf"), ("otf", "font/otf"), ("woff", "font/woff"), ("woff2", "font/woff2"),
])
def test_internal_family_extracted_for_every_format(tmp_path, fmt, ctype):
    # filename deliberately unrelated to the family name
    p = make_font(tmp_path / f"zzz_unrelated.{fmt}", "Cool Sans", fmt=fmt)
    face = parse_font_file(p, source="configured")
    assert face.families[0] == "Cool Sans"
    assert face.media_type == ctype
    assert face.filename == f"zzz_unrelated.{fmt}"
    assert "zzz_unrelated" not in " ".join(face.families)


def test_multiple_name_records_and_full_name_alias(tmp_path):
    p = make_font(
        tmp_path / "a.ttf", "Stone Sans", full_name="ITC Stone Sans Std Medium",
        typographic_family="Stone Sans Std",
        extra_names=[("Mac Stone", 1, 1, 0, 0), ("Stone Sans CZ", 1, 3, 1, 0x405)])
    face = parse_font_file(p, source="attachment")
    keys = face.keys
    assert {"stone sans", "stone sans std", "itc stone sans std medium",
            "mac stone", "stone sans cz"} <= keys
    reg = FontRegistry([face])
    assert reg.find("ITC STONE SANS STD MEDIUM") == [face]


def test_case_insensitive_and_vertical_prefix(tmp_path):
    face = parse_font_file(make_font(tmp_path / "f.ttf", "Open  Sans"), source="configured")
    reg = FontRegistry([face])
    assert reg.find("open sans") == reg.find("OPEN SANS") == reg.find("@Open Sans") == [face]
    assert normalize_family("  @Foo   Bar ") == "foo bar"
    assert reg.find("Open") == []


def test_style_flags(tmp_path):
    b = parse_font_file(make_font(tmp_path / "b.ttf", "F", bold=True), source="configured")
    i = parse_font_file(make_font(tmp_path / "i.ttf", "F", italic=True), source="configured")
    assert (b.bold, b.italic) == (True, False)
    assert (i.bold, i.italic) == (False, True)


def test_multiple_faces_same_family_preserved_and_ordered(tmp_path):
    reg = FontRegistry(
        parse_font_file(make_font(tmp_path / n, "Face", bold=bold), source="configured")
        for n, bold in (("z_bold.ttf", True), ("a_regular.ttf", False)))
    faces = reg.find("face")
    assert [f.bold for f in faces] == [False, True]      # deterministic: regular, bold
    assert len(reg) == 2


def test_identical_bytes_deduplicated(tmp_path):
    a = make_font(tmp_path / "a.ttf", "Same")
    (tmp_path / "b.ttf").write_bytes(a.read_bytes())
    reg = scan_font_directory(tmp_path)
    assert len(reg) == 1 and len(reg.find("Same")) == 1


def test_id_is_content_hash_and_stable(tmp_path):
    p = make_font(tmp_path / "a.ttf", "Stable")
    assert parse_font_file(p, source="configured").id == parse_font_file(p, source="configured").id
    q = make_font(tmp_path / "b.ttf", "Different")
    assert parse_font_file(q, source="configured").id != parse_font_file(p, source="configured").id


def test_malformed_font_raises_clean_error(tmp_path):
    bad = tmp_path / "bad.ttf"
    bad.write_bytes(b"this is not a font")
    with pytest.raises(FontParseError):
        parse_font_file(bad, source="configured")
    with pytest.raises(FontParseError):
        parse_font_file(tmp_path / "missing.ttf", source="configured")
    txt = tmp_path / "x.txt"
    txt.write_text("x")
    with pytest.raises(FontParseError):
        parse_font_file(txt, source="configured")


def test_configured_scan_skips_malformed_and_unsupported(tmp_path):
    make_font(tmp_path / "ok.ttf", "Good")
    make_font(tmp_path / "sub" / "ok2.woff2", "Nested", fmt="woff2")
    (tmp_path / "bad.otf").write_bytes(b"garbage")
    (tmp_path / "readme.txt").write_text("hi")
    reg = scan_font_directory(tmp_path)
    assert {f.families[0] for f in reg.faces()} == {"Good", "Nested"}
    assert scan_font_directory(tmp_path / "nope").faces() == []     # missing dir is fine
    assert all(f.source == "configured" for f in reg.faces())


def test_configured_registry_refresh(tmp_path, monkeypatch):
    from app.core.config import settings
    monkeypatch.setattr(settings, "fonts_root", tmp_path)
    set_configured_registry(None)
    assert len(refresh_configured_registry()) == 0
    make_font(tmp_path / "new.ttf", "Later")
    assert len(refresh_configured_registry().find("Later")) == 1
    set_configured_registry(None)


def test_find_font_priority_attachment_over_configured(tmp_path):
    att = parse_font_file(make_font(tmp_path / "att" / "x.ttf", "Dup"), source="attachment")
    cfg = parse_font_file(make_font(tmp_path / "cfg" / "y.ttf", "Dup", bold=True), source="configured")
    only_cfg = parse_font_file(make_font(tmp_path / "cfg" / "z.ttf", "Only"), source="configured")
    ctx = FontContext(attachments=FontRegistry([att]), configured=FontRegistry([cfg, only_cfg]))
    assert find_font_by_family("dup", ctx) == [att]          # attachment wins, not mixed
    assert find_font_by_family("Only", ctx) == [only_cfg]
    assert find_font_by_family("Arial", ctx) == []           # no aliasing
    assert find_font_by_family("anything", FontContext()) == []


def test_filename_is_not_a_family(tmp_path):
    face = parse_font_file(make_font(tmp_path / "Arial.ttf", "Totally Different"), source="configured")
    assert FontRegistry([face]).find("Arial") == []
