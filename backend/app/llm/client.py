"""Shared LLM client — the single path for every LLM call in the pipeline.

Responsibilities:
- structured outputs with a downgrade ladder: json_schema (strict) →
  json_object → free text + sanitize_llm_json; the working mode is cached
  per (base_url, model) so the probe cost is paid once
- transient-error retries (connection/timeout/429/5xx) with exponential backoff
- one corrective retry when the response fails schema validation
- per-task temperature policy with capability detection (models that reject
  the parameter get it dropped and the fact is cached)
- token/cost/latency accounting into the llm_calls table (best-effort)

Handlers call complete() and catch LlmError; the error codes match the
pre-existing handler codes (OPENAI_API_ERROR / RESPONSE_PARSE_ERROR) so
downstream chunk-failure classification keeps working.
"""
from __future__ import annotations

import json
import logging
import random
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from collections.abc import Callable

import openai
from pydantic import BaseModel, ValidationError

from app.core.database import SyncSessionLocal
from app.db.models import LlmCall
from app.db.options import AppOptions
from app.jobs.handlers.utils import sanitize_llm_json
from app.llm.schemas import strict_json_schema

logger = logging.getLogger(__name__)

# Per-task temperature defaults; None means "provider default".
TASK_TEMPERATURES: dict[str, float] = {
    "translate": 0.4,
    "polish": 0.3,
    "repair": 0.1,
    "analyze": 0.2,
    "style_bible": 0.2,
}

_MODES = ("json_schema", "json_object", "text")

# Capability caches, keyed by (base_url, model). Guarded by _cache_lock —
# handlers run in worker threads.
_mode_cache: dict[tuple[str, str], str] = {}
_temperature_unsupported: set[tuple[str, str]] = set()
_max_completion_tokens_unsupported: set[tuple[str, str]] = set()
_cache_lock = threading.Lock()


class LlmError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class LlmCallStats:
    model: str
    response_mode: str
    attempts: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float | None = None
    latency_ms: int = 0


def reset_capability_caches() -> None:
    """Test hook / option-change hook."""
    with _cache_lock:
        _mode_cache.clear()
        _temperature_unsupported.clear()
        _max_completion_tokens_unsupported.clear()


# Retries when the model hits the completion-token ceiling (finish_reason
# "length") — the budget doubles each time up to the configured limit.
_MAX_TRUNCATION_RETRIES = 2


def completion_budget(source_chars: int, line_count: int) -> int:
    """Completion-token budget for a per-line JSON response.

    Czech (and other diacritic-heavy languages) tokenizes at ~2–2.5 chars
    per token, and each JSON entry ({"i":…,"t":"…","c":…}) costs ~12 tokens
    of envelope. Reasoning models additionally burn invisible reasoning
    tokens from the same budget, hence the generous constant headroom —
    an oversized budget costs nothing, an undersized one truncates the
    response mid-string.
    """
    return int(source_chars / 2) + 30 * max(1, line_count) + 2000


def _price_table(options: AppOptions) -> dict[str, dict[str, float]]:
    raw = options.llm_prices_json
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        logger.warning("LLM_PRICES_JSON is not valid JSON; cost tracking disabled")
        return {}


def _compute_cost(prices: dict, model: str, prompt_tokens: int, completion_tokens: int) -> float | None:
    entry = prices.get(model)
    if not isinstance(entry, dict):
        return None
    try:
        return (float(entry.get("in", 0)) * prompt_tokens
                + float(entry.get("out", 0)) * completion_tokens) / 1_000_000
    except (TypeError, ValueError):
        return None


def _log_call(
    *, task: str, model: str, attempt: int, status: str, response_mode: str | None,
    prompt_tokens: int | None, completion_tokens: int | None, cost_usd: float | None,
    latency_ms: int | None, error_code: str | None,
    project_id: int | None, file_id: int | None, chunk_id: int | None,
) -> None:
    try:
        with SyncSessionLocal() as session:
            session.add(LlmCall(
                project_id=project_id, file_id=file_id, subtitle_chunk_id=chunk_id,
                task=task, model=model, attempt=attempt, status=status,
                response_mode=response_mode,
                prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
                cost_usd=cost_usd, latency_ms=latency_ms, error_code=error_code,
                created_at=datetime.utcnow().isoformat(),
            ))
            session.commit()
    except Exception:
        logger.exception("Failed to log llm_call row (task=%s model=%s)", task, model)


def _is_transient(exc: Exception) -> bool:
    if isinstance(exc, (openai.APIConnectionError, openai.APITimeoutError, openai.RateLimitError)):
        return True
    if isinstance(exc, openai.APIStatusError):
        return exc.status_code >= 500
    return False


def _initial_mode(options: AppOptions, base_url: str, model: str) -> str:
    configured = (options.llm_structured_outputs or "auto").strip().lower()
    if configured in _MODES:
        return configured
    with _cache_lock:
        return _mode_cache.get((base_url, model), "json_schema")


