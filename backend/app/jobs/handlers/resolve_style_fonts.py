from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from sqlalchemy import select

from app.core.database import SyncSessionLocal
from app.db.models import File, SubtitleStyle, file_subtitle_styles
from app.jobs.context import JobContext, JobResult, ProgressFn
from app.jobs.registry import register_job_handler

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Language detection
# Language codes (ISO 639-1 and ISO 639-2) that require Latin Extended A
# glyph coverage (characters like č, ř, ž, ě, etc.)
# ---------------------------------------------------------------------------
_LATIN_EXTENDED_REQUIRED: frozenset[str] = frozenset({
    # ISO 639-1
    "cs", "sk", "pl", "hu", "ro", "hr", "sl", "lt", "lv", "et",
    "bs", "sq", "mk", "uk", "be",
    # ISO 639-2 (terminological / bibliographic)
    "ces", "cze", "slk", "slo", "pol", "hun", "ron", "rum",
    "hrv", "scr", "slv", "lit", "lav", "est", "bos", "sqi", "alb",
    "mkd", "mac", "ukr", "bel",
})


def _needs_latin_extended(lang_code: str) -> bool:
    return lang_code.lower().strip() in _LATIN_EXTENDED_REQUIRED


# ---------------------------------------------------------------------------
# Font safe-list — fonts with confirmed Latin Extended A coverage
# ---------------------------------------------------------------------------
_LATIN_EXTENDED_SAFE: frozenset[str] = frozenset({
    # Core Windows / macOS system fonts
    "arial", "arial black", "arial narrow",
    "times new roman", "courier new",
    "verdana", "georgia", "tahoma", "trebuchet ms",
    "impact", "comic sans ms",
    "palatino linotype", "book antiqua",
    "calibri", "cambria", "candara", "consolas", "constantia", "corbel",
    "franklin gothic medium", "garamond",
    "lucida console", "lucida sans unicode",
    "microsoft sans serif", "segoe ui",
    # Linux / open-source fonts
    "liberation sans", "liberation serif", "liberation mono",
    "noto sans", "noto serif", "noto mono",
    "dejavu sans", "dejavu serif", "dejavu sans mono",
    "freesans", "freeserif", "freemono",
    "ubuntu", "ubuntu mono",
    # Web / Google fonts commonly bundled
    "open sans", "roboto", "roboto mono", "lato", "montserrat",
    "source sans pro", "source sans 3", "source serif pro",
    "pt sans", "pt serif", "pt mono",
    "merriweather", "raleway", "oswald",
})

# ---------------------------------------------------------------------------
# x-height ratios (x-height / em-size) for common fonts
# Used to produce visually equivalent replacement font sizes.
# Formula: replacement_size = source_size * (source_ratio / replacement_ratio)
# ---------------------------------------------------------------------------
_X_HEIGHT_RATIO: dict[str, float] = {
    # Sans-serif
    "arial": 0.519,
    "arial black": 0.519,
    "arial narrow": 0.519,
    "helvetica": 0.522,
    "helvetica neue": 0.517,
    "verdana": 0.546,
    "tahoma": 0.525,
    "trebuchet ms": 0.534,
    "impact": 0.590,
    "comic sans ms": 0.475,
    "calibri": 0.500,
    "candara": 0.502,
    "corbel": 0.519,
    "microsoft sans serif": 0.519,
    "segoe ui": 0.510,
    "franklin gothic medium": 0.510,
    "century gothic": 0.466,
    "futura": 0.468,
    "gill sans": 0.476,
    "myriad pro": 0.502,
    "open sans": 0.533,
    "roboto": 0.528,
    "roboto condensed": 0.528,
    "lato": 0.520,
    "montserrat": 0.477,
    "raleway": 0.472,
    "oswald": 0.517,
    "liberation sans": 0.519,
    "noto sans": 0.522,
    "dejavu sans": 0.527,
    "dejavu sans mono": 0.463,
    "freesans": 0.519,
    "ubuntu": 0.511,
    "pt sans": 0.514,
    "source sans pro": 0.486,
    "source sans 3": 0.486,
    # Serif
    "times new roman": 0.448,
    "georgia": 0.481,
    "palatino linotype": 0.442,
    "palatino": 0.442,
    "book antiqua": 0.442,
    "garamond": 0.410,
    "adobe garamond": 0.410,
    "adobe garamond pro": 0.410,
    "adobe garamond pro bold": 0.410,
    "itc garamond": 0.415,
    "cormorant garamond": 0.410,
    "eb garamond": 0.410,
    "cambria": 0.462,
    "constantia": 0.462,
    "bookman": 0.440,
    "bookman old style": 0.440,
    "minion pro": 0.430,
    "merriweather": 0.463,
    "liberation serif": 0.448,
    "noto serif": 0.463,
    "dejavu serif": 0.461,
    "freeserif": 0.448,
    "pt serif": 0.449,
    "source serif pro": 0.440,
    "georgia pro": 0.481,
    # Monospace
    "courier new": 0.426,
    "consolas": 0.470,
    "lucida console": 0.441,
    "liberation mono": 0.426,
    "noto mono": 0.470,
    "freemono": 0.426,
    "roboto mono": 0.502,
    "ubuntu mono": 0.490,
    "pt mono": 0.470,
}

_DEFAULT_X_HEIGHT = 0.500  # fallback for unknown fonts


