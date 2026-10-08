"""External (sidecar) source subtitles stored next to the media file.

Convention: a sidecar is ``<video stem>.<ext>`` — the video's exact basename
with only the extension swapped. No language/qualifier suffixes, no fuzzy
matching. Every match is assumed to be in the configured source language and
always outranks embedded MKV tracks.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

import pysubs2

logger = logging.getLogger(__name__)

# Extension -> (rank, pysubs2 format or None for content autodetection).
# ASS/SSA first (rich typesetting), then plain-text formats; the rank also
# makes the order among several sidecars deterministic.
_EXTENSIONS: dict[str, tuple[int, str | None]] = {
    ".ass": (0, "ass"),
    ".ssa": (1, "ssa"),
    ".srt": (2, "srt"),
    ".vtt": (3, "vtt"),
    ".sub": (4, None),  # text .sub only (MicroDVD / MPL2); VobSub is rejected
}

# MicroDVD frame timing needs a frame rate when the file doesn't declare one.
_FALLBACK_FPS = 23.976

# Start of an MPEG program stream — the container of VobSub (.sub + .idx) data.
_MPEG_PS_MAGIC = b"\x00\x00\x01\xba"


class SidecarError(Exception):
    """A sidecar that cannot be used as a text subtitle."""


@dataclass
class Sidecar:
    path: Path
    subs: pysubs2.SSAFile
    # "ass" for ASS/SSA (styles kept); "srt" for every plain-text format.
    format: str


def find_sidecars(video_path: Path) -> list[Path]:
    """Exact-basename subtitle files beside ``video_path``, best first.

    Not dependent on directory enumeration order.
    """
    stem = video_path.stem
    found: list[tuple[int, str, Path]] = []
    try:
        entries = list(os.scandir(video_path.parent))
    except OSError:
        return []
    for entry in entries:
        path = Path(entry.name)
        ext = path.suffix.lower()
        if ext not in _EXTENSIONS or path.stem != stem:
            continue
        try:
            if not entry.is_file():
                continue
        except OSError:
            continue
        found.append((_EXTENSIONS[ext][0], entry.name, Path(entry.path)))
    return [p for _, _, p in sorted(found, key=lambda t: (t[0], t[1]))]


def load_sidecar(path: Path) -> Sidecar:
    """Parse one sidecar into pysubs2's normalized form or raise SidecarError."""
    ext = path.suffix.lower()
    fmt = _EXTENSIONS[ext][1]
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise SidecarError(f"unreadable: {exc}") from exc
    if raw.startswith(_MPEG_PS_MAGIC) or b"\x00" in raw[:4096]:
        raise SidecarError("binary data (not a text subtitle; VobSub is unsupported)")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise SidecarError("not valid UTF-8") from exc
    # pysubs2.load reads with universal newlines; from_string does not.
    text = "\n".join(text.splitlines()) + "\n"
    try:
        subs = pysubs2.SSAFile.from_string(text, format_=fmt, fps=_FALLBACK_FPS)
    except Exception as exc:  # pysubs2 raises several parse/detection errors
        raise SidecarError(f"unparseable: {exc}") from exc
    if not any(not e.is_comment for e in subs):
        raise SidecarError("contains no subtitle events")
    return Sidecar(path=path, subs=subs, format="ass" if fmt in ("ass", "ssa") else "srt")


def select_sidecar(video_path: Path) -> Sidecar | None:
    """Best usable sidecar for ``video_path``, or None (use embedded tracks).

    Candidates that fail to parse are logged and skipped.
    """
    candidates = find_sidecars(video_path)
    if not candidates:
        logger.info("No external subtitle found for %s", video_path.name)
        return None
    for path in candidates:
        try:
            sidecar = load_sidecar(path)
        except SidecarError as exc:
            logger.warning("Skipping external subtitle %s: %s", path.name, exc)
            continue
        logger.info("Using external subtitle %s (%s)", path.name, sidecar.format)
        return sidecar
    logger.info("No usable external subtitle for %s; falling back to embedded tracks",
                video_path.name)
    return None
