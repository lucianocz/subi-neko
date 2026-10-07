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
* **Optional PlayRes scaling** (``scale_to_script_playres``): the template's
  ``\\pos``, ``\\move``, ``\\org`` coordinates, explicit ``\\fscx``/``\\fscy`` and
  the length tags ``\\fs`` (y), ``\\fsp`` (x), ``\\bord`` (y), ``\\xbord`` (x),
  ``\\ybord`` (y), ``\\shad`` (y), ``\\xshad`` (x), ``\\yshad`` (y) — also when
  nested in ``\\t(...)`` — are multiplied by ``target PlayRes / template PlayRes`` (independently
  per axis). Every template style is scaled the same way: Fontsize, Outline,
  Shadow (y), Spacing (x) and MarginL/MarginR (x), MarginV (y); the alignment
  number itself is a position code, not a length, so it is kept. Nothing else
  is touched — in particular drawing payloads after
  ``\\p`` and the ``\\pN`` level stay byte-for-byte as authored. Missing or
  non-positive PlayRes on either side raises ``BrandingError`` (no silent
  fallback to unscaled output).
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
    scale_to_script_playres: bool = False


def brand_dir() -> Path:
    from app.core.config import settings
    return settings.brand_dir


def spec_from_row(row) -> BrandingSpec | None:
    """The spec to render with: ``None`` unless a row exists, is enabled and
    names a template."""
    if row is None or not row.enabled or not row.template_filename:
        return None
    return BrandingSpec(
        row.template_filename, int(row.start_offset_ms or 0),
        bool(getattr(row, "scale_to_script_playres", 0)))


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


# --- optional PlayRes scaling (position + explicit scale tags only) ----------

_NUM = r"\s*(-?\d+(?:\.\d+)?|-?\.\d+)\s*"
_POS_RE = re.compile(r"\\pos\(" + _NUM + "," + _NUM + r"\)")
_ORG_RE = re.compile(r"\\org\(" + _NUM + "," + _NUM + r"\)")
_MOVE_RE = re.compile(r"\\move\(([^()]*)\)")
_FSC_RE = re.compile(r"\\fsc([xy])(-?\d+(?:\.\d+)?|-?\.\d+)")
# Length-like tags and the axis they scale with (x = horizontal, y = vertical).
_LENGTH_AXIS = {
    "fs": "y", "fsp": "x", "bord": "y", "xbord": "x", "ybord": "y",
    "shad": "y", "xshad": "x", "yshad": "y",
}
_LENGTH_RE = re.compile(
    r"\\(fsp|fs|bord|xbord|ybord|shad|xshad|yshad)(-?\d+(?:\.\d+)?|-?\.\d+)")
_NUMBER_RE = re.compile(r"^\s*(-?\d+(?:\.\d+)?|-?\.\d+)\s*$")


def _fmt(value: float) -> str:
    """Up to 6 decimals, no trailing zeros (``8``, ``278.333333``)."""
    out = f"{value:.6f}".rstrip("0").rstrip(".")
    return "0" if out in ("", "-0") else out


def _scale_move_args(args: str, sx: float, sy: float) -> str | None:
    parts = args.split(",")
    if len(parts) < 4 or not all(_NUMBER_RE.match(p) for p in parts[:4]):
        return None  # not a well-formed \move: leave it alone
    factors = (sx, sy, sx, sy)
    scaled = [_fmt(float(p) * f) for p, f in zip(parts[:4], factors)]
    return ",".join(scaled + parts[4:])


def _scale_block(block: str, sx: float, sy: float) -> str:
    block = _POS_RE.sub(
        lambda m: f"\\pos({_fmt(float(m[1]) * sx)},{_fmt(float(m[2]) * sy)})", block)
    block = _ORG_RE.sub(
        lambda m: f"\\org({_fmt(float(m[1]) * sx)},{_fmt(float(m[2]) * sy)})", block)

    def move(m: re.Match) -> str:
        scaled = _scale_move_args(m[1], sx, sy)
        return m[0] if scaled is None else f"\\move({scaled})"

    block = _MOVE_RE.sub(move, block)
    block = _LENGTH_RE.sub(
        lambda m: f"\\{m[1]}{_fmt(float(m[2]) * (sx if _LENGTH_AXIS[m[1]] == 'x' else sy))}", block)
    return _FSC_RE.sub(
        lambda m: f"\\fsc{m[1]}{_fmt(float(m[2]) * (sx if m[1] == 'x' else sy))}", block)


def scale_branding_tags(text: str, sx: float, sy: float) -> str:
    """Scale ``\\pos``/``\\move``/``\\org`` coordinates and ``\\fscx``/``\\fscy``
    values inside ``{...}`` override blocks only. Text outside blocks (including
    drawing payloads) and every other tag are returned unchanged."""
    return _BLOCK_RE.sub(lambda m: _scale_block(m[0], sx, sy), text)


def scale_branding_style(style: pysubs2.SSAStyle, sx: float, sy: float) -> None:
    """Scale a copied template style's lengths in place (see module docstring)."""
    style.fontsize = round(style.fontsize * sy, 6)
    style.outline = round(style.outline * sy, 6)
    style.shadow = round(style.shadow * sy, 6)
    style.spacing = round(style.spacing * sx, 6)
    style.marginl = round(style.marginl * sx)
    style.marginr = round(style.marginr * sx)
    style.marginv = round(style.marginv * sy)


def _play_res(subs: pysubs2.SSAFile, what: str) -> tuple[float, float]:
    try:
        x, y = float(subs.info["PlayResX"]), float(subs.info["PlayResY"])
    except (KeyError, ValueError):
        raise BrandingError(
            f"Cannot scale branding: the {what} has no valid PlayResX/PlayResY") from None
    if not (x > 0 and y > 0):
        raise BrandingError(
            f"Cannot scale branding: the {what} PlayRes {x:g}x{y:g} is not positive")
    return x, y


def playres_mismatch(main: pysubs2.SSAFile, template: pysubs2.SSAFile) -> bool:
    def res(s):
        return (s.info.get("PlayResX"), s.info.get("PlayResY"))
    return res(main)[0] is not None and res(template)[0] is not None and res(main) != res(template)


def apply_branding(subs: pysubs2.SSAFile, spec: BrandingSpec) -> None:
    """Merge the template into ``subs`` in place (see module docstring)."""
    template = load_template(spec.template_filename)
    scale: tuple[float, float] | None = None
    if spec.scale_to_script_playres:
        tx, ty = _play_res(template, f"branding template {spec.template_filename}")
        mx, my = _play_res(subs, "subtitle script")
        if (tx, ty) != (mx, my):
            scale = (mx / tx, my / ty)
    elif playres_mismatch(subs, template):
        logger.warning(
            "Branding template %s PlayRes %sx%s differs from the subtitle's %sx%s; "
            "branding is not rescaled",
            spec.template_filename,
            template.info.get("PlayResX"), template.info.get("PlayResY"),
            subs.info.get("PlayResX"), subs.info.get("PlayResY"),
        )
    names = set(template.styles)
    for name, style in template.styles.items():
        copied = style.copy()
        if scale is not None:
            scale_branding_style(copied, *scale)
        subs.styles[BRAND_STYLE_PREFIX + name] = copied
    for src in template.events:
        ev = src.copy()
        if ev.style in names:
            ev.style = BRAND_STYLE_PREFIX + ev.style
        ev.text = _rewrite_resets(ev.text, names)
        if scale is not None:
            ev.text = scale_branding_tags(ev.text, *scale)
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
