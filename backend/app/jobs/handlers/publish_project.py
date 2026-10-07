"""Explicit project publish: render every accepted file's translated ASS and mux
it into the original MKV.

This replaces the old per-file ``render_output_ass`` / ``mux_output_mkv`` pair,
which the orchestrator started automatically once every file was accepted. Now
the only entry point is the Publish action (``orchestrator.publish.start_publish``),
one job per click, keyed by ``publish_project:{project_id}:{attempt}`` so a
completed run never blocks a deliberate re-publish.

Safety properties:

* the revision to publish (``Project.output_revision``) is captured at job start,
  and all file data is read in one in-memory pass right after, so later edits can
  neither leak half-way into the output nor be mislabelled as published;
* everything is *staged* first (temp ASS + temp MKV beside the final paths, same
  filesystem). Only when every file muxed successfully are the temp files moved
  over the finals with ``os.replace``. A failed run therefore never leaves a
  half-written MKV and leaves the previously published output untouched.
  Limitation: the promotion loop itself is per-file atomic, not project-atomic —
  if ``os.replace`` fails part-way (e.g. a player holds an output open on
  Windows), earlier files already carry the new output.
"""
from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import pysubs2
from sqlalchemy import select, update

from app.core.database import SyncSessionLocal
from app.db.models import (
    File,
    FileBranding,
    Project,
    Subtitle,
    SubtitleEvent,
    SubtitleStyle,
    file_subtitle_styles,
)
from app.db.output_state import ACCEPTED_FOR_OUTPUT, PublishState
from app.jobs.context import JobContext, JobResult, ProgressFn
from app.jobs.registry import register_job_handler
from app.subs.ass_rendering import build_ass, save_ass
from app.subs.branding import BrandingError, spec_from_row

logger = logging.getLogger(__name__)

_MAX_ERROR_LEN = 2000


class PublishError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class _StagedFile:
    relative_path: str
    source_path: Path
    final_ass: Path
    final_mkv: Path
    tmp_ass: Path
    tmp_mkv: Path
    subs: pysubs2.SSAFile | None = None


def _safe_path(root: Path, source_directory: str, relative_path: str) -> Path:
    candidate = (root / source_directory / relative_path).resolve()
    if not candidate.is_relative_to(root.resolve()):
        raise ValueError(f"Resolved path {candidate} escapes root {root}")
    return candidate


def _fenced_update(project_id: int, attempt: int, **values: Any) -> None:
    """Write publish_* fields only while this run is still the project's
    current attempt (a superseded run must not touch the state)."""
    with SyncSessionLocal() as session:
        session.execute(
            update(Project)
            .where(Project.id == project_id, Project.publish_attempt == attempt)
            .values(updated_at=datetime.utcnow().isoformat(), **values)
            .execution_options(synchronize_session=False)
        )
        session.commit()


def _record_failure(project_id: int, attempt: int, message: str) -> None:
    _fenced_update(
        project_id, attempt,
        publish_state=PublishState.FAILED.value,
        publish_error=message[:_MAX_ERROR_LEN],
    )


@register_job_handler("publish_project")
def publish_project(
    payload: dict[str, Any],
    ctx: JobContext,
    progress: ProgressFn,
) -> JobResult:
    project_id: int = payload["project_id"]
    attempt: int = payload["publish_attempt"]
    try:
        return _publish(project_id, attempt, ctx, progress)
    except PublishError as exc:
        logger.warning("Publish failed for project %d: [%s] %s", project_id, exc.code, exc.message)
        _record_failure(project_id, attempt, exc.message)
        return JobResult(status="failed", result=None,
                         error_code=exc.code, error_message=exc.message)
    except Exception as exc:  # noqa: BLE001 — surface anything as a FAILED publish
        logger.exception("Publish crashed for project %d", project_id)
        message = f"{type(exc).__name__}: {exc}"
        _record_failure(project_id, attempt, message)
        return JobResult(status="failed", result=None,
                         error_code="UNEXPECTED_ERROR", error_message=message)


