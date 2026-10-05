"""Direct source-MKV streaming endpoint + the shared safe source-path resolver."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.responses import FileResponse

from app.api.routes import media as media_routes
from app.core.config import settings
from app.core.media_paths import (
    SourceFileMissingError,
    SourcePathError,
    resolve_source_path,
    resolve_source_path_parts,
)
from app.db.models import File
from tests.test_publish import env, seed_project  # noqa: F401

DATA = bytes(range(256)) * 4          # 1024 bytes, every offset distinguishable


@pytest.fixture
def media_env(env, monkeypatch):  # noqa: F811
    monkeypatch.setattr("app.api.routes.media.AsyncSessionLocal", env.asess)
    monkeypatch.setattr(settings, "import_root", env.import_root)
    pid = seed_project(env, n_files=1)
    (env.import_root / "series" / "ep1.mkv").write_bytes(DATA)
    with env.sync() as s:
        fid = s.query(File).filter(File.project_id == pid).one().id
    env.pid, env.fid = pid, fid
    return env


@pytest_asyncio.fixture
async def client(media_env):
    app = FastAPI()
    app.include_router(media_routes.router, prefix="/api")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        yield c


def url(env) -> str:
    return f"/api/projects/{env.pid}/files/{env.fid}/media"


def set_paths(env, *, source_directory=None, relative_path=None) -> None:
    from app.db.models import Project
    with env.sync() as s:
        if source_directory is not None:
            s.get(Project, env.pid).source_directory = source_directory
        if relative_path is not None:
            s.get(File, env.fid).relative_path = relative_path
        s.commit()


# --- A-D: full + ranges ------------------------------------------------------

@pytest.mark.asyncio
async def test_full_file(client, media_env):
    r = await client.get(url(media_env))
    assert r.status_code == 200
    assert r.content == DATA
    assert r.headers["content-type"] == "video/x-matroska"
    assert r.headers["accept-ranges"] == "bytes"
    assert r.headers["content-length"] == str(len(DATA))
    assert "content-disposition" not in r.headers      # inline, not a download


@pytest.mark.asyncio
async def test_range_206(client, media_env):
    r = await client.get(url(media_env), headers={"Range": "bytes=10-19"})
    assert r.status_code == 206
    assert r.content == DATA[10:20]
    assert r.headers["content-range"] == f"bytes 10-19/{len(DATA)}"
    assert r.headers["content-length"] == "10"
    assert r.headers["accept-ranges"] == "bytes"


@pytest.mark.asyncio
async def test_suffix_range(client, media_env):
    r = await client.get(url(media_env), headers={"Range": "bytes=-5"})
    assert r.status_code == 206
    assert r.content == DATA[-5:]
    assert r.headers["content-range"] == f"bytes {len(DATA) - 5}-{len(DATA) - 1}/{len(DATA)}"


@pytest.mark.asyncio
async def test_open_ended_range(client, media_env):
    r = await client.get(url(media_env), headers={"Range": "bytes=1000-"})
    assert r.status_code == 206
    assert r.content == DATA[1000:]
    assert r.headers["content-range"] == f"bytes 1000-{len(DATA) - 1}/{len(DATA)}"


@pytest.mark.asyncio
async def test_unsatisfiable_range_416(client, media_env):
    r = await client.get(url(media_env), headers={"Range": f"bytes={len(DATA) + 10}-"})
    assert r.status_code == 416
    # Starlette omits the "bytes" unit here ("*/N"); framework semantics preserved.
    assert r.headers["content-range"].endswith(f"*/{len(DATA)}")


@pytest.mark.asyncio
async def test_malformed_range_keeps_framework_400(client, media_env):
    r = await client.get(url(media_env), headers={"Range": "bytes=500-100"})
    assert r.status_code == 400


# --- F: HEAD ----------------------------------------------------------------

@pytest.mark.asyncio
async def test_head_returns_metadata_only(client, media_env):
    r = await client.head(url(media_env))
    assert r.status_code == 200
    assert r.content == b""
    assert r.headers["content-length"] == str(len(DATA))
    assert r.headers["accept-ranges"] == "bytes"
    assert r.headers["content-type"] == "video/x-matroska"


@pytest.mark.asyncio
async def test_head_with_range(client, media_env):
    r = await client.head(url(media_env), headers={"Range": "bytes=0-9"})
    assert r.status_code == 206 and r.content == b""
    assert r.headers["content-range"] == f"bytes 0-9/{len(DATA)}"


# --- G-H: relationship + traversal ------------------------------------------

@pytest.mark.asyncio
async def test_project_file_mismatch_404(client, media_env):
    other = seed_project(media_env, n_files=1, source_directory="other")
    r = await client.get(f"/api/projects/{other}/files/{media_env.fid}/media")
    assert r.status_code == 404
    assert (await client.get(f"/api/projects/{media_env.pid}/files/99999/media")).status_code == 404
    assert (await client.get(f"/api/projects/99999/files/{media_env.fid}/media")).status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("rel", [
    "../secret.mkv",                 # sibling inside import root, outside the project
    "../../outside.mkv",             # outside import root
    "sub/../../secret.mkv",
    "..\\secret.mkv",
])
async def test_traversal_in_relative_path_rejected(client, media_env, rel):
    (media_env.import_root / "secret.mkv").write_bytes(b"TOP SECRET")
    (media_env.tmp / "outside.mkv").write_bytes(b"TOP SECRET")
    set_paths(media_env, relative_path=rel)
    r = await client.get(url(media_env))
    assert r.status_code == 404
    assert b"TOP SECRET" not in r.content


@pytest.mark.asyncio
async def test_traversal_in_source_directory_rejected(client, media_env):
    (media_env.tmp / "outside.mkv").write_bytes(b"TOP SECRET")
    set_paths(media_env, source_directory="..", relative_path="outside.mkv")
    assert (await client.get(url(media_env))).status_code == 404


@pytest.mark.asyncio
async def test_absolute_relative_path_rejected(client, media_env):
    outside = media_env.tmp / "abs.mkv"
    outside.write_bytes(b"TOP SECRET")
    set_paths(media_env, relative_path=str(outside))
    r = await client.get(url(media_env))
    assert r.status_code == 404 and b"TOP SECRET" not in r.content


@pytest.mark.asyncio
async def test_symlink_escape_rejected(client, media_env):
    outside = media_env.tmp / "linked.mkv"
    outside.write_bytes(b"TOP SECRET")
    link = media_env.import_root / "series" / "link.mkv"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    set_paths(media_env, relative_path="link.mkv")
    assert (await client.get(url(media_env))).status_code == 404


@pytest.mark.asyncio
async def test_missing_file_and_directory_404(client, media_env):
    set_paths(media_env, relative_path="nope.mkv")
    assert (await client.get(url(media_env))).status_code == 404
    set_paths(media_env, relative_path=".")
    assert (await client.get(url(media_env))).status_code == 404     # a directory is not a file


# --- I: no custom full-file read -------------------------------------------

@pytest.mark.asyncio
async def test_no_full_file_buffering(client, media_env, monkeypatch):
    def boom(self, *a, **k):
        raise AssertionError("route must not read the whole file")
    monkeypatch.setattr(Path, "read_bytes", boom)
    r = await client.get(url(media_env), headers={"Range": "bytes=0-3"})
    assert r.status_code == 206

    resp = await media_routes.stream_source_media(media_env.pid, media_env.fid)
    assert isinstance(resp, FileResponse)               # Starlette streams it in chunks


# --- shared resolver --------------------------------------------------------

def test_resolver_ok_and_errors(tmp_path):
    root = tmp_path / "import"
    (root / "p" / "sub").mkdir(parents=True)
    ok = root / "p" / "sub" / "a.mkv"
    ok.write_bytes(b"1")
    (root / "secret.mkv").write_bytes(b"1")
    proj = SimpleNamespace(source_directory="p")

    assert resolve_source_path(proj, SimpleNamespace(relative_path="sub/a.mkv"), import_root=root) == ok.resolve()
    assert resolve_source_path(proj, SimpleNamespace(relative_path="sub/../sub/a.mkv"), import_root=root) == ok.resolve()
    with pytest.raises(SourceFileMissingError):
        resolve_source_path(proj, SimpleNamespace(relative_path="sub/none.mkv"), import_root=root)
    with pytest.raises(SourceFileMissingError):
        resolve_source_path(proj, SimpleNamespace(relative_path="sub"), import_root=root)
    with pytest.raises(SourcePathError, match="escapes the project"):
        resolve_source_path(proj, SimpleNamespace(relative_path="../secret.mkv"), import_root=root)
    with pytest.raises(SourcePathError, match="escapes import root"):
        resolve_source_path_parts("../elsewhere", "a.mkv", import_root=root)
    with pytest.raises(SourcePathError):
        resolve_source_path_parts("p", str(tmp_path / "abs.mkv"), import_root=root)
    # must_exist=False keeps containment but tolerates a missing file (job handlers)
    assert resolve_source_path_parts("p", "sub/none.mkv", import_root=root, must_exist=False).name == "none.mkv"
    with pytest.raises(SourcePathError):
        resolve_source_path_parts("p", "../secret.mkv", import_root=root, must_exist=False)
    assert issubclass(SourcePathError, ValueError)        # handlers catch ValueError
