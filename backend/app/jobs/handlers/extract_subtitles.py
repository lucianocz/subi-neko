from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

import pysubs2
from sqlalchemy import delete, insert

from app.core.database import SyncSessionLocal
from app.db.models import File, Subtitle, SubtitleEvent, SubtitleStyle
from app.jobs.context import JobContext, JobResult, ProgressFn
from app.jobs.registry import register_job_handler
from app.subs.content_classification import classify_content_type

logger = logging.getLogger(__name__)

_KNOWN_SCRIPT_INFO_KEYS = {
    "ScriptType", "WrapStyle", "PlayResX", "PlayResY",
    "ScaledBorderAndShadow", "LayoutResX", "LayoutResY",
    "YCbCr Matrix", "Kerning",
}

# SRT carries no canvas or styling information.  Use a conventional 1080p
# ASS canvas and a dialogue size of 5% of its height so the ASS-only output is
# immediately readable and scales predictably in players.
_PLAINTEXT_PLAY_RES_X = 1920
_PLAINTEXT_PLAY_RES_Y = 1080
_PLAINTEXT_FONT_NAME = "Arial"
_PLAINTEXT_FONT_SIZE = 54.0


def _apply_plaintext_defaults(subs: pysubs2.SSAFile) -> None:
    """Give a parsed SRT the metadata it needs for a useful ASS export."""
    subs.info.clear()
    subs.info.update({
        "ScriptType": "v4.00+",
        "WrapStyle": "0",
        "PlayResX": str(_PLAINTEXT_PLAY_RES_X),
        "PlayResY": str(_PLAINTEXT_PLAY_RES_Y),
        "ScaledBorderAndShadow": "yes",
    })
    subs.styles.clear()
    subs.styles["Default"] = pysubs2.SSAStyle(
        fontname=_PLAINTEXT_FONT_NAME,
        fontsize=_PLAINTEXT_FONT_SIZE,
        outline=3.0,
        shadow=0.0,
        alignment=2,
        marginl=60,
        marginr=60,
        marginv=45,
    )
    for event in subs:
        event.style = "Default"


def _color_to_str(c) -> str | None:
    if c is None:
        return None
    return f"&H{c.a:02X}{c.b:02X}{c.g:02X}{c.r:02X}&"


def _safe_source_path(ctx: JobContext, source_directory: str, relative_path: str) -> Path:
    candidate = (ctx.import_root / source_directory / relative_path).resolve()
    if not candidate.is_relative_to(ctx.import_root.resolve()):
        raise ValueError(f"Resolved path {candidate} escapes import root")
    return candidate


def _int(val) -> int | None:
    try:
        return int(val) if val is not None else None
    except (ValueError, TypeError):
        return None


