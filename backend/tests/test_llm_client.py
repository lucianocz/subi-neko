"""Tests for the shared LLM client: structured-output downgrade ladder,
transient retries, corrective schema retry, and llm_calls accounting."""
from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import openai
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.db.models import LlmCall
from app.db.options import AppOptions
from app.llm import client as llm_client
from app.jobs.handlers.infer_character_mapping import mapping_response_error
from app.llm.schemas import MappingResponse, TranslateResponse, strict_json_schema


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def sync_db(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr("app.llm.client.SyncSessionLocal", factory)
    yield factory
    engine.dispose()


@pytest.fixture(autouse=True)
def _fast_and_clean(monkeypatch):
    llm_client.reset_capability_caches()
    monkeypatch.setattr("time.sleep", lambda _s: None)
    yield
    llm_client.reset_capability_caches()


def _response(content: str, prompt_tokens: int = 100, completion_tokens: int = 50,
              finish_reason: str = "stop"):
    return SimpleNamespace(
        choices=[SimpleNamespace(
            message=SimpleNamespace(content=content),
            finish_reason=finish_reason,
        )],
        usage=SimpleNamespace(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens),
    )


def _bad_request(message: str) -> openai.BadRequestError:
    request = httpx.Request("POST", "http://test/v1/chat/completions")
    response = httpx.Response(400, request=request, text=message)
    return openai.BadRequestError(message, response=response, body=None)


def _rate_limit() -> openai.RateLimitError:
    request = httpx.Request("POST", "http://test/v1/chat/completions")
    response = httpx.Response(429, request=request, text="rate limited")
    return openai.RateLimitError("rate limited", response=response, body=None)


class FakeCompletions:
    """Scripted chat.completions.create: pops the next behavior per call."""

    def __init__(self, script):
        self.script = list(script)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        action = self.script.pop(0)
        if isinstance(action, Exception):
            raise action
        return action


def _install_fake(monkeypatch, script) -> FakeCompletions:
    completions = FakeCompletions(script)

    class FakeOpenAI:
        def __init__(self, **_kwargs):
            self.chat = SimpleNamespace(completions=completions)

    monkeypatch.setattr("app.llm.client.openai.OpenAI", FakeOpenAI)
    return completions


VALID_JSON = json.dumps({"translations": [{"i": 0, "t": "Ahoj", "c": 0.9}]})


def _complete(**overrides):
    kwargs = dict(
        task="translate",
        model="test-model",
        system="system prompt",
        user="user prompt",
        schema=TranslateResponse,
        options=AppOptions(),
        max_completion_tokens=1000,
        project_id=1,
        file_id=2,
        chunk_id=3,
        backoff_base_seconds=0.0,
    )
    kwargs.update(overrides)
    return llm_client.complete(**kwargs)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_json_schema_success_first_try(sync_db, monkeypatch):
    completions = _install_fake(monkeypatch, [_response(VALID_JSON)])

    result, stats = _complete()

    assert result.translations[0].t == "Ahoj"
    assert result.translations[0].c == 0.9
    assert stats.response_mode == "json_schema"
    assert stats.prompt_tokens == 100
    call = completions.calls[0]
    assert call["response_format"]["type"] == "json_schema"
    assert call["response_format"]["json_schema"]["strict"] is True
    assert call["temperature"] == 0.4  # translate task default
    assert call["max_completion_tokens"] == 1000


def test_downgrade_to_json_object_on_schema_rejection(sync_db, monkeypatch):
    completions = _install_fake(monkeypatch, [
        _bad_request("response_format json_schema is not supported"),
        _response(VALID_JSON),
    ])

    result, stats = _complete()

    assert stats.response_mode == "json_object"
    assert completions.calls[1]["response_format"] == {"type": "json_object"}

    # The working mode is cached — the next call skips the failed probe.
    completions2 = _install_fake(monkeypatch, [_response(VALID_JSON)])
    _result, stats2 = _complete()
    assert stats2.response_mode == "json_object"
    assert completions2.calls[0]["response_format"] == {"type": "json_object"}


def test_downgrade_to_text_and_sanitize_fallback(sync_db, monkeypatch):
    fenced = f"```json\n{VALID_JSON}\n```"
    _install_fake(monkeypatch, [
        _bad_request("json_schema not supported"),
        _bad_request("response_format not supported"),
        _response(fenced),
    ])

    result, stats = _complete()

    assert stats.response_mode == "text"
    assert result.translations[0].t == "Ahoj"


def test_temperature_dropped_when_rejected(sync_db, monkeypatch):
    completions = _install_fake(monkeypatch, [
        _bad_request("Unsupported parameter: temperature"),
        _response(VALID_JSON),
    ])

    _result, _stats = _complete()

    assert "temperature" in completions.calls[0]
    assert "temperature" not in completions.calls[1]


def test_transient_error_retried(sync_db, monkeypatch):
    completions = _install_fake(monkeypatch, [
        _rate_limit(),
        _rate_limit(),
        _response(VALID_JSON),
    ])

    result, stats = _complete()

    assert result.translations[0].i == 0
    assert len(completions.calls) == 3


def test_transient_errors_exhausted_raise_api_error(sync_db, monkeypatch):
    _install_fake(monkeypatch, [_rate_limit()] * 4)

    with pytest.raises(llm_client.LlmError) as exc_info:
        _complete(max_transient_retries=3)

    assert exc_info.value.code == "OPENAI_API_ERROR"


def test_corrective_retry_on_schema_mismatch(sync_db, monkeypatch):
    completions = _install_fake(monkeypatch, [
        _response('{"wrong_key": []}'),
        _response(VALID_JSON),
    ])

    result, _stats = _complete()

    assert result.translations[0].t == "Ahoj"
    # The corrective retry feeds the bad output back to the model.
    second_call_messages = completions.calls[1]["messages"]
    assert second_call_messages[-2]["role"] == "assistant"
    assert "failed validation" in second_call_messages[-1]["content"]


def test_parse_error_after_corrective_retry_raises(sync_db, monkeypatch):
    _install_fake(monkeypatch, [
        _response("not json at all"),
        _response("still not json"),
    ])

    with pytest.raises(llm_client.LlmError) as exc_info:
        _complete()

    assert exc_info.value.code == "RESPONSE_PARSE_ERROR"


def _mapping_json(*speakers: str) -> str:
    return json.dumps({"matches": [
        {
            "speaker": speaker,
            "character_external_id": None,
            "confidence": 0.2,
            "inferred_gender": None,
            "rationale": "uncertain",
        }
        for speaker in speakers
    ]})


def _mapping_complete(requested: list[str], **overrides):
    return _complete(
        task="analyze",
        schema=MappingResponse,
        response_validator=lambda response: mapping_response_error(response, requested),
        **overrides,
    )


def test_response_validator_missing_speaker_gets_corrective_retry(sync_db, monkeypatch):
    completions = _install_fake(monkeypatch, [
        _response(_mapping_json("One")),
        _response(_mapping_json("One", "Two")),
    ])

    result, stats = _mapping_complete(["One", "Two"])

    assert [match.speaker for match in result.matches] == ["One", "Two"]
    assert stats.attempts == 2
    correction = completions.calls[1]["messages"][-1]["content"]
    assert "exactly one result for every requested speaker" in correction
    assert "preserve each speaker label exactly" in correction


def test_response_validator_duplicate_and_unexpected_gets_corrective_retry(sync_db, monkeypatch):
    completions = _install_fake(monkeypatch, [
        _response(_mapping_json("One", "One", "Other")),
        _response(_mapping_json("One", "Two")),
    ])

    result, stats = _mapping_complete(["One", "Two"])

    assert [match.speaker for match in result.matches] == ["One", "Two"]
    assert stats.attempts == 2
    correction = completions.calls[1]["messages"][-1]["content"]
    assert "duplicate speakers" in correction
    assert "unexpected speakers" in correction


def test_response_validator_second_invalid_response_fails(sync_db, monkeypatch):
    _install_fake(monkeypatch, [
        _response(_mapping_json("One")),
        _response(_mapping_json("One", "One")),
    ])

    with pytest.raises(llm_client.LlmError) as exc_info:
        _mapping_complete(["One", "Two"])

    assert exc_info.value.code == "RESPONSE_PARSE_ERROR"
    assert "Mapping response must contain exactly one result" in exc_info.value.message


def test_response_validator_valid_first_response_uses_one_call(sync_db, monkeypatch):
    completions = _install_fake(monkeypatch, [_response(_mapping_json("One", "Two"))])

    result, stats = _mapping_complete(["One", "Two"])

    assert len(result.matches) == 2
    assert stats.attempts == 1
    assert len(completions.calls) == 1


def test_llm_calls_rows_written(sync_db, monkeypatch):
    _install_fake(monkeypatch, [
        _rate_limit(),
        _response(VALID_JSON),
    ])
    options = AppOptions(llm_prices_json='{"test-model": {"in": 1.0, "out": 2.0}}')

    _result, stats = _complete(options=options)

    with sync_db() as session:
        rows = list(session.scalars(select(LlmCall).order_by(LlmCall.id)).all())
    assert [r.status for r in rows] == ["retried", "succeeded"]
    success = rows[-1]
    assert success.task == "translate"
    assert success.model == "test-model"
    assert success.project_id == 1 and success.file_id == 2 and success.subtitle_chunk_id == 3
    assert success.prompt_tokens == 100 and success.completion_tokens == 50
    # (100 * 1.0 + 50 * 2.0) / 1e6
    assert success.cost_usd == pytest.approx(0.0002)
    assert stats.cost_usd == pytest.approx(0.0002)


def test_truncated_output_escalates_token_budget(sync_db, monkeypatch):
    """finish_reason='length' means the JSON was cut mid-string — the client
    must retry with a doubled budget instead of a pointless corrective retry."""
    truncated = '{"translations":[{"i":0,"t":"Ahoj, nedokon'
    completions = _install_fake(monkeypatch, [
        _response(truncated, finish_reason="length"),
        _response(VALID_JSON),
    ])

    result, _stats = _complete(max_completion_tokens=1000)

    assert result.translations[0].t == "Ahoj"
    assert completions.calls[0]["max_completion_tokens"] == 1000
    assert completions.calls[1]["max_completion_tokens"] == 2000


def test_truncation_at_ceiling_raises_output_truncated(sync_db, monkeypatch):
    truncated = '{"translations":[{"i":0,"t":"Aho'
    _install_fake(monkeypatch, [_response(truncated, finish_reason="length")] * 4)

    options = AppOptions(llm_max_completion_tokens=1000)
    with pytest.raises(llm_client.LlmError) as exc_info:
        _complete(options=options, max_completion_tokens=1000)

    # Deliberately NOT a retryable code — same budget would truncate again.
    assert exc_info.value.code == "OUTPUT_TRUNCATED"
    assert "CHUNK_SIZE" in exc_info.value.message


def test_completion_budget_scales_with_lines_and_chars():
    # 200 lines × 40 chars: the old chars/3 formula gave ~4k and truncated
    # real Czech output — the new one must budget for ~2.5 chars/token plus
    # per-line JSON envelope overhead.
    budget = llm_client.completion_budget(8000, 200)
    assert budget >= 12000
    assert llm_client.completion_budget(0, 0) >= 2000  # constant headroom


def test_strict_schema_marks_all_properties_required():
    schema = strict_json_schema(TranslateResponse)
    # Root object
    assert set(schema["required"]) == set(schema["properties"].keys())
    # Nested item object (c is optional-by-meaning but required-nullable)
    item = schema["$defs"]["TranslationItem"]
    assert set(item["required"]) == {"i", "t", "c"}
    assert item["additionalProperties"] is False
