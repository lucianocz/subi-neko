"""Font metadata registry for the QC preview (JASSUB / libass).

libass matches an ASS ``Fontname`` against the *internal* name-table records of
the font it is handed, never against the filename. This module mirrors that:
every font file is opened with fontTools and indexed by every family-ish name it
carries, on every platform/language record.

Name IDs indexed: 1 (family), 16 (typographic family), 21 (WWS family),
4 (full name), 6 (PostScript name). Full/PostScript names are included because
libass also resolves those (e.g. "ITC Stone Sans Std Medium" is only a full-name
style record on a Semibold file).

Two sources feed it (see ``find_font_by_family`` for precedence):

* ``attachment`` — fonts attached to a source MKV (``app.subs.font_attachments``)
* ``configured`` — the application font directory (``Settings.fonts_dir``)

Resource IDs are a hash of the font *bytes*, so they are stable while the bytes
are unchanged and safe to serve with immutable caching.

Everything is in memory; nothing is persisted. WOFF2 parsing needs the
``brotli`` package (fontTools raises otherwise; such a file is skipped).
"""
from __future__ import annotations

import hashlib
import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Literal

logger = logging.getLogger(__name__)

FontSource = Literal["attachment", "configured"]

FONT_MEDIA_TYPES: dict[str, str] = {
    ".ttf": "font/ttf",
    ".otf": "font/otf",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
}
SUPPORTED_FONT_EXTENSIONS = frozenset(FONT_MEDIA_TYPES)

# name-table IDs libass may match a requested font name against
_FAMILY_NAME_IDS = (1, 16, 21, 4, 6)


class FontParseError(ValueError):
    pass


def normalize_family(name: str) -> str:
    """Lookup key: casefolded, whitespace-collapsed, without the ASS vertical-text
    ``@`` prefix. Display names are kept separately."""
    return " ".join(name.strip().lstrip("@").split()).casefold()


@dataclass(frozen=True)
class FontFace:
    id: str                      # sha1(font bytes)[:20]
    source: FontSource
    path: Path                   # server-side only; never serialized to clients
    filename: str                # original filename (attachment name / on-disk name)
    media_type: str
    families: tuple[str, ...]    # display names, first = primary family
    bold: bool = False
    italic: bool = False
    size: int = 0
    project_id: int | None = None
    file_id: int | None = None

    @property
    def keys(self) -> frozenset[str]:
        return frozenset(normalize_family(n) for n in self.families)


def parse_font_file(
    path: Path,
    *,
    source: FontSource,
    filename: str | None = None,
    project_id: int | None = None,
    file_id: int | None = None,
) -> FontFace:
    """Read family names/style flags from a font file. Raises ``FontParseError``."""
    from fontTools.ttLib import TTFont

    filename = filename or path.name
    ext = path.suffix.lower()   # the on-disk name is authoritative (attachment names may lack one)
    if ext not in SUPPORTED_FONT_EXTENSIONS:
        raise FontParseError(f"Unsupported font type: {filename}")

    try:
        data = path.read_bytes()
        digest = hashlib.sha1(data).hexdigest()[:20]
        import io
        font = TTFont(io.BytesIO(data), lazy=True)
        try:
            name_table = font["name"]
            families: list[str] = []
            seen: set[str] = set()
            for name_id in _FAMILY_NAME_IDS:
                for record in name_table.names:
                    if record.nameID != name_id:
                        continue
                    try:
                        text = record.toUnicode().strip()
                    except Exception:  # undecodable legacy record
                        continue
                    key = normalize_family(text)
                    if key and key not in seen:
                        seen.add(key)
                        families.append(text)
            bold = italic = False
            if "OS/2" in font:
                sel = font["OS/2"].fsSelection
                italic, bold = bool(sel & 0x01), bool(sel & 0x20)
            elif "head" in font:
                mac = font["head"].macStyle
                bold, italic = bool(mac & 0x01), bool(mac & 0x02)
        finally:
            font.close()
    except FontParseError:
        raise
    except Exception as exc:  # fontTools raises many types for corrupt input
        raise FontParseError(f"Cannot parse font {filename}: {exc}") from exc

    if not families:
        raise FontParseError(f"Font {filename} has no usable family names")
    return FontFace(
        id=digest, source=source, path=path, filename=filename,
        media_type=FONT_MEDIA_TYPES[ext], families=tuple(families),
        bold=bold, italic=italic, size=len(data),
        project_id=project_id, file_id=file_id,
    )


