import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.main import mount_spa, resolve_static_file


@pytest.fixture()
def site(tmp_path):
    static = tmp_path / "static"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text("<html>SPA-INDEX</html>")
    (static / "favicon.svg").write_text("ICON")
    (tmp_path / "secret.txt").write_text("TOP-SECRET")
    # a sibling dir sharing the static prefix must not count as "inside"
    (tmp_path / "static-evil").mkdir()
    (tmp_path / "static-evil" / "x.txt").write_text("EVIL")
    app = FastAPI()
    mount_spa(app, str(static))
    return TestClient(app), static


def test_valid_spa_routes_fall_back_to_index(site):
    client, _ = site
    for path in ("/", "/projects/1", "/projects/1/files/2/qc"):
        r = client.get(path)
        assert r.status_code == 200 and "SPA-INDEX" in r.text


def test_existing_static_file_served(site):
    client, _ = site
    assert client.get("/favicon.svg").text == "ICON"


@pytest.mark.parametrize("path", [
    "/../secret.txt",
    "/%2e%2e/secret.txt",
    "/..%2fsecret.txt",
    "/%2e%2e%2fsecret.txt",
    "/../static-evil/x.txt",
    "/a/../../secret.txt",
    "/..%5csecret.txt",
])
def test_traversal_never_leaks(site, path):
    client, _ = site
    r = client.get(path)
    assert "TOP-SECRET" not in r.text and "EVIL" not in r.text


def test_resolve_rejects_escape(site):
    _, static = site
    s = str(static)
    assert resolve_static_file(s, "favicon.svg") is not None
    assert resolve_static_file(s, "../secret.txt") is None
    assert resolve_static_file(s, "../static-evil/x.txt") is None
    assert resolve_static_file(s, os.path.abspath(static.parent / "secret.txt")) is None
    assert resolve_static_file(s, "bad\x00name") is None
    assert resolve_static_file(s, "nope.txt") is None
