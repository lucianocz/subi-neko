"""Only temporary databases and mocked providers/website requests."""
import json

import httpx
import openai
import pytest
from sqlalchemy import select

from app.db.models import LlmCall
from app.llm import client
from app.llm.metrics import usage_statement
from app.publishing import description, service
from app.publishing.schemas import SaveRequest, TitleMetadata, WebConfig
from app.publishing.web_client import PublishingError
from tests.test_llm_client import _install_fake, _response
from tests.test_web_publishing import env, run, setup  # noqa: F401


def request(env, version=0):
    return SaveRequest(version=version, config=env.config)


def prepare(env, monkeypatch, script):
    monkeypatch.setattr(description, "SyncSessionLocal", env.sync)
    monkeypatch.setattr(client, "SyncSessionLocal", env.sync)
    client.reset_capability_caches()
    env.config.metadata.original_description = "A hero named Rei searches for home."
    env.config.metadata.description = "An already edited synopsis."
    env.opts.target_lang_name = "German"
    env.opts.target_lang_code = "de"
    env.opts.openai_model_better = "test-model"
    env.opts.llm_prices_json = json.dumps({"test-model": {"in": 1, "out": 2}})
    return _install_fake(monkeypatch, script)


def test_legacy_description_is_preserved_and_seeds_original():
    metadata = TitleMetadata(title_english="Film", original_language="ja", description_english="Approved")
    assert metadata.description == metadata.original_description == "Approved"
    assert "description_english" not in metadata.model_dump()
    metadata = TitleMetadata(title_english="Film", original_language="ja", description="Existing")
    assert metadata.original_description == "Existing"
    explicit = metadata.model_copy(update={"original_description": "English"})
    assert TitleMetadata.model_validate(explicit.model_dump()).original_description == "English"


@pytest.mark.asyncio
async def test_translation_original_language_repeated_usage_and_persistence(env, monkeypatch):
    fake = prepare(env, monkeypatch, [_response('{"description":"Rei sucht ein Zuhause."}'),
                                      _response('{"description":"Rei ist auf der Suche."}')])
    first = description.translate(env.pid, request(env))
    # Proposal cannot write after a closed/edited wizard; explicit save is separate.
    assert service.state(env.pid)["config"] is None
    translated = env.config.model_copy(deep=True)
    translated.metadata.description = first["description"]
    saved = service.save(env.pid, SaveRequest(version=0, config=translated))
    env.config = WebConfig.model_validate(saved["config"])
    description.translate(env.pid, request(env, saved["version"]))
    for call in fake.calls:
        assert call["messages"][1]["content"] == "A hero named Rei searches for home."
        assert "German" in call["messages"][0]["content"]
        assert "test-token" not in str(call) and "tmdb-secret" not in str(call)
    assert saved["config"]["metadata"]["description"] == first["description"]
    assert saved["config"]["metadata"]["original_description"] == env.config.metadata.original_description
    with env.sync() as session:
        calls = list(session.scalars(select(LlmCall)))
        assert len(calls) == 2
        assert all(c.model == "test-model" and c.file_id is None for c in calls)
        assert sum(c.prompt_tokens for c in calls) == 200
        assert sum(c.completion_tokens for c in calls) == 100
        assert sum(c.cost_usd for c in calls) == pytest.approx(0.0004)


@pytest.mark.asyncio
@pytest.mark.parametrize("source", [None, "", "  \n", "bad\x00source"])
async def test_invalid_source_never_calls_provider(env, monkeypatch, source):
    fake = prepare(env, monkeypatch, [])
    env.config.metadata.original_description = source
    with pytest.raises(PublishingError, match="valid original description"):
        description.translate(env.pid, request(env))
    assert not fake.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", ['{"description":" "}', '{"description":null}', '{}', 'not JSON'])
async def test_invalid_response_clear_error_and_accounting(env, monkeypatch, payload):
    prepare(env, monkeypatch, [_response(payload), _response(payload)])
    with pytest.raises(PublishingError, match="invalid description"):
        description.translate(env.pid, request(env))
    with env.sync() as session:
        calls = list(session.scalars(select(LlmCall)))
        assert len(calls) == 2 and calls[-1].status == "failed"
        assert sum(c.prompt_tokens for c in calls) == 200


@pytest.mark.asyncio
async def test_provider_timeout_clear_error_without_persisting(env, monkeypatch):
    timeout = openai.APITimeoutError(request=httpx.Request("POST", "https://provider.example"))
    prepare(env, monkeypatch, [timeout, timeout])
    monkeypatch.setattr(client.time, "sleep", lambda _: None)
    with pytest.raises(PublishingError, match="failed or timed out"):
        description.translate(env.pid, request(env))
    assert service.state(env.pid)["config"] is None


@pytest.mark.asyncio
async def test_language_code_fallback_and_missing_configuration(env, monkeypatch):
    fake = prepare(env, monkeypatch, [_response('{"description":"Hallo"}')])
    env.opts.target_lang_name = None
    description.translate(env.pid, request(env))
    assert "into de." in fake.calls[0]["messages"][0]["content"]
    env.opts.target_lang_code = None
    with pytest.raises(PublishingError, match="Configure a target language"):
        description.translate(env.pid, request(env))
    assert len(fake.calls) == 1


