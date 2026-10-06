"""
Options helper -- thin key/value store backed by the `options` table.

Sync API  (for job handlers running in threads):
    options.get(name, default)
    options.set(name, value)
    options.snapshot() -> AppOptions

Async API (for FastAPI routes):
    await options.aget(name, default)
    await options.aset(name, value)
    await options.asnapshot() -> AppOptions

Cache:
    All rows are loaded into memory on first access and kept until
    invalidated.  Writes update the cache immediately.
    Call options.invalidate() to force a full reload, or
    options.invalidate(name) to drop a single key.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.core.database import AsyncSessionLocal, SyncSessionLocal
from app.db.default_prompts import (
    DEFAULT_ANALYZE_PROMPT,
    DEFAULT_FINAL_QA_PROMPT,
    DEFAULT_MAPPING_PROMPT,
    DEFAULT_POLISH_PROMPT,
    DEFAULT_REPAIR_PROMPT,
    DEFAULT_STYLE_BIBLE_PROMPT,
    DEFAULT_STYLE_BIBLE_UPDATE_PROMPT,
    DEFAULT_TRANSLATION_PROMPT,
    DEFAULT_SIGN_TRANSLATION_PROMPT,
    DEFAULT_SONG_TRANSLATION_PROMPT,
)
from app.db.models import Option
from app.db.output_state import touch_all_outputs_sync


# -- Typed options -------------------------------------------------------------

_VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
_VALID_STRUCTURED_OUTPUT_MODES = {"auto", "json_schema", "json_object", "text"}


@dataclass
class AppOptions:
    target_lang_name: str | None = None
    target_lang_code: str | None = None
    chunk_size: int = 100
    prepend_context_size: int = 10
    # Source-only lines shown after a chunk's targets, so the tail of every
    # chunk isn't translated blind to what follows. 0 disables.
    lookahead_context_size: int = 5
    openai_api_base: str = "https://api.openai.com/v1"
    openai_api_key: str | None = None
    openai_model_cheap: str = "gpt-5.4-mini"
    openai_model_better: str = "gpt-5.6-terra"
    llm_structured_outputs: str = "auto"
    llm_prices_json: str | None = None
    llm_max_completion_tokens: int = 32768
    translate_karaoke: bool = False
    # Translated ASS uses a style's replacement_font_* (when set) instead of
    # the source font. Off ignores them without clearing the stored values.
    replace_incompatible_fonts: bool = True
    require_style_bible: bool = True
    # HARD reading-speed limit: drives QA `high_cps` and the red CPS styling.
    cps_limit: float = 20.0
    # SOFT limit: editor-only orange warning band (soft < CPS <= hard); never
    # creates QA. Effective value is clamped below cps_limit (see from_dict).
    soft_cps_limit: float = 18.0
    max_row_chars: int = 42
    # Deterministically rebalance over-long dialogue rows with \N before the
    # readability check (long_row then only flags what a split can't fix).
    auto_line_break: bool = True
    # Remove dialogue line breaks that aren't needed: joined text must fit
    # min(join_lines_under, max_row_chars). Same reflow pass as auto_line_break.
    auto_join_short_lines: bool = True
    join_lines_under: int = 45
    auto_accept_policy: str = "fully_clean"  # manual | fully_clean | no_blockers
    auto_mapping_accept_threshold: float = 0.8
    translation_confidence_flag_threshold: float = 0.55
    log_level: str = "INFO"
    job_worker_count: int = 4
    mapping_character_description_max: int = 400
    mapping_character_description_budget: int = 24000
    style_bible_character_description_max: int = 600
    style_bible_character_description_budget: int = 32000
    translation_prompt: str = DEFAULT_TRANSLATION_PROMPT
    repair_prompt: str = DEFAULT_REPAIR_PROMPT
    polish_prompt: str = DEFAULT_POLISH_PROMPT
    final_qa_prompt: str = DEFAULT_FINAL_QA_PROMPT
    sign_translation_prompt: str = DEFAULT_SIGN_TRANSLATION_PROMPT
    song_translation_prompt: str = DEFAULT_SONG_TRANSLATION_PROMPT
    analyze_prompt: str = DEFAULT_ANALYZE_PROMPT
    mapping_prompt: str = DEFAULT_MAPPING_PROMPT
    style_bible_prompt: str = DEFAULT_STYLE_BIBLE_PROMPT
    style_bible_update_prompt: str = DEFAULT_STYLE_BIBLE_UPDATE_PROMPT

    @classmethod
    def from_dict(cls, d: dict[str, str | None]) -> "AppOptions":
        raw_chunk_size = d.get("CHUNK_SIZE")
        raw_context_size = d.get("PREPEND_CONTEXT_SIZE")
        cps_limit = _validated_positive_float(d.get("CPS_LIMIT"), "CPS_LIMIT", 20.0)
        soft_cps_limit = _validated_positive_float(d.get("SOFT_CPS_LIMIT"), "SOFT_CPS_LIMIT", 18.0)
        if soft_cps_limit >= cps_limit:
            # Stored hard limit below the (default) soft one: collapse the
            # warning band instead of mutating either stored value.
            soft_cps_limit = cps_limit
        return cls(
            target_lang_name=d.get("TARGET_LANG_NAME"),
            target_lang_code=d.get("TARGET_LANG_CODE"),
            chunk_size=int(raw_chunk_size) if raw_chunk_size is not None else 100,
            prepend_context_size=int(raw_context_size) if raw_context_size is not None else 10,
            lookahead_context_size=_validated_non_negative_int(
                d.get("LOOKAHEAD_CONTEXT_SIZE"), "LOOKAHEAD_CONTEXT_SIZE", 5),
            openai_api_base=d.get("OPENAI_API_BASE") or "https://api.openai.com/v1",
            openai_api_key=d.get("OPENAI_API_KEY"),
            openai_model_cheap=d.get("OPENAI_MODEL_CHEAP") or "gpt-5.4-mini",
            openai_model_better=d.get("OPENAI_MODEL_BETTER") or "gpt-5.6-terra",
            llm_structured_outputs=_validated_structured_outputs(d.get("LLM_STRUCTURED_OUTPUTS")),
            llm_prices_json=d.get("LLM_PRICES_JSON"),
            llm_max_completion_tokens=_validated_positive_int(
                d.get("LLM_MAX_COMPLETION_TOKENS"), "LLM_MAX_COMPLETION_TOKENS", 32768),
            translate_karaoke=_validated_bool(d.get("TRANSLATE_KARAOKE")),
            replace_incompatible_fonts=_validated_bool_default_true(
                d.get("REPLACE_INCOMPATIBLE_FONTS")),
            require_style_bible=_validated_bool_default_true(d.get("REQUIRE_STYLE_BIBLE")),
            cps_limit=cps_limit,
            soft_cps_limit=soft_cps_limit,
            max_row_chars=_validated_positive_int(d.get("MAX_ROW_CHARS"), "MAX_ROW_CHARS", 42),
            auto_line_break=_validated_bool_default_true(d.get("AUTO_LINE_BREAK")),
            auto_join_short_lines=_validated_bool_default_true(d.get("AUTO_JOIN_SHORT_LINES")),
            join_lines_under=_validated_positive_int(d.get("JOIN_LINES_UNDER"), "JOIN_LINES_UNDER", 45),
            auto_accept_policy=_validated_auto_accept_policy(d.get("AUTO_ACCEPT_POLICY")),
            auto_mapping_accept_threshold=_validated_ratio(
                d.get("AUTO_MAPPING_ACCEPT_THRESHOLD"), "AUTO_MAPPING_ACCEPT_THRESHOLD", 0.8),
            translation_confidence_flag_threshold=_validated_ratio(
                d.get("TRANSLATION_CONFIDENCE_FLAG_THRESHOLD"),
                "TRANSLATION_CONFIDENCE_FLAG_THRESHOLD", 0.55),
            log_level=_validated_log_level(d.get("LOG_LEVEL")),
            job_worker_count=_validated_worker_count(d.get("JOB_WORKER_COUNT")),
            mapping_character_description_max=_validated_bounded_non_negative_int(
                d.get("MAPPING_CHARACTER_DESCRIPTION_MAX"),
                "MAPPING_CHARACTER_DESCRIPTION_MAX", 400, 4000),
            mapping_character_description_budget=_validated_bounded_non_negative_int(
                d.get("MAPPING_CHARACTER_DESCRIPTION_BUDGET"),
                "MAPPING_CHARACTER_DESCRIPTION_BUDGET", 24000, 200000),
            style_bible_character_description_max=_validated_bounded_non_negative_int(
                d.get("STYLE_BIBLE_CHARACTER_DESCRIPTION_MAX"),
                "STYLE_BIBLE_CHARACTER_DESCRIPTION_MAX", 600, 4000),
            style_bible_character_description_budget=_validated_bounded_non_negative_int(
                d.get("STYLE_BIBLE_CHARACTER_DESCRIPTION_BUDGET"),
                "STYLE_BIBLE_CHARACTER_DESCRIPTION_BUDGET", 32000, 200000),
            translation_prompt=d.get("TRANSLATION_PROMPT") or DEFAULT_TRANSLATION_PROMPT,
            repair_prompt=d.get("REPAIR_PROMPT") or DEFAULT_REPAIR_PROMPT,
            polish_prompt=d.get("POLISH_PROMPT") or DEFAULT_POLISH_PROMPT,
            final_qa_prompt=d.get("FINAL_QA_PROMPT") or DEFAULT_FINAL_QA_PROMPT,
            sign_translation_prompt=d.get("SIGN_TRANSLATION_PROMPT") or DEFAULT_SIGN_TRANSLATION_PROMPT,
            song_translation_prompt=d.get("SONG_TRANSLATION_PROMPT") or DEFAULT_SONG_TRANSLATION_PROMPT,
            analyze_prompt=d.get("ANALYZE_PROMPT") or DEFAULT_ANALYZE_PROMPT,
            mapping_prompt=d.get("MAPPING_PROMPT") or DEFAULT_MAPPING_PROMPT,
            style_bible_prompt=d.get("STYLE_BIBLE_PROMPT") or DEFAULT_STYLE_BIBLE_PROMPT,
            style_bible_update_prompt=(
                d.get("STYLE_BIBLE_UPDATE_PROMPT") or DEFAULT_STYLE_BIBLE_UPDATE_PROMPT),
        )

    def _resolve(self, prompt: str) -> str:
        lang = self.target_lang_name or "the target language"
        return prompt.replace("{TARGET_LANG_NAME}", lang)

    def resolved_translation_prompt(self) -> str:
        return self._resolve(self.translation_prompt)

    def resolved_repair_prompt(self) -> str:
        return self._resolve(self.repair_prompt)

    def resolved_polish_prompt(self) -> str:
        return self._resolve(self.polish_prompt)

    def resolved_final_qa_prompt(self) -> str:
        return self._resolve(self.final_qa_prompt)

    def resolved_sign_translation_prompt(self) -> str:
        return self._resolve(self.sign_translation_prompt)

    def resolved_song_translation_prompt(self) -> str:
        return self._resolve(self.song_translation_prompt)

    def resolved_analyze_prompt(self) -> str:
        return self._resolve(self.analyze_prompt)

    def resolved_mapping_prompt(self) -> str:
        # Language-neutral by design — resolved anyway so a user-supplied
        # prompt may use the placeholder if they want to.
        return self._resolve(self.mapping_prompt)

    def resolved_style_bible_prompt(self) -> str:
        return self._resolve(self.style_bible_prompt)

    def resolved_style_bible_update_prompt(self) -> str:
        return self._resolve(self.style_bible_update_prompt)


# -- Validation helpers --------------------------------------------------------

_logger = logging.getLogger(__name__)


def _validated_log_level(raw: str | None) -> str:
    if raw is None:
        return "INFO"
    normalized = raw.strip().upper()
    if normalized not in _VALID_LOG_LEVELS:
        _logger.warning("Invalid LOG_LEVEL %r, falling back to INFO", raw)
        return "INFO"
    return normalized


def _validated_structured_outputs(raw: str | None) -> str:
    if raw is None:
        return "auto"
    normalized = raw.strip().lower()
    if normalized not in _VALID_STRUCTURED_OUTPUT_MODES:
        _logger.warning("Invalid LLM_STRUCTURED_OUTPUTS %r, falling back to auto", raw)
        return "auto"
    return normalized


def _validated_bool(raw: str | None) -> bool:
    if raw is None:
        return False
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _validated_bool_default_true(raw: str | None) -> bool:
    """Like _validated_bool but defaults to True when key is absent."""
    if raw is None:
        return True
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _validated_positive_int(raw: str | None, name: str, default: int) -> int:
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        _logger.warning("Invalid %s %r, falling back to %d", name, raw, default)
        return default
    if value <= 0:
        _logger.warning("%s must be > 0, got %d, falling back to %d", name, value, default)
        return default
    return value


def _validated_non_negative_int(raw: str | None, name: str, default: int) -> int:
    """Like _validated_positive_int but 0 is a legal value (feature off)."""
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        _logger.warning("Invalid %s %r, falling back to %d", name, raw, default)
        return default
    if value < 0:
        _logger.warning("%s must be >= 0, got %d, falling back to %d", name, value, default)
        return default
    return value


def _validated_bounded_non_negative_int(
    raw: str | None, name: str, default: int, maximum: int,
) -> int:
    value = _validated_non_negative_int(raw, name, default)
    if value > maximum:
        _logger.warning("%s must be <= %d, got %d, falling back to %d",
                        name, maximum, value, default)
        return default
    return value


def _validated_positive_float(raw: str | None, name: str, default: float) -> float:
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        _logger.warning("Invalid %s %r, falling back to %s", name, raw, default)
        return default
    if value <= 0:
        _logger.warning("%s must be > 0, got %s, falling back to %s", name, value, default)
        return default
    return value


_VALID_AUTO_ACCEPT_POLICIES = {"manual", "fully_clean", "no_blockers"}


def _validated_auto_accept_policy(raw: str | None) -> str:
    if raw is None:
        return "fully_clean"
    normalized = raw.strip().lower()
    if normalized not in _VALID_AUTO_ACCEPT_POLICIES:
        _logger.warning("Invalid AUTO_ACCEPT_POLICY %r, falling back to fully_clean", raw)
        return "fully_clean"
    return normalized


def _validated_ratio(raw: str | None, name: str, default: float) -> float:
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        _logger.warning("Invalid %s %r, falling back to %s", name, raw, default)
        return default
    if value < 0.0 or value > 1.0:
        _logger.warning("%s must be in [0,1], got %r, falling back to %s", name, raw, default)
        return default
    return value


def _validated_worker_count(raw: str | None) -> int:
    if raw is None:
        return 4
    try:
        n = int(raw)
    except ValueError:
        _logger.warning("Invalid JOB_WORKER_COUNT %r, falling back to 4", raw)
        return 4
    if n < 1:
        _logger.warning("JOB_WORKER_COUNT must be >= 1, got %d, falling back to 4", n)
        return 4
    if n > 32:
        _logger.warning("JOB_WORKER_COUNT capped at 32, got %d", n)
        return 32
    return n


def validate_option_changes(changes: dict[str, str | None], current: dict[str, str | None]) -> list[str]:
    """Cross-field/range validation for a PATCH, evaluated on the *resulting*
    values so a request carrying both limits is judged as a whole. Returns
    human-readable problems (empty = fine)."""
    merged = {**current, **changes}
    problems: list[str] = []

    def number(key: str, label: str, default: float) -> float:
        raw = merged.get(key)
        if raw in (None, ""):
            return default
        value = _try_float(raw)
        if value is None or value <= 0:
            if key in changes:  # stored garbage already falls back in from_dict
                problems.append(f"{label} must be a positive number.")
            return default
        return value

    hard = number("CPS_LIMIT", "Hard CPS limit", 20.0)
    soft = number("SOFT_CPS_LIMIT", "Soft CPS limit", 18.0)
    join = number("JOIN_LINES_UNDER", "Join lines under", 45.0)
    if "JOIN_LINES_UNDER" in changes and not join.is_integer():
        problems.append("Join lines under must be a whole number.")
    if not problems and soft >= hard and {"CPS_LIMIT", "SOFT_CPS_LIMIT"} & changes.keys():
        problems.append(
            f"Soft CPS limit ({soft:g}) must be lower than the hard CPS limit ({hard:g}).")
    return problems


def _try_float(raw: str | None) -> float | None:
    try:
        return float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


# -- Change listeners (async only) ---------------------------------------------

ChangeListener = Callable[[str, str | None], Awaitable[None]]
_change_listeners: list[ChangeListener] = []


def register_change_listener(fn: ChangeListener) -> None:
    """Register a coroutine function called after every aset() write."""
    _change_listeners.append(fn)


# -- In-memory cache -----------------------------------------------------------
_cache: dict[str, str | None] = {}
_loaded = False
_lock = threading.Lock()


# -- Internal loaders ----------------------------------------------------------

def _load_sync() -> None:
    global _loaded
    with SyncSessionLocal() as session:
        rows = session.scalars(select(Option)).all()
    with _lock:
        for row in rows:
            _cache[row.name] = row.value
        _loaded = True


async def _load_async() -> None:
    global _loaded
    async with AsyncSessionLocal() as session:
        rows = (await session.scalars(select(Option))).all()
    with _lock:
        for row in rows:
            _cache[row.name] = row.value
        _loaded = True


# -- Sync API ------------------------------------------------------------------

def get(name: str, default: str | None = None) -> str | None:
    if not _loaded:
        _load_sync()
    with _lock:
        return _cache.get(name, default)


# Options that change the rendered ASS/MKV of *already translated* data. A real
# change invalidates every project's published output (their output_revision).
# Pipeline-only options (models, prompts, chunking, CPS…) shape future
# translations, whose data changes are tracked at the event level instead.
OUTPUT_AFFECTING_OPTIONS = frozenset({
    "REPLACE_INCOMPATIBLE_FONTS",  # which font the translated ASS styles use
    "TARGET_LANG_NAME",            # ASS Title + MKV subtitle track name
    "TARGET_LANG_CODE",            # MKV subtitle track language
})


def _changes_output(name: str, value: str | None) -> bool:
    # Compared against the in-memory cache (loaded at startup). A cold cache can
    # only over-invalidate, never miss a change.
    return name in OUTPUT_AFFECTING_OPTIONS and (_cache.get(name) or None) != (value or None)


def set(name: str, value: str | None) -> None:
    now = datetime.utcnow().isoformat()
    with SyncSessionLocal() as session:
        if _changes_output(name, value):
            touch_all_outputs_sync(session)
        session.execute(
            sqlite_insert(Option)
            .values(name=name, value=value, created_at=now, updated_at=now)
            .on_conflict_do_update(
                index_elements=["name"],
                set_={"value": value, "updated_at": now},
            )
        )
        session.commit()
    with _lock:
        _cache[name] = value


def snapshot() -> AppOptions:
    """Return a typed snapshot of current options for passing into JobContext."""
    if not _loaded:
        _load_sync()
    with _lock:
        return AppOptions.from_dict(dict(_cache))


# -- Async API -----------------------------------------------------------------

async def aget(name: str, default: str | None = None) -> str | None:
    if not _loaded:
        await _load_async()
    with _lock:
        return _cache.get(name, default)


async def aset(name: str, value: str | None) -> None:
    now = datetime.utcnow().isoformat()
    async with AsyncSessionLocal() as session:
        if _changes_output(name, value):
            await session.run_sync(touch_all_outputs_sync)
        await session.execute(
            sqlite_insert(Option)
            .values(name=name, value=value, created_at=now, updated_at=now)
            .on_conflict_do_update(
                index_elements=["name"],
                set_={"value": value, "updated_at": now},
            )
        )
        await session.commit()
    with _lock:
        _cache[name] = value
    for listener in list(_change_listeners):
        try:
            await listener(name, value)
        except Exception:
            _logger.exception("Change listener failed for option %r", name)


async def asnapshot() -> AppOptions:
    """Async version of snapshot() for use in FastAPI routes."""
    if not _loaded:
        await _load_async()
    with _lock:
        return AppOptions.from_dict(dict(_cache))


# -- Cache control -------------------------------------------------------------

def invalidate(name: str | None = None) -> None:
    """Drop one key or the entire cache (forces reload on next access)."""
    global _loaded
    with _lock:
        if name is None:
            _cache.clear()
            _loaded = False
        else:
            _cache.pop(name, None)
