"""MKV font attachment discovery / extraction / cache (mkvtoolnix mocked)."""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.subs import font_attachments as fa
from tests.font_fixtures import make_font


class FakeMkvtoolnix:
    """Stands in for subprocess.run of mkvmerge -J / mkvextract attachments."""

    def __init__(self, tmp: Path):
        self.attachments = [
            {"id": 1, "file_name": "Stone Sans.ttf", "content_type": "application/x-truetype-font", "size": 1},
            {"id": 2, "file_name": "cover.jpg", "content_type": "image/jpeg", "size": 1},
            {"id": 3, "file_name": "opaque_name", "content_type": "font/otf", "size": 1},
            {"id": 4, "file_name": "chapters.xml", "content_type": "text/xml", "size": 1},
            {"id": 5, "file_name": "Web.WOFF2", "content_type": "application/octet-stream", "size": 1},
        ]
        self.blobs = {
            1: make_font(tmp / "src" / "1.ttf", "Stone Sans").read_bytes(),
            3: make_font(tmp / "src" / "3.otf", "Opaque Face", fmt="otf").read_bytes(),
            5: make_font(tmp / "src" / "5.woff2", "Web Face", fmt="woff2").read_bytes(),
        }
        self.discover_calls = 0
        self.extract_calls: list[list[str]] = []
        self.fail_extract = False
        self.discover_rc = 0
        self.delay = 0.0

    def __call__(self, cmd, capture_output=True, timeout=None, **kw):
        if cmd[0] == "mkvmerge":
            self.discover_calls += 1
            return SimpleNamespace(returncode=self.discover_rc, stderr=b"boom",
                                   stdout=json.dumps({"attachments": self.attachments}).encode())
        assert cmd[0] == "mkvextract" and cmd[2] == "attachments"
        self.extract_calls.append(cmd)
        time.sleep(self.delay)
        if self.fail_extract:
            return SimpleNamespace(returncode=2, stdout=b"Error: nope", stderr=b"")
        for arg in cmd[3:]:
            att_id, out = arg.split(":", 1)
            Path(out).write_bytes(self.blobs[int(att_id)])
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")


@pytest.fixture
def mkv(tmp_path, monkeypatch):
    from app.core.config import settings
    monkeypatch.setattr(settings, "config_root", tmp_path / "config")
    fa._registries.clear()
    fake = FakeMkvtoolnix(tmp_path)
    monkeypatch.setattr(fa.subprocess, "run", fake)
    src = tmp_path / "media" / "ep1.mkv"
    src.parent.mkdir()
    src.write_bytes(b"x" * 100)
    fake.src = src
    fake.cache = tmp_path / "config" / "cache" / "font_attachments"
    yield fake
    fa._registries.clear()


def test_discovery_filters_font_attachments(mkv):
    found = fa.discover_font_attachments(mkv.src)
    assert [(a.id, a.ext) for a in found] == [(1, ".ttf"), (3, ".otf"), (5, ".woff2")]
    assert [a.file_name for a in found][0] == "Stone Sans.ttf"


def test_non_font_attachments_never_extracted(mkv):
    fa.get_attachment_registry(mkv.src)
    args = mkv.extract_calls[0][3:]
    assert sorted(a.split(":")[0] for a in args) == ["1", "3", "5"]
    stored = sorted(p.name for d in mkv.cache.iterdir() for p in d.iterdir())
    assert stored == ["1.ttf", "3.otf", "5.woff2", "manifest.json"]   # no jpg/xml


def test_registry_indexes_internal_names_with_original_filename(mkv):
    reg = fa.get_attachment_registry(mkv.src, project_id=7, file_id=9)
    assert {f.families[0] for f in reg.faces()} == {"Stone Sans", "Opaque Face", "Web Face"}
    face = reg.find("stone sans")[0]
    assert face.filename == "Stone Sans.ttf" and face.source == "attachment"
    assert (face.project_id, face.file_id) == (7, 9)
    assert reg.find("opaque face")[0].media_type == "font/otf"     # ext from MIME


def test_extraction_cached_once_and_reused(mkv):
    a = fa.get_attachment_registry(mkv.src)
    b = fa.get_attachment_registry(mkv.src)
    assert a is b and len(mkv.extract_calls) == 1 and mkv.discover_calls == 1
    fa._registries.clear()                       # new process: reuse the on-disk cache
    c = fa.get_attachment_registry(mkv.src)
    assert len(mkv.extract_calls) == 1 and len(c) == 3


def test_source_change_invalidates_cache(mkv):
    fa.get_attachment_registry(mkv.src)
    old_dirs = {d.name for d in mkv.cache.iterdir()}
    mkv.src.write_bytes(b"y" * 250)              # size changes
    fa.get_attachment_registry(mkv.src)
    assert len(mkv.extract_calls) == 2
    new_dirs = {d.name for d in mkv.cache.iterdir()}
    assert len(new_dirs) == 1 and new_dirs != old_dirs      # stale dir pruned

    st = mkv.src.stat()
    os.utime(mkv.src, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))   # mtime only
    fa.get_attachment_registry(mkv.src)
    assert len(mkv.extract_calls) == 3


def test_concurrent_requests_extract_once_without_partial_cache(mkv):
    mkv.delay = 0.2
    results, errors = [], []

    def go():
        try:
            results.append(fa.get_attachment_registry(mkv.src))
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=go) for _ in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors
    assert len(mkv.extract_calls) == 1
    assert all(len(r) == 3 for r in results)
    names = [d.name for d in mkv.cache.iterdir()]
    assert len(names) == 1 and ".tmp-" not in names[0]
    assert (mkv.cache / names[0] / "manifest.json").is_file()


def test_failed_extraction_is_clear_and_leaves_no_cache(mkv):
    mkv.fail_extract = True
    with pytest.raises(fa.FontAttachmentError, match="mkvextract failed"):
        fa.get_attachment_registry(mkv.src)
    assert list(mkv.cache.iterdir()) == []       # no partial / temp dirs
    mkv.fail_extract = False
    assert len(fa.get_attachment_registry(mkv.src)) == 3       # recovers on retry


def test_discovery_failure_and_missing_tools(mkv, monkeypatch):
    mkv.discover_rc = 2
    with pytest.raises(fa.FontAttachmentError, match="mkvmerge failed"):
        fa.get_attachment_registry(mkv.src)

    def missing(*a, **k):
        raise FileNotFoundError()
    monkeypatch.setattr(fa.subprocess, "run", missing)
    with pytest.raises(fa.FontAttachmentError, match="not installed"):
        fa.get_attachment_registry(mkv.src)
    with pytest.raises(fa.FontAttachmentError, match="not readable"):
        fa.get_attachment_registry(mkv.src.with_name("gone.mkv"))


def test_malformed_attachment_skipped_others_kept(mkv):
    mkv.blobs[3] = b"not a font"
    reg = fa.get_attachment_registry(mkv.src)
    assert {f.families[0] for f in reg.faces()} == {"Stone Sans", "Web Face"}


def test_mkv_without_attachments(mkv):
    mkv.attachments = []
    assert len(fa.get_attachment_registry(mkv.src)) == 0
    assert mkv.extract_calls == []               # nothing to extract
