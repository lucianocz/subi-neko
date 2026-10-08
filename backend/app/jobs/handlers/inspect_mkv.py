from __future__ import annotations

import json
import logging
import re
import subprocess
from datetime import datetime
from typing import Any

from sqlalchemy.orm import selectinload

from app.core.database import SyncSessionLocal
from app.core.languages import track_matches_language
from app.core.media_paths import resolve_source_path_parts
from app.db.models import File
from app.jobs.context import JobContext, JobResult, ProgressFn
from app.jobs.registry import register_job_handler
from app.subs.sidecar import select_sidecar

logger = logging.getLogger(__name__)

# `File.subtitle_track_index` value meaning "external sidecar file, not an MKV
# track"; extract_subtitles re-resolves the sidecar next to the media file.
EXTERNAL_TRACK_INDEX = -1

_SUBTITLE_FORMAT_BY_CODEC_ID = {
    "S_TEXT/ASS": "ass",
    "S_TEXT/SSA": "ass",
    # Matroska stores SubRip subtitles under this codec id.  MediaInfo calls
    # it "UTF-8 Plain Text", even though the extracted payload is SRT.
    "S_TEXT/UTF8": "srt",
}

# Releases commonly ship two English ASS tracks — a signs/songs typesetting
# track and the full dialogue track — in that order (e.g. Judas:
# "English [Signs-Songs]" then "English [Full]"). Picking the first eng track
# silently yields a file with zero dialogue events, so the track *name* has to
# be part of the decision.
_SIGNS_TRACK_NAME_RE = re.compile(r"sign|song|karaoke|lyric|typeset|forced", re.IGNORECASE)
_FULL_TRACK_NAME_RE = re.compile(r"\bfull\b|dialog", re.IGNORECASE)


def _track_rank(track: dict, source_lang_code: str = "en") -> tuple:
    """Sort key for subtitle track preference — lower is better.

    Lexicographic so each signal only breaks ties left by the ones above it:
    language first (the configured source language), ASS/SSA over
    plain text next, then avoid signs/songs tracks, then hearing-impaired and forced flags,
    then prefer an explicit "Full"/"Dialogue" name, and finally keep the
    file's own order.
    """
    props = track.get("properties", {})
    name = props.get("track_name") or ""
    return (
        # Language outranks format: a source-language SRT beats an other-language ASS.
        not track_matches_language(props, source_lang_code),
        # Among equal-language tracks, preserve rich typesetting; plain text
        # is the fallback.
        _SUBTITLE_FORMAT_BY_CODEC_ID.get(props.get("codec_id")) != "ass",
        bool(_SIGNS_TRACK_NAME_RE.search(name)),
        bool(props.get("flag_hearing_impaired", False)),
        bool(props.get("forced_track", False)),
        not _FULL_TRACK_NAME_RE.search(name),
        track.get("id", 0),
    )


def _supported_subtitle_tracks(tracks: list[dict]) -> list[dict]:
    return [
        t for t in tracks
        if t.get("type") == "subtitles"
        and t.get("properties", {}).get("codec_id") in _SUBTITLE_FORMAT_BY_CODEC_ID
    ]


def _pick_subtitle_track(tracks: list[dict], source_lang_code: str = "en") -> dict | None:
    candidates = _supported_subtitle_tracks(tracks)
    if not candidates:
        return None
    return min(candidates, key=lambda t: _track_rank(t, source_lang_code))


def _describe_candidates(tracks: list[dict], source_lang_code: str = "en") -> list[dict]:
    """Every supported candidate, recorded so a mis-picked track
    stays diagnosable after the fact."""
    return [
        {
            "id": t.get("id"),
            "language": t.get("properties", {}).get("language"),
            "track_name": t.get("properties", {}).get("track_name"),
        }
        for t in sorted(_supported_subtitle_tracks(tracks),
                        key=lambda t: _track_rank(t, source_lang_code))
    ]


