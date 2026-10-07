"""Render-time branding overlay for the translated ASS.

A branding template is a plain ``.ass`` file in ``<config>/brand``. It is never
imported into ``subtitle_events``; ``apply_branding`` merges its styles and
events into the already-built translated ``SSAFile`` (called only from
``build_ass``, so QC preview, translated download and Publish agree).

Merge policy
------------
* **Script Info**: the main subtitle's script info stays authoritative; nothing
  is copied from the template. The template's coordinates (``\\pos``, drawings,
  font sizes, margins) are therefore interpreted in the *main* script's
  PlayRes. No rescaling is done — a template authored for another PlayRes
  renders mis-scaled, which is logged as a warning (``playres_mismatch``).
* **Styles**: every template style is copied property-for-property under the
  name ``__brand__<name>``; template events (and ``\\r<style>`` resets inside
  their override blocks) are rewritten to match. Main styles are untouched.
* **Events**: copied unchanged (layer, actor, margins, effect, text, type) in
  template order, appended after the normal events, with
  ``start/end += start_offset_ms`` — a pure additive shift (no scaling,
  clipping or normalisation).
"""
from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass
from pathlib import Path

import pysubs2

logger = logging.getLogger(__name__)

BRAND_STYLE_PREFIX = "__brand__"
TEMPLATE_SUFFIX = ".ass"

_RESET_RE = re.compile(r"\\r([^\\}]*)")
_BLOCK_RE = re.compile(r"\{[^}]*\}")


class BrandingError(ValueError):
    """Template missing/unsafe/unparseable."""


@dataclass(frozen=True)
class BrandingSpec:
    template_filename: str
    start_offset_ms: int


def brand_dir() -> Path:
    from app.core.config import settings
    return settings.brand_dir


def spec_from_row(row) -> BrandingSpec | None:
    """The spec to render with: ``None`` unless a row exists, is enabled and
    names a template."""
    if row is None or not row.enabled or not row.template_filename:
        return None
    return BrandingSpec(row.template_filename, int(row.start_offset_ms or 0))


# ---------------------------------------------------------------------------
# Discovery / safe resolution
# ---------------------------------------------------------------------------

def _inside(root: Path, path: Path) -> bool:
    try:
        return path.is_file() and path.resolve().is_relative_to(root.resolve())
    except OSError:
        return False


def list_templates(root: Path | None = None) -> list[str]:
    """``*.ass`` filenames directly under the brand dir, sorted. Symlinks that
    leave the directory are not listed. No recursion, no paths."""
    root = root if root is not None else brand_dir()
    if not root.is_dir():
        return []
    return sorted(
        p.name for p in root.iterdir()
        if p.suffix.lower() == TEMPLATE_SUFFIX and _inside(root, p)
    )


def resolve_template(filename: str, root: Path | None = None) -> Path:
    """Absolute path of a listed template, or ``BrandingError``.

    Only a bare ``*.ass`` filename is accepted (no separators, drive letters or
    ``..``); the resolved target must still be a regular file inside the brand
    dir, so symlink escapes are rejected too.
    """
    root = root if root is not None else brand_dir()
    name = filename or ""
    if (
        not name or name in (".", "..") or "/" in name or "\\" in name or ":" in name
        or "\0" in name or Path(name).name != name
        or not name.lower().endswith(TEMPLATE_SUFFIX)
    ):
        raise BrandingError(f"Invalid branding template name: {filename!r}")
    candidate = root / name
    if not _inside(root, candidate):
        raise BrandingError(f"Branding template not found: {name}")
    return candidate.resolve()


# ---------------------------------------------------------------------------
# Parsing (cached on path + size + mtime so edits are picked up)
# ---------------------------------------------------------------------------

_cache: dict[Path, tuple[int, int, pysubs2.SSAFile]] = {}
_cache_lock = threading.Lock()


def load_template(filename: str, root: Path | None = None) -> pysubs2.SSAFile:
    """Parsed template. The returned object is shared — never mutate it."""
    path = resolve_template(filename, root)
    st = path.stat()
    key = (st.st_size, st.st_mtime_ns)
    with _cache_lock:
        hit = _cache.get(path)
        if hit is not None and hit[:2] == key:
            return hit[2]
    try:
        subs = pysubs2.load(str(path), encoding="utf-8-sig", format_="ass")
    except Exception as exc:  # parser raises assorted types
        raise BrandingError(f"Could not parse branding template {path.name}: {exc}") from exc
    with _cache_lock:
        _cache[path] = (*key, subs)
    return subs


# ---------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------

def _rewrite_resets(text: str, names: set[str]) -> str:
    def block(m: re.Match) -> str:
        def reset(r: re.Match) -> str:
            return f"\\r{BRAND_STYLE_PREFIX}{r.group(1)}" if r.group(1) in names else r.group(0)
        return _RESET_RE.sub(reset, m.group(0))
    return _BLOCK_RE.sub(block, text)


def playres_mismatch(main: pysubs2.SSAFile, template: pysubs2.SSAFile) -> bool:
    def res(s):
        return (s.info.get("PlayResX"), s.info.get("PlayResY"))
    return res(main)[0] is not None and res(template)[0] is not None and res(main) != res(template)


def apply_branding(subs: pysubs2.SSAFile, spec: BrandingSpec) -> None:
    """Merge the template into ``subs`` in place (see module docstring)."""
    template = load_template(spec.template_filename)
    if playres_mismatch(subs, template):
        logger.warning(
            "Branding template %s PlayRes %sx%s differs from the subtitle's %sx%s; "
            "branding is not rescaled",
            spec.template_filename,
            template.info.get("PlayResX"), template.info.get("PlayResY"),
            subs.info.get("PlayResX"), subs.info.get("PlayResY"),
        )
    names = set(template.styles)
    for name, style in template.styles.items():
        subs.styles[BRAND_STYLE_PREFIX + name] = style.copy()
    for src in template.events:
        ev = src.copy()
        if ev.style in names:
            ev.style = BRAND_STYLE_PREFIX + ev.style
        ev.text = _rewrite_resets(ev.text, names)
        ev.start = src.start + spec.start_offset_ms
        ev.end = src.end + spec.start_offset_ms
        subs.events.append(ev)


def branding_families(spec: BrandingSpec) -> list[str]:
    """Raw font families the template requests (style fonts + ``\\fn``)."""
    from app.subs.qc_fonts import families_in_text

    template = load_template(spec.template_filename)
    names = [s.fontname for s in template.styles.values() if s.fontname]
    for ev in template.events:
        names.extend(families_in_text(ev.text))
    return names