def _face_sort_key(face: FontFace):
    return (face.families[0].casefold(), face.bold, face.italic, face.filename.casefold(), face.id)


class FontRegistry:
    """``normalized family name -> [FontFace]``. All faces of a family are kept
    (regular/bold/italic…); libass picks by the internal weight/slant itself."""

    def __init__(self, faces: Iterable[FontFace] = ()) -> None:
        self._by_id: dict[str, FontFace] = {}
        self._index: dict[str, list[FontFace]] = {}
        for face in faces:
            self.add(face)

    def add(self, face: FontFace) -> None:
        if face.id in self._by_id:      # identical bytes under another name: first wins
            return
        self._by_id[face.id] = face
        for key in face.keys:
            bucket = self._index.setdefault(key, [])
            bucket.append(face)
            bucket.sort(key=_face_sort_key)

    def get(self, font_id: str) -> FontFace | None:
        return self._by_id.get(font_id)

    def find(self, family_name: str) -> list[FontFace]:
        return list(self._index.get(normalize_family(family_name), ()))

    def faces(self) -> list[FontFace]:
        return sorted(self._by_id.values(), key=_face_sort_key)

    def __len__(self) -> int:
        return len(self._by_id)


def scan_font_directory(directory: Path, *, recursive: bool = True) -> FontRegistry:
    """Index every supported font under ``directory`` (may be missing/empty).
    A malformed file is logged and skipped."""
    registry = FontRegistry()
    if not directory.is_dir():
        return registry
    paths = directory.rglob("*") if recursive else directory.iterdir()
    for path in sorted(paths):
        if not path.is_file() or path.suffix.lower() not in SUPPORTED_FONT_EXTENSIONS:
            continue
        try:
            registry.add(parse_font_file(path, source="configured"))
        except FontParseError as exc:
            logger.warning("Skipping font %s: %s", path, exc)
    return registry


# --- configured-font registry (process-wide, rebuildable) ------------------

_configured: FontRegistry | None = None
_configured_lock = threading.Lock()


def get_configured_registry() -> FontRegistry:
    """Built lazily (and at startup); ``refresh_configured_registry`` rebuilds."""
    global _configured
    with _configured_lock:
        if _configured is None:
            _configured = _scan_configured()
        return _configured


def refresh_configured_registry() -> FontRegistry:
    global _configured
    registry = _scan_configured()
    with _configured_lock:
        _configured = registry
    return registry


def set_configured_registry(registry: FontRegistry | None) -> None:
    """Inject (tests) or clear the process-wide registry."""
    global _configured
    with _configured_lock:
        _configured = registry


def _scan_configured() -> FontRegistry:
    from app.core.config import settings
    registry = scan_font_directory(settings.fonts_dir)
    logger.info("Configured font registry: %d font(s) in %s", len(registry), settings.fonts_dir)
    return registry


# --- family matching --------------------------------------------------------

@dataclass
class FontContext:
    """Where a family may be resolved from, in priority order."""
    attachments: FontRegistry | None = None
    configured: FontRegistry | None = field(default=None)


def find_font_by_family(family_name: str, context: FontContext) -> list[FontFace]:
    """Faces for ``family_name`` (case-insensitive, internal names only).

    Priority: (1) the source MKV's attachments, (2) the configured font
    directory. The first source with any match wins *as a whole* — its faces are
    never mixed with the other source's, so an attached family keeps the file's
    own regular/bold set. No filename matching, no fontconfig-style aliasing
    (Arial is never silently Liberation Sans). Empty list = no match.

    Attachments outrank configured fonts because a release's own font is what
    the typesetter authored against; replacement fonts only fill gaps.
    """
    for registry in (context.attachments, context.configured):
        if registry is not None:
            faces = registry.find(family_name)
            if faces:
                return faces
    return []
