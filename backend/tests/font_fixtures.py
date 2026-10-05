"""Tiny generated fonts (no binary fixtures committed)."""
from __future__ import annotations

from pathlib import Path

from fontTools.fontBuilder import FontBuilder
from fontTools.pens.t2CharStringPen import T2CharStringPen
from fontTools.pens.ttGlyphPen import TTGlyphPen


def make_font(
    path: Path,
    family: str,
    *,
    fmt: str = "ttf",             # ttf | otf | woff | woff2
    full_name: str | None = None,
    typographic_family: str | None = None,
    extra_names: list[tuple[str, int, int, int, int]] | None = None,  # (text, nameID, platform, enc, lang)
    bold: bool = False,
    italic: bool = False,
) -> Path:
    """Write a minimal valid font whose internal names are what the tests assert on."""
    cff = fmt == "otf"
    fb = FontBuilder(1000, isTTF=not cff)
    fb.setupGlyphOrder([".notdef"])
    fb.setupCharacterMap({})
    names = {
        "familyName": family,
        "styleName": "Bold" if bold else "Regular",
        "uniqueFontIdentifier": f"{family}-{fmt}-{bold}-{italic}",
        "fullName": full_name or f"{family} {'Bold' if bold else 'Regular'}",
        "psName": family.replace(" ", "") + ("-Bold" if bold else "-Regular"),
        "version": "Version 1.0",
    }
    if typographic_family:
        names["typographicFamily"] = typographic_family
    if cff:
        pen = T2CharStringPen(600, None)
        pen.moveTo((0, 0))
        pen.lineTo((0, 100))
        pen.lineTo((100, 0))
        pen.closePath()
        fb.setupCFF(names["psName"], {"FullName": names["fullName"]},
                    {".notdef": pen.getCharString()}, {})
        metrics = {".notdef": (600, 0)}
    else:
        pen = TTGlyphPen(None)
        pen.moveTo((0, 0))
        pen.lineTo((0, 100))
        pen.lineTo((100, 0))
        pen.closePath()
        fb.setupGlyf({".notdef": pen.glyph()})
        metrics = {".notdef": (600, 0)}
    fb.setupHorizontalMetrics(metrics)
    fb.setupHorizontalHeader(ascent=800, descent=-200)
    fb.setupNameTable(names)
    fb.setupOS2(sTypoAscender=800, sTypoDescender=-200, usWinAscent=800, usWinDescent=200,
                fsSelection=(0x20 if bold else 0) | (0x01 if italic else 0) | (0x40 if not (bold or italic) else 0))
    fb.setupPost()
    for text, name_id, platform, enc, lang in extra_names or []:
        fb.font["name"].setName(text, name_id, platform, enc, lang)
    if fmt in ("woff", "woff2"):
        fb.font.flavor = fmt
    path.parent.mkdir(parents=True, exist_ok=True)
    fb.save(str(path))
    return path