def complete(
    *,
    task: str,
    model: str,
    system: str,
    user: str,
    schema: type[BaseModel],
    options: AppOptions,
    temperature: float | None = None,
    max_completion_tokens: int | None = None,
    project_id: int | None = None,
    file_id: int | None = None,
    chunk_id: int | None = None,
    response_validator: Callable[[BaseModel], str | None] | None = None,
    max_transient_retries: int = 3,
    backoff_base_seconds: float = 1.0,
) -> tuple[BaseModel, LlmCallStats]:
    """Run one structured LLM completion and return the validated response.

    Raises LlmError("OPENAI_API_ERROR") for exhausted/permanent API failures
    and LlmError("RESPONSE_PARSE_ERROR") when the response cannot be coerced
    into the schema even after the corrective retry.
    """
    base_url = options.openai_api_base or "https://api.openai.com/v1"
    client = openai.OpenAI(
        api_key=options.openai_api_key or "no-key",
        base_url=base_url,
    )
    cache_key = (base_url, model)

    mode = _initial_mode(options, base_url, model)
    if temperature is None:
        temperature = TASK_TEMPERATURES.get(task)
    with _cache_lock:
        if cache_key in _temperature_unsupported:
            temperature = None
        send_max_tokens = cache_key not in _max_completion_tokens_unsupported

    limit = options.llm_max_completion_tokens
    if max_completion_tokens is None or max_completion_tokens > limit:
        max_completion_tokens = limit

    prices = _price_table(options)
    stats = LlmCallStats(model=model, response_mode=mode)
    messages: list[dict[str, str]] = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]

    schema_name = schema.__name__
    strict_schema = strict_json_schema(schema)

    transient_attempts = 0
    truncation_retries = 0
    corrective_retry_done = False
    last_error: str | None = None

    while True:
        kwargs: dict = {"model": model, "messages": messages}
        if mode == "json_schema":
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": schema_name, "strict": True, "schema": strict_schema},
            }
        elif mode == "json_object":
            kwargs["response_format"] = {"type": "json_object"}
        if temperature is not None:
            kwargs["temperature"] = temperature
        if send_max_tokens and max_completion_tokens:
            kwargs["max_completion_tokens"] = max_completion_tokens

        stats.attempts += 1
        started = time.monotonic()
        try:
            response = client.chat.completions.create(**kwargs)
        except openai.BadRequestError as exc:
            # Capability probing: downgrade and retry immediately without
            # counting against the transient budget.
            message = str(exc).lower()
            if mode == "json_schema" and ("json_schema" in message or "response_format" in message or "strict" in message):
                mode = "json_object"
                with _cache_lock:
                    _mode_cache[cache_key] = mode
                logger.info("Model %s rejected json_schema; downgrading to json_object", model)
                continue
            if mode == "json_object" and "response_format" in message:
                mode = "text"
                with _cache_lock:
                    _mode_cache[cache_key] = mode
                logger.info("Model %s rejected json_object; downgrading to free text", model)
                continue
            if temperature is not None and "temperature" in message:
                temperature = None
                with _cache_lock:
                    _temperature_unsupported.add(cache_key)
                logger.info("Model %s rejected temperature; dropping the parameter", model)
                continue
            if send_max_tokens and ("max_completion_tokens" in message or "max_tokens" in message):
                send_max_tokens = False
                with _cache_lock:
                    _max_completion_tokens_unsupported.add(cache_key)
                logger.info("Model %s rejected max_completion_tokens; dropping the parameter", model)
                continue
            _log_call(task=task, model=model, attempt=stats.attempts, status="failed",
                      response_mode=mode, prompt_tokens=None, completion_tokens=None,
                      cost_usd=None, latency_ms=int((time.monotonic() - started) * 1000),
                      error_code="BAD_REQUEST",
                      project_id=project_id, file_id=file_id, chunk_id=chunk_id)
            raise LlmError("OPENAI_API_ERROR", str(exc)) from exc
        except Exception as exc:
            latency_ms = int((time.monotonic() - started) * 1000)
            if _is_transient(exc) and transient_attempts < max_transient_retries:
                transient_attempts += 1
                delay = backoff_base_seconds * (2 ** (transient_attempts - 1)) + random.uniform(0, 0.5)
                logger.warning(
                    "Transient LLM error (task=%s model=%s attempt=%d/%d): %s — retrying in %.1fs",
                    task, model, transient_attempts, max_transient_retries, exc, delay,
                )
                _log_call(task=task, model=model, attempt=stats.attempts, status="retried",
                          response_mode=mode, prompt_tokens=None, completion_tokens=None,
                          cost_usd=None, latency_ms=latency_ms, error_code="TRANSIENT",
                          project_id=project_id, file_id=file_id, chunk_id=chunk_id)
                time.sleep(delay)
                continue
            _log_call(task=task, model=model, attempt=stats.attempts, status="failed",
                      response_mode=mode, prompt_tokens=None, completion_tokens=None,
                      cost_usd=None, latency_ms=latency_ms, error_code="API_ERROR",
                      project_id=project_id, file_id=file_id, chunk_id=chunk_id)
            raise LlmError("OPENAI_API_ERROR", str(exc)) from exc

        latency_ms = int((time.monotonic() - started) * 1000)
        stats.latency_ms += latency_ms
        stats.response_mode = mode

        usage = getattr(response, "usage", None)
        prompt_tokens = getattr(usage, "prompt_tokens", None) or 0
        completion_tokens = getattr(usage, "completion_tokens", None) or 0
        stats.prompt_tokens += prompt_tokens
        stats.completion_tokens += completion_tokens
        cost = _compute_cost(prices, model, prompt_tokens, completion_tokens)
        if cost is not None:
            stats.cost_usd = (stats.cost_usd or 0.0) + cost

        raw_content = response.choices[0].message.content or ""

        # Hard token-limit truncation: the JSON is cut mid-string and no
        # corrective retry can fix it at the same budget — escalate the
        # budget instead, and fail with a distinct (non-retryable) code
        # when the configured ceiling is reached.
        finish_reason = getattr(response.choices[0], "finish_reason", None)
        if finish_reason == "length":
            if (send_max_tokens and max_completion_tokens
                    and max_completion_tokens < limit
                    and truncation_retries < _MAX_TRUNCATION_RETRIES):
                truncation_retries += 1
                max_completion_tokens = min(limit, max_completion_tokens * 2)
                logger.warning(
                    "Output truncated (task=%s model=%s) — retrying with "
                    "max_completion_tokens=%d (%d/%d)",
                    task, model, max_completion_tokens,
                    truncation_retries, _MAX_TRUNCATION_RETRIES,
                )
                _log_call(task=task, model=model, attempt=stats.attempts, status="retried",
                          response_mode=mode, prompt_tokens=prompt_tokens,
                          completion_tokens=completion_tokens, cost_usd=cost,
                          latency_ms=latency_ms, error_code="TRUNCATED",
                          project_id=project_id, file_id=file_id, chunk_id=chunk_id)
                continue
            _log_call(task=task, model=model, attempt=stats.attempts, status="failed",
                      response_mode=mode, prompt_tokens=prompt_tokens,
                      completion_tokens=completion_tokens, cost_usd=cost,
                      latency_ms=latency_ms, error_code="OUTPUT_TRUNCATED",
                      project_id=project_id, file_id=file_id, chunk_id=chunk_id)
            raise LlmError(
                "OUTPUT_TRUNCATED",
                f"Model output hit the completion-token ceiling "
                f"({max_completion_tokens if send_max_tokens else 'provider default'} tokens) "
                f"even after {truncation_retries} escalation(s). "
                f"Reduce CHUNK_SIZE or raise LLM_MAX_COMPLETION_TOKENS.",
            )

        try:
            payload = json.loads(sanitize_llm_json(raw_content))
            result = schema.model_validate(payload)
            if response_validator is not None:
                validation_message = response_validator(result)
                if validation_message:
                    raise ValueError(validation_message)
        except (json.JSONDecodeError, ValidationError, TypeError, ValueError) as exc:
            if not corrective_retry_done:
                corrective_retry_done = True
                logger.warning(
                    "Schema validation failed (task=%s model=%s): %s — corrective retry",
                    task, model, exc,
                )
                _log_call(task=task, model=model, attempt=stats.attempts, status="retried",
                          response_mode=mode, prompt_tokens=prompt_tokens,
                          completion_tokens=completion_tokens, cost_usd=cost,
                          latency_ms=latency_ms, error_code="SCHEMA_MISMATCH",
                          project_id=project_id, file_id=file_id, chunk_id=chunk_id)
                messages = messages + [
                    {"role": "assistant", "content": raw_content},
                    {"role": "user", "content": (
                        "Your previous output failed validation:\n"
                        f"{exc}\n\n"
                        "Return ONLY a valid JSON object matching the required schema — "
                        "no markdown, no commentary."
                    )},
                ]
                continue
            _log_call(task=task, model=model, attempt=stats.attempts, status="failed",
                      response_mode=mode, prompt_tokens=prompt_tokens,
                      completion_tokens=completion_tokens, cost_usd=cost,
                      latency_ms=latency_ms, error_code="RESPONSE_PARSE_ERROR",
                      project_id=project_id, file_id=file_id, chunk_id=chunk_id)
            raise LlmError(
                "RESPONSE_PARSE_ERROR",
                f"Could not parse model response: {exc}\n\nRaw: {raw_content[:500]}",
            ) from exc

        _log_call(task=task, model=model, attempt=stats.attempts, status="succeeded",
                  response_mode=mode, prompt_tokens=prompt_tokens,
                  completion_tokens=completion_tokens, cost_usd=cost,
                  latency_ms=latency_ms, error_code=last_error,
                  project_id=project_id, file_id=file_id, chunk_id=chunk_id)
        return result, stats
