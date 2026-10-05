"""QC font manifest + font byte serving (attachments mocked, configured dir real)."""
from __future__ import annotations

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import update

from app.api.routes import media as media_routes
from app.api.routes import projects as projects_routes
from app.api.routes import qc as qc_routes
from app.core.config import settings
from app.db import options as options_store
from app.db.models import File, SubtitleEvent, SubtitleStyle
from app.subs import font_attachments as fa
from app.subs.font_registry import set_configured_registry
from app.subs.qc_fonts import collect_required_families, families_in_text
from tests.font_fixtures import make_font
from tests.test_font_attachments import FakeMkvtoolnix
from tests.test_publish import env, seed_project  # noqa: F401
from tests.test_qc import add_chunks, add_event, file_row


@pytest.fixture
def fenv(env, monkeypatch, tmp_path):  # noqa: F811
    monkeypatch.setattr("app.api.routes.qc.AsyncSessionLocal", env.asess)
    monkeypatch.setattr("app.api.routes.media.AsyncSessionLocal", env.asess)
    monkeypatch.setattr(settings, "import_root", env.import_root)
    monkeypatch.setattr(settings, "config_root", tmp_path / "config")
    monkeypatch.setattr(settings, "fonts_root", tmp_path / "fonts")
    (tmp_path / "fonts").mkdir()
    fa._registries.clear()
    set_configured_registry(None)
    env.mkv = FakeMkvtoolnix(tmp_path)
    monkeypatch.setattr(fa.subprocess, "run", env.mkv)
    pid = seed_project(env, n_files=1)
    env.pid, env.fid = pid, file_row(env, pid).id
    add_chunks(env, env.fid, "complete")
    yield env
    fa._registries.clear()
    set_configured_registry(None)


@pytest_asyncio.fixture
async def client(fenv):
    app = FastAPI()
    app.include_router(projects_routes.router, prefix="/api")
    app.include_router(qc_routes.router, prefix="/api")
    app.include_router(media_routes.router, prefix="/api")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        yield c


def fonts_url(env) -> str:
    return f"/api/projects/{env.pid}/files/{env.fid}/qc/fonts"


def set_style(env, **values) -> None:
    with env.sync() as s:
        s.execute(update(SubtitleStyle).values(**values))
        s.commit()


def add_config_font(env, name: str, family: str, **kw):
    return make_font(settings.fonts_dir / name, family, **kw)


def all_families(body) -> set[str]:
    return {n for f in body["fonts"] for n in f["family_names"]}


# --- pure helpers -----------------------------------------------------------

def test_fn_scanning_is_conservative():
    assert families_in_text(r"{\fnArial Black\b1}Hi {\i1\fn Foo Bar }x") == ["Arial Black", "Foo Bar"]
    assert families_in_text(r"{\fn}reset") == []                    # \fn with no name resets
    assert families_in_text(r"plain \fnNotATag text") == []         # outside {} is text
    assert families_in_text(None) == []


def test_collect_required_prefers_effective_font_and_dedups():
    from types import SimpleNamespace as NS
    styles = [NS(style_name="Default", font_name="Arial", replacement_font_name="Noto Sans",
                 replacement_font_size=None, font_size=20),
              NS(style_name="Unused", font_name="Zapf", replacement_font_name=None,
                 replacement_font_size=None, font_size=20)]
    events = [("Default", r"{\fnarial}x", False), ("Default", r"{\fnNOTO SANS}y", False),
              ("Unused", "hidden only", True)]
    on = collect_required_families(styles, events, use_font_replacements=True)
    assert [n.casefold() for n in on] == ["arial", "noto sans"]      # case-variants collapse
    off = collect_required_families(styles, events, use_font_replacements=False)
    assert [n.casefold() for n in off] == ["arial", "noto sans"]     # n overrides still counted
    assert "Zapf" not in on + off                                    # style only used by hidden events


# --- manifest ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_attached_font_appears(client, fenv):
    set_style(fenv, font_name="Stone Sans", replacement_font_name=None)
    r = await client.get(fonts_url(fenv))
    assert r.status_code == 200
    body = r.json()
    assert "Stone Sans" in all_families(body)
    assert body["missing_families"] == [] and body["attachment_error"] is None
    # every attachment is shipped, even ones the styles do not name (v1: conservative)
    assert {"Opaque Face", "Web Face"} <= all_families(body)
    att = [f for f in body["fonts"] if f["source"] == "attachment"]
    assert att[0]["url"] == f"{fonts_url(fenv)}/{att[0]['id']}"