def _publish(
    project_id: int, attempt: int, ctx: JobContext, progress: ProgressFn,
) -> JobResult:
    progress(0.02, "Capturing output revision")

    # --- 1. capture the revision and read everything, in memory, in one pass ---
    with SyncSessionLocal() as session:
        project = session.get(Project, project_id)
        if project is None:
            return JobResult(status="failed", result=None, error_code="PROJECT_NOT_FOUND",
                             error_message=f"Project id={project_id} not found")
        if (
            project.publish_attempt != attempt
            or project.publish_state != PublishState.PUBLISHING.value
        ):
            logger.info(
                "Publish run %d for project %d is superseded (current attempt=%s state=%s) — skipping",
                attempt, project_id, project.publish_attempt, project.publish_state,
            )
            return JobResult(status="succeeded", result={"skipped": "superseded"},
                             error_code=None, error_message=None)

        target_revision = project.output_revision
        source_directory = project.source_directory
        project.publish_target_revision = target_revision
        session.commit()

        files = list(session.scalars(
            select(File).where(File.project_id == project_id).order_by(File.relative_path)
        ).all())
        if not files:
            raise PublishError("NO_FILES", "Project has no files to publish")
        not_accepted = [f.relative_path for f in files if f.status not in ACCEPTED_FOR_OUTPUT]
        if not_accepted:
            raise PublishError(
                "FILES_NOT_ACCEPTED",
                f"{len(not_accepted)} file(s) are not accepted (e.g. {not_accepted[0]})",
            )

        staged: list[_StagedFile] = []
        for f in files:
            try:
                source_path = _safe_path(ctx.import_root, source_directory, f.relative_path)
                final_mkv = _safe_path(ctx.output_root, source_directory, f.relative_path)
                final_ass = final_mkv.with_suffix(".ass")
            except ValueError as exc:
                raise PublishError("INVALID_PATH", str(exc)) from exc
            if final_mkv == source_path:
                raise PublishError(
                    "OUTPUT_EQUALS_SOURCE",
                    f"Output path equals the source file ({source_path}); "
                    "OUTPUT_ROOT must differ from IMPORT_ROOT",
                )

            subtitle = session.scalar(select(Subtitle).where(Subtitle.file_id == f.id))
            if subtitle is None:
                raise PublishError("SUBTITLE_NOT_EXTRACTED",
                                   f"No subtitle record for {f.relative_path}")
            styles = session.scalars(
                select(SubtitleStyle)
                .join(file_subtitle_styles,
                      file_subtitle_styles.c.subtitle_style_id == SubtitleStyle.id)
                .where(file_subtitle_styles.c.file_id == f.id)
                .order_by(SubtitleStyle.id)
            ).all()
            events = session.scalars(
                select(SubtitleEvent)
                .where(SubtitleEvent.file_id == f.id)
                .order_by(SubtitleEvent.line_index)
            ).all()
            # Existing authoritative rendering path (incl. enabled branding).
            branding = spec_from_row(session.scalar(
                select(FileBranding).where(FileBranding.file_id == f.id)))
            try:
                subs = build_ass(
                    subtitle, styles, events,
                    text_variant="translated",
                    title=ctx.options.target_lang_name or "",
                    use_font_replacements=ctx.options.replace_incompatible_fonts,
                    branding=branding,
                )
            except BrandingError as exc:
                raise PublishError("BRANDING_FAILED", f"{f.relative_path}: {exc}") from exc
            staged.append(_StagedFile(
                relative_path=f.relative_path,
                source_path=source_path,
                final_ass=final_ass,
                final_mkv=final_mkv,
                tmp_ass=final_ass.with_name(f"{final_ass.stem}.publish-tmp.ass"),
                tmp_mkv=final_mkv.with_name(f"{final_mkv.stem}.publish-tmp.mkv"),
                subs=subs,
            ))

    warnings: list[str] = []
    try:
        # --- 2. stage: render + mux every file to temp paths ---------------------
        total = len(staged)
        for i, item in enumerate(staged):
            progress(0.05 + 0.85 * (i / total), f"Muxing {item.relative_path} ({i + 1}/{total})")
            warnings.extend(_stage_file(item, ctx))
            item.subs = None  # release the in-memory document

        # --- 3. promote: only reached when every file staged successfully --------
        progress(0.92, "Replacing published output")
        for item in staged:
            try:
                os.replace(item.tmp_ass, item.final_ass)
                os.replace(item.tmp_mkv, item.final_mkv)
            except OSError as exc:
                raise PublishError(
                    "OUTPUT_REPLACE_FAILED",
                    f"Could not replace output for {item.relative_path}: {exc}",
                ) from exc
    finally:
        for item in staged:
            for tmp in (item.tmp_ass, item.tmp_mkv):
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    logger.warning("Could not remove staging file %s", tmp)

    # --- 4. success: record exactly the revision that was captured -----------------
    _fenced_update(
        project_id, attempt,
        publish_state=PublishState.PUBLISHED.value,
        published_revision=target_revision,
        published_at=datetime.utcnow().isoformat(),
        publish_error=None,
    )
    progress(1.0, "Done")
    return JobResult(
        status="succeeded",
        result={
            "revision": target_revision,
            "files_published": len(staged),
            "warnings": warnings,
        },
        error_code=None,
        error_message=None,
    )