@register_job_handler("extract_subtitles")
def extract_subtitles(
    payload: dict[str, Any],
    ctx: JobContext,
    progress: ProgressFn,
) -> JobResult:
    file_id: int = payload["file_id"]
    now = datetime.utcnow().isoformat()

    progress(0.05, "Loading file record")

    from sqlalchemy.orm import selectinload
    with SyncSessionLocal() as session:
        file = session.get(File, file_id, options=[selectinload(File.project)])
        if file is None:
            return JobResult(status="failed", result=None,
                             error_code="FILE_NOT_FOUND",
                             error_message=f"File id={file_id} not found")
        if file.subtitle_track_index is None:
            return JobResult(status="failed", result=None,
                             error_code="NO_TRACK_INDEX",
                             error_message="subtitle_track_index not set - run inspect_mkv first")
        track_id = file.subtitle_track_index
        subtitle_format = file.detected_subtitle_format or "ass"
        source_directory = file.project.source_directory
        relative_path = file.relative_path

    if subtitle_format not in {"ass", "srt"}:
        return JobResult(status="failed", result=None,
                         error_code="UNSUPPORTED_SUBTITLE_FORMAT",
                         error_message=f"Unsupported subtitle format: {subtitle_format}")

    try:
        source_path = _safe_source_path(ctx, source_directory, relative_path)
    except ValueError as exc:
        return JobResult(status="failed", result=None,
                         error_code="INVALID_PATH", error_message=str(exc))

    progress(0.1, "Extracting subtitle track")

    fd, tmp_path = tempfile.mkstemp(suffix=f".{subtitle_format}")
    os.close(fd)
    _progress_re = re.compile(r'Progress:\s*(\d+)%')
    try:
        proc = subprocess.Popen(
            ["mkvextract", str(source_path), "tracks", f"{track_id}:{tmp_path}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        # Stream stdout to capture mkvextract progress lines
        assert proc.stdout is not None
        for line in proc.stdout:
            m = _progress_re.search(line)
            if m:
                pct = int(m.group(1))
                # Map mkvextract 0–100% to our 0.10–0.32 range
                progress(0.10 + pct * 0.22 / 100, "Extracting subtitle track")
        proc.wait()
        if proc.returncode != 0:
            stderr_out = proc.stderr.read() if proc.stderr else ""
            return JobResult(status="failed", result=None,
                             error_code="MKVEXTRACT_FAILED",
                             error_message=stderr_out.strip())

        progress(0.35, f"Parsing {subtitle_format.upper()} file")

        try:
            subs = pysubs2.load(tmp_path, encoding="utf-8")
            if subtitle_format == "srt":
                _apply_plaintext_defaults(subs)
        except Exception as exc:
            with SyncSessionLocal() as session:
                file = session.get(File, file_id)
                file.blocking_reason = "subtitle_parse_failed"
                file.updated_at = now
                session.commit()
            return JobResult(status="failed", result=None,
                             error_code="subtitle_parse_failed",
                             error_message=str(exc))
    finally:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass

    progress(0.55, "Building rows")

    info = subs.info
    extra = {k: v for k, v in info.items() if k not in _KNOWN_SCRIPT_INFO_KEYS}

    subtitle_row = dict(
        file_id=file_id,
        script_type=info.get("ScriptType"),
        wrap_style=_int(info.get("WrapStyle")),
        play_res_x=_int(info.get("PlayResX")),
        play_res_y=_int(info.get("PlayResY")),
        scaled_border_and_shadow=info.get("ScaledBorderAndShadow"),
        layout_res_x=_int(info.get("LayoutResX")),
        layout_res_y=_int(info.get("LayoutResY")),
        ycbcr_matrix=info.get("YCbCr Matrix"),
        kerning=info.get("Kerning"),
        extra_script_info_json=json.dumps(extra) if extra else None,
        created_at=now,
        updated_at=now,
    )

    style_rows = [
        dict(
            file_id=file_id,
            style_name=name,
            font_name=s.fontname,
            font_size=float(s.fontsize),
            primary_colour=_color_to_str(s.primarycolor),
            secondary_colour=_color_to_str(s.secondarycolor),
            outline_colour=_color_to_str(s.outlinecolor),
            back_colour=_color_to_str(s.backcolor),
            bold=int(s.bold),
            italic=int(s.italic),
            underline=int(s.underline),
            strikeout=int(s.strikeout),
            scale_x=float(s.scalex),
            scale_y=float(s.scaley),
            spacing=float(s.spacing),
            angle=float(s.angle),
            border_style=int(s.borderstyle),
            outline=float(s.outline),
            shadow=float(s.shadow),
            alignment=int(s.alignment),
            margin_l=int(s.marginl),
            margin_r=int(s.marginr),
            margin_v=int(s.marginv),
            encoding=int(s.encoding),
            created_at=now,
            updated_at=now,
        )
        for name, s in subs.styles.items()
    ]

    event_rows = []
    for idx, event in enumerate(subs):
        event_type = event.type.lower()
        style = event.style or ""
        content_type, content_type_reason = classify_content_type(event_type, style, event.text)
        event_rows.append(dict(
            file_id=file_id,
            line_index=idx,
            event_type=event_type,
            content_type=content_type,
            content_type_reason=content_type_reason,
            # 'other' (ASS comments) is never chunked or translated — an
            # honest terminal status instead of a perpetual 'pending'.
            translation_status="skipped" if content_type == "other" else "pending",
            layer=int(event.layer),
            start_ms=int(event.start),
            end_ms=int(event.end),
            style=style,
            name=(event.name.strip() or None) if event.name else None,
            margin_l=int(event.marginl) if event.marginl else None,
            margin_r=int(event.marginr) if event.marginr else None,
            margin_v=int(event.marginv) if event.marginv else None,
            effect=event.effect or None,
            source_text=event.text,
            created_at=now,
            updated_at=now,
        ))

    progress(0.75, f"Writing {len(event_rows)} events, {len(style_rows)} styles")

    with SyncSessionLocal() as session:
        session.execute(delete(SubtitleEvent).where(SubtitleEvent.file_id == file_id))
        session.execute(delete(SubtitleStyle).where(SubtitleStyle.file_id == file_id))
        session.execute(delete(Subtitle).where(Subtitle.file_id == file_id))

        session.execute(insert(Subtitle), [subtitle_row])
        if style_rows:
            session.execute(insert(SubtitleStyle), style_rows)
        if event_rows:
            session.execute(insert(SubtitleEvent), event_rows)

        session.commit()

    progress(1.0, "Done")
    return JobResult(
        status="succeeded",
        result={"events": len(event_rows), "styles": len(style_rows)},
        error_code=None,
        error_message=None,
    )