@pytest.mark.asyncio
async def test_configured_replacement_font_appears(client, fenv):
    set_style(fenv, font_name="Arial", replacement_font_name="Noto Sans")
    add_config_font(fenv, "whatever.otf", "Noto Sans", fmt="otf")
    body = (await client.get(fonts_url(fenv))).json()
    cfg = [f for f in body["fonts"] if f["source"] == "configured"]
    assert [f["family_names"][0] for f in cfg] == ["Noto Sans"]
    assert cfg[0]["url"] == f"/api/fonts/configured/{cfg[0]['id']}"
    assert body["missing_families"] == []
    assert body["replace_incompatible_fonts"] is True
    assert "Noto Sans" in body["required_families"] and "Arial" not in body["required_families"]


@pytest.mark.asyncio
async def test_missing_replacement_reported_not_fatal(client, fenv):
    set_style(fenv, font_name="Arial", replacement_font_name="Absent Font")
    r = await client.get(fonts_url(fenv))
    assert r.status_code == 200
    body = r.json()
    assert body["missing_families"] == ["Absent Font"]
    assert len(body["fonts"]) == 3          # attachments still returned


@pytest.mark.asyncio
async def test_replace_option_on_vs_off(client, fenv):
    set_style(fenv, font_name="Source Face", replacement_font_name="Repl Face")
    add_config_font(fenv, "r.ttf", "Repl Face")
    add_config_font(fenv, "s.ttf", "Source Face")
    await options_store.aset("REPLACE_INCOMPATIBLE_FONTS", "true")
    on = (await client.get(fonts_url(fenv))).json()
    assert on["required_families"] == ["Repl Face"]
    assert "Repl Face" in all_families(on) and "Source Face" not in all_families(on)

    await options_store.aset("REPLACE_INCOMPATIBLE_FONTS", "false")
    off = (await client.get(fonts_url(fenv))).json()
    assert off["replace_incompatible_fonts"] is False
    assert off["required_families"] == ["Source Face"]
    assert "Source Face" in all_families(off) and "Repl Face" not in all_families(off)


@pytest.mark.asyncio
async def test_explicit_fn_family_included(client, fenv):
    add_config_font(fenv, "e.ttf", "Inline Face")
    add_event(fenv, fenv.fid, 1, translated_text=r"{\fnInline Face}Ahoj")
    body = (await client.get(fonts_url(fenv))).json()
    assert "Inline Face" in body["required_families"]
    assert "Inline Face" in all_families(body)


@pytest.mark.asyncio
async def test_hidden_events_do_not_require_fonts(client, fenv):
    add_event(fenv, fenv.fid, 1, translated_text=r"{\fnHidden Face}x", is_hidden=1)
    assert "Hidden Face" not in (await client.get(fonts_url(fenv))).json()["required_families"]


@pytest.mark.asyncio
async def test_no_absolute_paths_leak(client, fenv):
    set_style(fenv, font_name="Stone Sans", replacement_font_name="Noto Sans")
    add_config_font(fenv, "n.ttf", "Noto Sans")
    raw = (await client.get(fonts_url(fenv))).text
    assert str(fenv.tmp) not in raw and str(fenv.tmp).replace("\\", "/") not in raw
    assert str(fenv.tmp).replace("\\", "\\\\") not in raw
    assert '"path"' not in raw


@pytest.mark.asyncio
async def test_font_urls_serve_bytes_with_headers(client, fenv):
    set_style(fenv, font_name="Stone Sans", replacement_font_name="Noto Sans")
    add_config_font(fenv, "n.woff", "Noto Sans", fmt="woff")
    body = (await client.get(fonts_url(fenv))).json()
    assert {f["source"] for f in body["fonts"]} == {"attachment", "configured"}
    for font in body["fonts"]:
        r = await client.get(font["url"])
        assert r.status_code == 200
        assert r.headers["content-type"] == font["media_type"]
        assert "immutable" in r.headers["cache-control"]
        assert "access-control-allow-origin" not in r.headers      # same-origin; nothing worker-hostile
        assert len(r.content) > 100
        head = await client.head(font["url"])
        assert head.status_code == 200 and head.content == b""
    # ids come from content: bytes served are exactly what was registered
    att = next(f for f in body["fonts"] if f["source"] == "attachment" and f["filename"] == "Stone Sans.ttf")
    assert (await client.get(att["url"])).content == fenv.mkv.blobs[1]
    assert att["media_type"] == "font/ttf"