def _subtitle_format(track: dict) -> str:
    return _SUBTITLE_FORMAT_BY_CODEC_ID[track.get("properties", {}).get("codec_id")]


@register_job_handler("inspect_mkv")
def inspect_mkv(
    payload: dict[str, Any],
    ctx: JobContext,
    progress: ProgressFn,
) -> JobResult:
    file_id: int = payload["file_id"]
    now = datetime.utcnow().isoformat()

    progress(0.05, "Loading file record")

    with SyncSessionLocal() as session:
        file = session.get(File, file_id, options=[selectinload(File.project)])
        if file is None:
            return JobResult(status="failed", result=None,
                             error_code="FILE_NOT_FOUND",
                             error_message=f"File id={file_id} not found")
        source_directory = file.project.source_directory
        relative_path = file.relative_path

    try:
        source_path = resolve_source_path_parts(
            source_directory, relative_path,
            import_root=ctx.import_root, must_exist=False)
    except ValueError as exc:
        return JobResult(status="failed", result=None,
                         error_code="INVALID_PATH", error_message=str(exc))

    source_lang_code = ctx.options.source_lang_code
    logger.info("Selecting source subtitles for %s (source language: %s)",
                relative_path, source_lang_code)

    # External sidecars always outrank embedded tracks.
    progress(0.15, "Looking for external subtitles")
    sidecar = select_sidecar(source_path)
    if sidecar is not None:
        with SyncSessionLocal() as session:
            file = session.get(File, file_id)
            file.subtitle_track_index = EXTERNAL_TRACK_INDEX
            file.detected_subtitle_format = sidecar.format
            file.status = "discovering"
            file.updated_at = now
            session.commit()
        progress(1.0, "Done")
        return JobResult(
            status="succeeded",
            result={
                "subtitle_track_index": EXTERNAL_TRACK_INDEX,
                "format": sidecar.format,
                "external_subtitle": sidecar.path.name,
            },
            error_code=None,
            error_message=None,
        )

    progress(0.2, "Running mkvmerge inspection")

    proc = subprocess.run(
        ["mkvmerge", "-J", str(source_path)],
        capture_output=True,
    )
    if proc.returncode != 0:
        return JobResult(status="failed", result=None,
                         error_code="MKVMERGE_FAILED",
                         error_message=proc.stderr.decode(errors="replace").strip())

    try:
        info = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        return JobResult(status="failed", result=None,
                         error_code="MKVMERGE_PARSE_ERROR",
                         error_message=str(exc))

    progress(0.7, "Selecting subtitle track")

    all_tracks = info.get("tracks", [])
    best = _pick_subtitle_track(all_tracks, source_lang_code)
    if best is None:
        with SyncSessionLocal() as session:
            file = session.get(File, file_id)
            file.status = "failed"
            file.blocking_reason = "subtitle_missing"
            file.updated_at = now
            session.commit()
        return JobResult(status="failed", result=None,
                         error_code="subtitle_missing",
                         error_message="No ASS/SSA or UTF-8 plain-text subtitle track found in file")

    track_id: int = best["id"]
    subtitle_format = _subtitle_format(best)
    best_props = best.get("properties", {})
    logger.info("Selected embedded subtitle track %s (language=%s, codec=%s)",
                track_id, best_props.get("language"), best_props.get("codec_id"))

    progress(0.9, "Saving result")

    with SyncSessionLocal() as session:
        file = session.get(File, file_id)
        file.subtitle_track_index = track_id
        file.detected_subtitle_format = subtitle_format
        file.status = "discovering"
        file.updated_at = now
        session.commit()

    progress(1.0, "Done")
    return JobResult(
        status="succeeded",
        result={
            "subtitle_track_index": track_id,
            "format": subtitle_format,
            "track_name": best.get("properties", {}).get("track_name"),
            "candidates": _describe_candidates(all_tracks, source_lang_code),
        },
        error_code=None,
        error_message=None,
    )
