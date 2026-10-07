"""Which fonts the QC preview of one file needs, and how they resolve.

The required families follow the SAME semantics as ``build_ass`` for the
translated variant: ``effective_font`` picks ``replacement_font_name`` when the
``REPLACE_INCOMPATIBLE_FONTS`` option is on (falling back to ``font_name``),
else ``font_name``. On top of the style fonts, explicit ``\\fn`` overrides in
the (non-hidden) translated event text are collected; ``\\fn`` with no name
(reset to style font) is ignored.

Resolution (``resolve_manifest``) returns *every* font attached to the MKV
(v1 is deliberately conservative: attachments are cheap and the file's own
typesetting may reference them from places we cannot see) plus the faces for
each required family that ``find_font_by_family`` resolves from attachments or
the configured directory. Families nothing resolves are reported as missing —
that is a warning for the UI, not an error.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from app.subs.ass_rendering import effective_font
from app.subs.font_registry import FontContext, FontFace, FontRegistry, find_font_by_family, normalize_family

_OVERRIDE_BLOCK_RE = re.compile(r"\{([^}]*)\}")
_FN_RE = re.compile(r"\\fn([^\\}]*)")


def families_in_text(text: str | None) -> list[str]:
    """Explicit ``\\fn<name>`` families inside ``{...}`` override blocks."""
    if not text or "\\fn" not in text:
        return []
    found = []
    for block in _OVERRIDE_BLOCK_RE.findall(text):
        for name in _FN_RE.findall(block):
            name = name.strip()
            if name:
                found.append(name)
    return found


def collect_required_families(
    styles: Iterable,
    events: Iterable[tuple[str, str | None, bool]],
    *,
    use_font_replacements: bool,
    extra_families: Iterable[str] = (),
) -> list[str]:
    """Distinct (case-insensitive) family names the translated ASS will request,
    sorted. ``events`` = ``(style_name, translated_text, is_hidden)``.
    ``extra_families`` are render-time additions (branding template fonts)
    that no event row carries."""
    by_name = {s.style_name: s for s in styles}
    used_styles: set[str] = set()
    names: list[str] = list(extra_families)
    for style_name, text, is_hidden in events:
        if is_hidden:
            continue
        used_styles.add(style_name)
        names.extend(families_in_text(text))
    if not used_styles:                       # nothing visible: every style
        used_styles = set(by_name)
    for style_name in used_styles:
        style = by_name.get(style_name)
        if style is not None:
            font_name, _ = effective_font(
                style, text_variant="translated", use_font_replacements=use_font_replacements)
            if font_name:
                names.append(font_name)

    unique: dict[str, str] = {}
    for name in sorted(names, key=lambda n: (normalize_family(n), n)):
        unique.setdefault(normalize_family(name), name.strip().lstrip("@"))
    return [unique[k] for k in sorted(unique) if k]


@dataclass
class ResolvedFonts:
    fonts: list[FontFace]
    missing_families: list[str]


def resolve_manifest(
    required: list[str],
    attachments: FontRegistry | None,
    configured: FontRegistry | None,
) -> ResolvedFonts:
    context = FontContext(attachments=attachments, configured=configured)
    chosen: dict[str, FontFace] = {}
    if attachments is not None:
        for face in attachments.faces():
            chosen[face.id] = face
    missing: list[str] = []
    for family in required:
        faces = find_font_by_family(family, context)
        if not faces:
            missing.append(family)
        for face in faces:
            chosen.setdefault(face.id, face)

    def order(face: FontFace):
        return (face.source != "attachment", face.families[0].casefold(),
                face.bold, face.italic, face.filename.casefold(), face.id)

    return ResolvedFonts(sorted(chosen.values(), key=order), missing)