@pytest.mark.asyncio
async def test_unknown_font_ids_404_and_no_path_params(client, fenv):
    assert (await client.get(f"{fonts_url(fenv)}/deadbeef")).status_code == 404
    assert (await client.get("/api/fonts/configured/deadbeef")).status_code == 404
    assert (await client.get("/api/fonts/configured/..%2f..%2fetc%2fpasswd")).status_code == 404
    assert (await client.get(f"{fonts_url(fenv)}/..%2f..%2fsecret")).status_code == 404
    # a configured id is not reachable through the attachment route and vice versa
    p = add_config_font(fenv, "n.ttf", "Noto Sans")
    set_configured = (await client.get("/api/fonts/configured/x")).status_code
    assert set_configured == 404
    from app.subs.font_registry import parse_font_file
    cid = parse_font_file(p, source="configured").id
    assert (await client.get(f"{fonts_url(fenv)}/{cid}")).status_code == 404


@pytest.mark.asyncio
async def test_qc_unavailable_file_rejected(client, fenv):
    with fenv.sync() as s:
        from app.db.models import SubtitleChunk
        s.add(SubtitleChunk(file_id=fenv.fid, chunk_index=5, translate_from_line=0,
                            translate_to_line=0, status="pending"))
        s.commit()
    assert (await client.get(fonts_url(fenv))).status_code == 409
    assert (await client.get(f"{fonts_url(fenv)}/abc")).status_code == 409
    assert (await client.get(f"/api/projects/{fenv.pid}/files/9999/qc/fonts")).status_code == 404


@pytest.mark.asyncio
async def test_attachment_failure_degrades_gracefully(client, fenv):
    set_style(fenv, font_name="Arial", replacement_font_name="Noto Sans")
    add_config_font(fenv, "n.ttf", "Noto Sans")
    fenv.mkv.fail_extract = True
    r = await client.get(fonts_url(fenv))
    assert r.status_code == 200
    body = r.json()
    assert "mkvextract failed" in body["attachment_error"]
    assert [f["source"] for f in body["fonts"]] == ["configured"]
    assert (await client.get(f"{fonts_url(fenv)}/abc")).status_code == 502


@pytest.mark.asyncio
async def test_missing_source_file_degrades(client, fenv):
    (fenv.import_root / "series" / "ep1.mkv").unlink()
    body = (await client.get(fonts_url(fenv))).json()
    assert body["attachment_error"] == "Source media file not found"
    assert str(fenv.tmp) not in str(body)


@pytest.mark.asyncio
async def test_duplicate_family_deterministic_and_all_faces_kept(client, fenv):
    set_style(fenv, font_name="Stone Sans", replacement_font_name="Stone Sans")
    # attachment and configured both expose "Stone Sans": attachment wins; configured
    # bold/regular faces of the same family are NOT mixed in; output order is stable.
    add_config_font(fenv, "b.ttf", "Stone Sans", bold=True)
    add_config_font(fenv, "r.ttf", "Stone Sans")
    first = (await client.get(fonts_url(fenv))).json()
    second = (await client.get(fonts_url(fenv))).json()
    assert first == second
    stone = [f for f in first["fonts"] if "Stone Sans" in f["family_names"]]
    assert {f["source"] for f in stone} == {"attachment"}

    # configured-only family keeps every face (regular + bold), regular first
    set_style(fenv, font_name="Pair", replacement_font_name="Pair")
    add_config_font(fenv, "pb.ttf", "Pair", bold=True)
    add_config_font(fenv, "pr.ttf", "Pair")
    set_configured_registry(None)
    pair = [f for f in (await client.get(fonts_url(fenv))).json()["fonts"] if "Pair" in f["family_names"]]
    assert [f["bold"] for f in pair] == [False, True]
