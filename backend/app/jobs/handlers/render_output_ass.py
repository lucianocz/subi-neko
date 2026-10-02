from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.core.database import SyncSessionLocal
from app.db.models import File, Subtitle, SubtitleEvent, SubtitleStyle, file_subtitle_styles
from app.jobs.context import JobContext, JobResult, ProgressFn
from app.jobs.registry import register_job_handler
from app.subs.ass_rendering import build_ass, save_ass

logger = logging.getLogger(__name__)


def _safe_output_path(ctx: JobContext, source_directory: str, relative_path: str) -> Path:
    candidate = (ctx.output_root / source_directory / relative_path).resolve()
    if not candidate.is_relative_to(ctx.output_root.resolve()):
        raise ValueError(f"Resolved path {candidate} escapes output root")
    return candidate


@register_job_handler("render_output_ass")
def render_output_ass(
    payload: dict[str, Any],
    ctx: JobContext,
    progress: ProgressFn,
) -> JobResult:
    file_id: int = payload["file_id"]

    progress(0.05, "Loading DB records")

    with SyncSessionLocal() as session:
        file = session.get(File, file_id, options=[selectinload(File.project)])
        if file is None:
            return JobResult(status="failed", result=None,
                             error_code="FILE_NOT_FOUND",
                             error_message=f"File id={file_id} not found")

        source_directory = file.project.source_directory
        relative_path = file.relative_path

        subtitle = session.scalar(
            select(Subtitle).where(Subtitle.file_id == file_id)
        )
        if subtitle is None:
            return JobResult(status="failed", result=None,
                             error_code="SUBTITLE_NOT_EXTRACTED",
                             error_message="No subtitle record - run extract_subtitles first")

        styles = session.scalars(
            select(SubtitleStyle)
            .join(file_subtitle_styles, file_subtitle_styles.c.subtitle_style_id == SubtitleStyle.id)
            .where(file_subtitle_styles.c.file_id == file_id)
            .order_by(SubtitleStyle.id)
        ).all()

        events = session.scalars(
            select(SubtitleEvent)
            .where(SubtitleEvent.file_id == file_id)
            .order_by(SubtitleEvent.line_index)
        ).all()

        progress(0.25, "Building ASS structure")
        subs = build_ass(
            subtitle,
            styles,
            events,
            text_variant="translated",
            title=ctx.options.target_lang_name or "",
        )

        try:
            output_ass_path = _safe_output_path(
                ctx, source_directory, str(Path(relative_path).with_suffix(".ass"))
            )
        except ValueError as exc:
            return JobResult(status="failed", result=None,
                             error_code="INVALID_PATH", error_message=str(exc))

        progress(0.75, f"Writing ASS ({len(events)} events, {len(styles)} styles)")
        save_ass(subs, output_ass_path)

    progress(1.0, "Done")
    return JobResult(
        status="succeeded",
        result={
            "output_ass_path": str(output_ass_path),
            "events_written": len(events),
        },
        error_code=None,
        error_message=None,
    )