def _stage_file(item: _StagedFile, ctx: JobContext) -> list[str]:
    """Write the temp ASS and mux the temp MKV. Returns mkvmerge warnings."""
    if not item.source_path.exists():
        raise PublishError("SOURCE_MISSING", f"Source MKV not found: {item.source_path}")
    item.final_mkv.parent.mkdir(parents=True, exist_ok=True)
    save_ass(item.subs, item.tmp_ass)

    lang_name = ctx.options.target_lang_name or "Unknown"
    lang_code = ctx.options.target_lang_code or "und"
    try:
        proc = subprocess.run(
            [
                "mkvmerge",
                "-o", str(item.tmp_mkv),
                str(item.source_path),
                "--track-name", f"0:{lang_name}",
                "--language", f"0:{lang_code}",
                "--default-track", "0:yes",
                str(item.tmp_ass),
            ],
            capture_output=True,
        )
    except FileNotFoundError as exc:
        raise PublishError("MKVMERGE_NOT_FOUND", "mkvmerge is not installed or not on PATH") from exc

    stdout = proc.stdout.decode(errors="replace") if proc.stdout else ""
    stderr = proc.stderr.decode(errors="replace") if proc.stderr else ""

    # mkvmerge exit codes (MKVToolNix docs): 0 = success, 1 = finished but at
    # least one warning was emitted (the output is complete and valid),
    # 2 = error. Only 2 (or anything unexpected) fails the publish.
    if proc.returncode not in (0, 1):
        errors = [ln for ln in stdout.splitlines() if ln.startswith("Error:")]
        detail = "; ".join(errors) or stderr.strip() or stdout.strip() or "no output"
        raise PublishError(
            "MKVMERGE_FAILED",
            f"mkvmerge failed for {item.relative_path} (exit {proc.returncode}): {detail}",
        )

    warnings: list[str] = []
    if proc.returncode == 1:
        warnings = [
            f"{item.relative_path}: {ln.strip()}"
            for ln in stdout.splitlines() if ln.startswith("Warning:")
        ] or [f"{item.relative_path}: mkvmerge finished with warnings"]
        for w in warnings:
            logger.warning("mkvmerge warning — %s", w)

    if not item.tmp_mkv.exists() or item.tmp_mkv.stat().st_size == 0:
        raise PublishError("MKVMERGE_NO_OUTPUT",
                           f"mkvmerge produced no output for {item.relative_path}")
    return warnings
