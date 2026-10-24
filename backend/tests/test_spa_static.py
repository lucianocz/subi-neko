import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.main import mount_spa


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

    @app.get("/api/ping")
    async def ping():
        return {"ok": True}

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
    "/%2e%2e%5csecret.txt",
    "/bad%00name",
])
def test_traversal_never_leaks(site, path):
    client, _ = site
    r = client.get(path)
    assert "TOP-SECRET" not in r.text and "EVIL" not in r.text


def test_absolute_path_never_leaks(site):
    client, static = site
    secret = static.parent / "secret.txt"
    response = client.get("/" + secret.as_posix().lstrip("/"))
    assert "TOP-SECRET" not in response.text


@pytest.mark.parametrize("directory", [False, True])
def test_symlink_outside_root_never_leaks(site, directory):
    client, static = site
    target = static.parent / ("static-evil" if directory else "secret.txt")
    try:
        (static / "outside").symlink_to(target, target_is_directory=directory)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation unavailable")
    response = client.get("/outside/x.txt" if directory else "/outside")
    assert "TOP-SECRET" not in response.text and "EVIL" not in response.text


def test_missing_assets_do_not_fall_back_to_spa(site):
    client, _ = site
    response = client.get("/assets/missing.js")
    assert response.status_code == 404
    assert "SPA-INDEX" not in response.text


def test_spa_does_not_shadow_api_routes(site):
    client, _ = site
    assert client.get("/api/ping").json() == {"ok": True}


def test_non_get_request_does_not_return_spa(site):
    client, _ = site
    assert client.post("/projects/1").status_code == 405


def test_static_files_support_head_and_conditional_get(site):
    client, _ = site
    response = client.get("/favicon.svg")
    assert client.head("/favicon.svg").status_code == 200
    assert client.get("/favicon.svg", headers={"If-None-Match": response.headers["etag"]}).status_code == 304