@pytest.mark.asyncio
async def test_http_translation_then_existing_save_persists_without_publishing(env, monkeypatch):
    from app.main import create_app

    monkeypatch.setattr("app.api.archive_guard.AsyncSessionLocal", env.asess)
    monkeypatch.setattr("app.api.routes.projects.AsyncSessionLocal", env.asess)
    prepare(env, monkeypatch, [_response('{"description":"Hallo Rei"}')])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(create_app()), base_url="http://local") as api:
        empty = await api.get(f"/api/projects/{env.pid}/llm-metrics")
        assert empty.status_code == 200 and empty.json() == []
        translated = await api.post(f"/api/projects/{env.pid}/web/translate-description", json=request(env).model_dump())
        assert translated.status_code == 200
        env.config.metadata.description = translated.json()["description"]
        saved = await api.put(f"/api/projects/{env.pid}/web", json=request(env).model_dump())
        assert saved.status_code == 200
        assert saved.json()["config"]["metadata"]["description"] == "Hallo Rei"
        usage = await api.get(f"/api/projects/{env.pid}/llm-metrics")
        assert usage.json()[0]["category"] == "Other" and usage.json()[0]["calls"] == 1
    assert not env.remote.calls


@pytest.mark.asyncio
async def test_delayed_proposal_cannot_overwrite_new_manual_save(env, monkeypatch):
    prepare(env, monkeypatch, [_response('{"description":"Delayed"}')])
    initial = setup(env, mappings=False)
    proposal = description.translate(env.pid, request(env, initial["version"]))
    manual = env.config.model_copy(deep=True)
    manual.metadata.description = "New manual edit"
    newer = service.save(env.pid, SaveRequest(version=initial["version"], config=manual))
    env.config.metadata.description = proposal["description"]
    with pytest.raises(PublishingError, match="Configuration changed"):
        service.save(env.pid, request(env, initial["version"]))
    assert service.state(env.pid)["config"] == newer["config"]


@pytest.mark.asyncio
@pytest.mark.parametrize("edited", [True, False])
async def test_refresh_preserves_edits_and_omission(env, edited):
    env.config.metadata.original_description = "Old source"
    env.config.metadata.description = "Approved translation" if edited else "Old source"
    saved = setup(env, mappings=False)
    fresh = env.config.model_copy(deep=True)
    fresh.metadata.original_description = fresh.metadata.description = "New source"
    saved = service.save(env.pid, SaveRequest(version=saved["version"], config=fresh))
    assert saved["config"]["metadata"]["description"] == ("Approved translation" if edited else "New source")
    omitted = fresh.model_copy(deep=True)
    omitted.metadata.original_description = omitted.metadata.description = None
    result = service.save(env.pid, SaveRequest(version=saved["version"], config=omitted))
    assert result["config"]["metadata"] == saved["config"]["metadata"]


@pytest.mark.asyncio
async def test_website_contract_receives_only_approved_description(env):
    env.config.metadata.original_description = "English source"
    env.config.metadata.description = "Schválený překlad"
    setup(env)
    assert run(env)["status"] == "succeeded"
    title = next(k["json"] for m, p, k in env.remote.calls if m == "PUT" and p.endswith("/titles"))
    assert title["description_english"] == "Schválený překlad"
    assert "description" not in title and "original_description" not in title


def test_metadata_fingerprint_keeps_legacy_shape_and_ignores_original():
    legacy = {"metadata": {"title_english": "Title", "description_english": "Approved"}}
    modern = {"metadata": {"title_english": "Title", "description": "Approved", "original_description": "English"}}
    assert service.fingerprint(legacy) == service.fingerprint(modern)
    modern["metadata"]["original_description"] = "Updated English"
    assert service.fingerprint(legacy) == service.fingerprint(modern)
    modern["metadata"]["description"] = "New approved"
    assert service.fingerprint(legacy) != service.fingerprint(modern)


@pytest.mark.asyncio
async def test_usage_categories_models_retries_totals_and_history(env):
    with env.sync() as session:
        records = [
            ("analyze", None, None),  # initial character inference
            ("initial_style_bible", None, None),
            ("web_description", None, None),
            ("translate", 1, 1), ("polish", 1, 1), ("analyze", 1, None),
            ("style_bible", 1, None),  # historical sample or episode update: retain association
            ("repair", None, 7),  # chunk association still episode-related
        ]
        for i, (task, fid, cid) in enumerate(records):
            session.add(LlmCall(project_id=env.pid, file_id=fid, subtitle_chunk_id=cid,
                                task=task, model="a" if i % 2 else "b", status="retried" if i == 2 else "succeeded",
                                prompt_tokens=10, completion_tokens=5, cost_usd=0.1))
        session.commit()
        rows = list(session.execute(usage_statement(env.pid)).mappings())
        assert sum(r["calls"] for r in rows) == 8
        assert sum(r["calls"] for r in rows if r["category"] == "Other") == 3
        assert sum(r["calls"] for r in rows if r["category"] == "Episodes") == 5
        assert sum(r["prompt_tokens"] for r in rows) == 80
        assert sum(r["completion_tokens"] for r in rows) == 40
        assert sum(r["cost_usd"] for r in rows) == pytest.approx(0.8)
        assert {r["model"] for r in rows} == {"a", "b"}
