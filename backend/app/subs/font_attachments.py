"""Font attachments of a source MKV: discovery, extraction, cache, registry.

Uses mkvtoolnix (already in the runtime image): ``mkvmerge -J`` lists
attachments, ``mkvextract <src> attachments ID:out`` extracts the font ones.
Only supported font types are extracted; images/XML/etc. are ignored.

Cache layout (outside source media, under ``Settings.cache_dir``)::

    font_attachments/<pathkey>-<statkey>/<attachment-id>.<ext> ... manifest.json

* ``pathkey`` = hash of the resolved source path, ``statkey`` = hash of
  (size, mtime_ns): a replaced/modified MKV gets a new directory and the old
  directories for that path are removed. The multi-GB MKV is never hashed.
* Extraction goes to a sibling temp dir and is ``rename``d into place; the
  manifest is written last, so a directory without one is never trusted. Output
  names are generated (``<id>.<ext>``) — attachment names never touch the path.
* A per-fingerprint thread lock stops concurrent requests from extracting twice;
  a cross-process race just loses the rename and discards its temp dir.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path

from app.subs.font_registry import (
    FONT_MEDIA_TYPES,
    FontFace,
    FontParseError,
    FontRegistry,
    parse_font_file,
)

logger = logging.getLogger(__name__)

MKVMERGE_TIMEOUT_S = 60
MKVEXTRACT_TIMEOUT_S = 300

# MIME types muxers use for attached fonts -> extension, for attachments whose
# filename has no useful extension.
_FONT_MIME_TO_EXT = {
    "application/x-truetype-font": ".ttf",
    "application/x-font-ttf": ".ttf",
    "font/ttf": ".ttf",
    "font/sfnt": ".ttf",
    "application/font-sfnt": ".ttf",
    "application/vnd.ms-opentype": ".otf",
    "application/x-font-otf": ".otf",
    "font/otf": ".otf",
    "application/font-woff": ".woff",
    "font/woff": ".woff",
    "font/woff2": ".woff2",
}


class FontAttachmentError(RuntimeError):
    """Attachment discovery/extraction failed (message is client-safe)."""


@dataclass(frozen=True)
class AttachmentInfo:
    id: int
    file_name: str
    mime_type: str
    size: int
    ext: str


def _font_extension(file_name: str, mime_type: str) -> str | None:
    ext = Path(file_name).suffix.lower()
    if ext in FONT_MEDIA_TYPES:
        return ext
    return _FONT_MIME_TO_EXT.get(mime_type.lower())


def discover_font_attachments(source_path: Path) -> list[AttachmentInfo]:
    """Font attachments of ``source_path`` (``mkvmerge -J``)."""
    try:
        proc = subprocess.run(
            ["mkvmerge", "-J", str(source_path)],
            capture_output=True, timeout=MKVMERGE_TIMEOUT_S,
        )
    except FileNotFoundError as exc:
        raise FontAttachmentError("mkvmerge is not installed") from exc
    except subprocess.TimeoutExpired as exc:
        raise FontAttachmentError("mkvmerge timed out reading attachments") from exc
    # mkvmerge exits 1 for warnings but still prints valid JSON; 2 is an error.
    if proc.returncode not in (0, 1):
        raise FontAttachmentError(
            f"mkvmerge failed: {proc.stderr.decode(errors='replace').strip()[:300] or proc.returncode}")
    try:
        info = json.loads(proc.stdout.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        raise FontAttachmentError("mkvmerge returned unreadable output") from exc

    found: list[AttachmentInfo] = []
    for att in info.get("attachments") or []:
        name = str(att.get("file_name") or "")
        mime = str(att.get("content_type") or "")
        ext = _font_extension(name, mime)
        if ext is None or att.get("id") is None:
            continue
        found.append(AttachmentInfo(
            id=int(att["id"]), file_name=name, mime_type=mime,
            size=int(att.get("size") or 0), ext=ext))
    return found


# --- cache ------------------------------------------------------------------

_MANIFEST = "manifest.json"
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()
_registries: dict[str, FontRegistry] = {}


def _lock_for(key: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


def _h(text: str, n: int) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:n]


def _cache_root() -> Path:
    from app.core.config import settings
    return settings.cache_dir / "font_attachments"


def _fingerprint(source_path: Path) -> tuple[str, str]:
    st = source_path.stat()
    return _h(str(source_path), 16), _h(f"{st.st_size}:{st.st_mtime_ns}", 12)


def _extract(source_path: Path, attachments: list[AttachmentInfo], target: Path) -> None:
    root = target.parent
    root.mkdir(parents=True, exist_ok=True)
    tmp = root / f"{target.name}.tmp-{uuid.uuid4().hex}"
    tmp.mkdir()
    try:
        if attachments:
            args = [f"{a.id}:{tmp / f'{a.id}{a.ext}'}" for a in attachments]
            try:
                proc = subprocess.run(
                    ["mkvextract", str(source_path), "attachments", *args],
                    capture_output=True, timeout=MKVEXTRACT_TIMEOUT_S,
                )
            except FileNotFoundError as exc:
                raise FontAttachmentError("mkvextract is not installed") from exc
            except subprocess.TimeoutExpired as exc:
                raise FontAttachmentError("mkvextract timed out extracting fonts") from exc
            if proc.returncode not in (0, 1):
                raise FontAttachmentError(
                    "mkvextract failed: "
                    f"{(proc.stdout + proc.stderr).decode(errors='replace').strip()[:300] or proc.returncode}")
            missing = [a.file_name for a in attachments if not (tmp / f"{a.id}{a.ext}").is_file()]
            if missing:
                raise FontAttachmentError(f"mkvextract did not produce: {', '.join(missing)}")
        manifest = [
            {"id": a.id, "file_name": a.file_name, "mime_type": a.mime_type,
             "stored": f"{a.id}{a.ext}"} for a in attachments
        ]
        (tmp / _MANIFEST).write_text(json.dumps(manifest), encoding="utf-8")
        try:
            os.rename(tmp, target)       # atomic; fails if another process won the race
        except OSError:
            if not (target / _MANIFEST).is_file():
                raise
            shutil.rmtree(tmp, ignore_errors=True)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise


def _prune_stale(root: Path, pathkey: str, keep: str) -> None:
    for entry in root.glob(f"{pathkey}-*"):
        if entry.name != keep and ".tmp-" not in entry.name:
            shutil.rmtree(entry, ignore_errors=True)
    for key in [k for k in _registries if k.startswith(f"{pathkey}-") and k != keep]:
        _registries.pop(key, None)


def get_attachment_registry(
    source_path: Path, *, project_id: int | None = None, file_id: int | None = None,
) -> FontRegistry:
    """Registry of the MKV's font attachments, extracted on first use and cached
    until the file's size/mtime change. Unparseable fonts are skipped (logged).
    Raises ``FontAttachmentError`` when discovery/extraction fails."""
    try:
        pathkey, statkey = _fingerprint(source_path)
    except OSError as exc:
        raise FontAttachmentError(f"Source file is not readable: {exc.strerror}") from exc
    name = f"{pathkey}-{statkey}"
    root = _cache_root()
    target = root / name

    with _lock_for(name):
        cached = _registries.get(name)
        if cached is not None and (target / _MANIFEST).is_file():
            return cached
        if not (target / _MANIFEST).is_file():
            logger.info("Extracting font attachments of %s", source_path.name)
            _extract(source_path, discover_font_attachments(source_path), target)
            _prune_stale(root, pathkey, name)
        try:
            entries = json.loads((target / _MANIFEST).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            shutil.rmtree(target, ignore_errors=True)
            raise FontAttachmentError("Font attachment cache was corrupt; retry") from exc

        registry = FontRegistry()
        for entry in entries:
            try:
                face: FontFace = parse_font_file(
                    target / entry["stored"], source="attachment",
                    filename=entry["file_name"] or entry["stored"],
                    project_id=project_id, file_id=file_id)
            except (FontParseError, OSError, KeyError) as exc:
                logger.warning("Skipping attachment %s: %s", entry.get("file_name"), exc)
                continue
            registry.add(face)
        _registries[name] = registry
        return registry