# ---------------------------------------------------------------------------
# Font category classification → automatic replacement font selection
# ---------------------------------------------------------------------------
_SANS_SERIF_FONTS: frozenset[str] = frozenset({
    "arial", "arial black", "arial narrow",
    "helvetica", "helvetica neue",
    "verdana", "tahoma", "trebuchet ms", "impact", "comic sans ms",
    "calibri", "candara", "corbel", "segoe ui", "microsoft sans serif",
    "franklin gothic medium", "century gothic", "futura", "gill sans",
    "myriad pro",
    "open sans", "roboto", "roboto condensed", "lato", "montserrat",
    "raleway", "oswald",
    "liberation sans", "noto sans", "dejavu sans",
    "freesans", "ubuntu", "pt sans", "source sans pro", "source sans 3",
})

_SERIF_FONTS: frozenset[str] = frozenset({
    "times new roman", "georgia",
    "palatino linotype", "palatino", "book antiqua",
    "garamond", "adobe garamond", "adobe garamond pro", "adobe garamond pro bold",
    "itc garamond", "cormorant garamond", "eb garamond",
    "cambria", "constantia",
    "bookman", "bookman old style",
    "minion pro", "merriweather",
    "liberation serif", "noto serif", "dejavu serif", "freeserif",
    "pt serif", "source serif pro",
})

_MONOSPACE_FONTS: frozenset[str] = frozenset({
    "courier new", "consolas", "lucida console",
    "liberation mono", "noto mono", "freemono",
    "roboto mono", "ubuntu mono", "pt mono",
    "dejavu sans mono",
})

# Replacement font chosen by category
_REPLACEMENT_BY_CATEGORY: dict[str, str] = {
    "sans-serif": "Arial",
    "serif": "Times New Roman",
    "monospace": "Courier New",
}


def _classify_font(font_name: str) -> str:
    """Return 'sans-serif', 'serif', or 'monospace' for the given font name."""
    lower = font_name.lower().strip()
    if lower in _MONOSPACE_FONTS:
        return "monospace"
    if lower in _SERIF_FONTS:
        return "serif"
    if lower in _SANS_SERIF_FONTS:
        return "sans-serif"
    # Heuristics for unknown fonts
    if any(kw in lower for kw in ("mono", "code", "console", "typewriter")):
        return "monospace"
    if "sans" in lower:
        return "sans-serif"
    if "serif" in lower:
        return "serif"
    # Default: sans-serif
    return "sans-serif"


def _font_x_height(font_name: str) -> float:
    return _X_HEIGHT_RATIO.get(font_name.lower().strip(), _DEFAULT_X_HEIGHT)


def _compute_replacement_size(source_font: str, source_size: float, target_font: str) -> float:
    """Return the replacement font size that matches the visual x-height of the source."""
    src_ratio = _font_x_height(source_font)
    tgt_ratio = _font_x_height(target_font)
    return round(source_size * (src_ratio / tgt_ratio), 1)


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------

@register_job_handler("resolve_style_fonts")
def resolve_style_fonts(
    payload: dict[str, Any],
    ctx: JobContext,
    progress: ProgressFn,
) -> JobResult:
    file_id: int = payload["file_id"]
    now = datetime.utcnow().isoformat()

    lang_code = (ctx.options.target_lang_code or "").strip()

    progress(0.05, "Loading subtitle styles")

    with SyncSessionLocal() as session:
        file = session.get(File, file_id)
        if file is None:
            return JobResult(status="failed", result=None,
                             error_code="FILE_NOT_FOUND",
                             error_message=f"File id={file_id} not found")

        styles = list(session.scalars(
            select(SubtitleStyle)
            .join(file_subtitle_styles, file_subtitle_styles.c.subtitle_style_id == SubtitleStyle.id)
            .where(
                file_subtitle_styles.c.file_id == file_id,
                # Shared styles that were already resolved (or hand-tuned in the
                # style editor) must not be overwritten by another file's run.
                SubtitleStyle.font_check_status == "unchecked",
            )
        ).all())
        style_data = [
            {"id": s.id, "font_name": s.font_name, "font_size": s.font_size}
            for s in styles
        ]

    if not style_data:
        return JobResult(status="succeeded",
                         result={"styles_checked": 0, "styles_replaced": 0},
                         error_code=None, error_message=None)

    requires_extended = _needs_latin_extended(lang_code) if lang_code else False

    progress(0.2, f"Checking {len(style_data)} styles "
                  f"(lang={lang_code or 'not set'}, check={'yes' if requires_extended else 'no'})")

    updates: list[dict] = []
    replaced = 0

    for sd in style_data:
        font_name: str = sd["font_name"]
        font_size: float = sd["font_size"]
        font_lower = font_name.lower().strip()

        if not requires_extended or font_lower in _LATIN_EXTENDED_SAFE:
            updates.append({
                "id": sd["id"],
                "status": "supported",
                "replacement_font_name": None,
                "replacement_font_size": None,
            })
        else:
            category = _classify_font(font_name)
            replacement_font = _REPLACEMENT_BY_CATEGORY[category]
            new_size = _compute_replacement_size(font_name, font_size, replacement_font)
            updates.append({
                "id": sd["id"],
                "status": "replaced",
                "replacement_font_name": replacement_font,
                "replacement_font_size": new_size,
            })
            replaced += 1
            logger.debug(
                "Font replacement: '%s' %.1f → '%s' (category=%s) %.1f",
                font_name, font_size, replacement_font, category, new_size,
            )

    progress(0.75, f"Writing results ({replaced} replaced, {len(style_data) - replaced} supported)")

    with SyncSessionLocal() as session:
        for u in updates:
            style = session.get(SubtitleStyle, u["id"])
            if style is None:
                continue
            style.font_check_status = u["status"]
            style.replacement_font_name = u["replacement_font_name"]
            style.replacement_font_size = u["replacement_font_size"]
            style.updated_at = now
        session.commit()

    progress(1.0, "Done")
    return JobResult(
        status="succeeded",
        result={"styles_checked": len(style_data), "styles_replaced": replaced},
        error_code=None,
        error_message=None,
    )
