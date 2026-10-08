"""Language catalog helpers shared by options and subtitle selection.

The options UI stores a language as a two-letter ISO 639-1 code plus its
display name (the frontend catalog in ``OptionsDrawer.tsx``). MKV track
metadata uses ISO 639-2 (``language``: eng/jpn/ger…) and, in newer files,
BCP 47 (``language_ietf``: en/ja/de…), so matching a track needs this mapping.
Keep the codes here in step with that catalog.
"""
from __future__ import annotations

# 639-1 -> (display name, ISO 639-2 codes: bibliographic and terminological)
_CATALOG: dict[str, tuple[str, tuple[str, ...]]] = {
    "ar": ("Arabic", ("ara",)),
    "ca": ("Catalan", ("cat",)),
    "zh": ("Chinese", ("chi", "zho")),
    "hr": ("Croatian", ("hrv",)),
    "cs": ("Czech", ("cze", "ces")),
    "da": ("Danish", ("dan",)),
    "nl": ("Dutch", ("dut", "nld")),
    "en": ("English", ("eng",)),
    "fi": ("Finnish", ("fin",)),
    "fr": ("French", ("fre", "fra")),
    "de": ("German", ("ger", "deu")),
    "el": ("Greek", ("gre", "ell")),
    "hu": ("Hungarian", ("hun",)),
    "id": ("Indonesian", ("ind",)),
    "it": ("Italian", ("ita",)),
    "ja": ("Japanese", ("jpn",)),
    "ko": ("Korean", ("kor",)),
    "no": ("Norwegian", ("nor", "nob", "nno")),
    "pl": ("Polish", ("pol",)),
    "pt": ("Portuguese", ("por",)),
    "ro": ("Romanian", ("rum", "ron")),
    "ru": ("Russian", ("rus",)),
    "sr": ("Serbian", ("srp",)),
    "sk": ("Slovak", ("slo", "slk")),
    "sl": ("Slovenian", ("slv",)),
    "es": ("Spanish", ("spa",)),
    "sv": ("Swedish", ("swe",)),
    "tr": ("Turkish", ("tur",)),
    "uk": ("Ukrainian", ("ukr",)),
    "vi": ("Vietnamese", ("vie",)),
}

DEFAULT_SOURCE_LANG_CODE = "en"
DEFAULT_SOURCE_LANG_NAME = "English"


def normalize_lang_code(code: str | None) -> str | None:
    """Catalog 639-1 code for ``code`` (case-insensitive), or None if unknown."""
    key = (code or "").strip().lower()
    return key if key in _CATALOG else None


def language_name(code: str) -> str | None:
    entry = _CATALOG.get(code)
    return entry[0] if entry else None


def track_matches_language(properties: dict, code: str) -> bool:
    """Whether an mkvmerge track's language metadata is the 639-1 ``code``."""
    entry = _CATALOG.get(code)
    if entry is None:
        return False
    ietf = (properties.get("language_ietf") or "").strip().lower()
    if ietf and ietf.split("-")[0] == code:
        return True
    return (properties.get("language") or "").strip().lower() in entry[1]
