from __future__ import annotations

import json
import logging
import re
from collections import Counter
from datetime import datetime
from typing import Any

from sqlalchemy import delete, select

from app.core.database import SyncSessionLocal
from app.db.models import File, QaItem, SubtitleChunk, SubtitleEvent
from app.jobs.context import JobContext, JobResult, ProgressFn
from app.jobs.handlers.translate_chunk import FRAGMENT_QA_TYPE
from app.jobs.handlers.utils import allows_ai_edit
from app.jobs.registry import register_job_handler
from app.subs.tag_masking import plain_text

logger = logging.getLogger(__name__)

# Patterns for text corruption detection
_CORRUPTION_PREFIXES = re.compile(
    r"^\s*(translation\s*:|note\s*:|translator\s*:|output\s*:|result\s*:)",
    re.IGNORECASE,
)
_MARKDOWN_FENCE = re.compile(r"```")
_BROKEN_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_LEADING_ASS_OVERRIDE_BLOCKS = re.compile(r"^\s*(?:\{\\[^}]*\}\s*)+")


# ---------------------------------------------------------------------------
# Individual checks — each returns a list of (qa_type, message, details)
# An empty list means the event passed.
# ---------------------------------------------------------------------------

def _check_missing_translation(event: SubtitleEvent) -> list[tuple[str, str, dict]]:
    if not plain_text(event.source_text or ""):
        # Nothing translatable in the source (empty or markup-only line) —
        # an empty translation is the correct outcome, not an error.
        return []
    if event.translated_text is None or not event.translated_text.strip():
        return [("missing_translation", "Translated text is empty.", {})]
    return []


def _ass_override_blocks(text: str) -> tuple[list[str], bool]:
    """Return ASS override blocks and whether an override block is unclosed.

    ASS override tags are blocks starting with "{\\". Other braces can appear
    in source event text from real-world files, so validation only treats ASS
    override spans as formatting that must be preserved.
    """
    blocks: list[str] = []
    malformed = False
    pos = 0
    while pos < len(text):
        start = text.find("{\\", pos)
        if start < 0:
            break
        end = text.find("}", start + 2)
        if end < 0:
            malformed = True
            break
        blocks.append(text[start:end + 1])
        pos = end + 1
    return blocks, malformed


def _check_formatting_tag_mismatch(event: SubtitleEvent) -> list[tuple[str, str, dict]]:
    """Every override block from the source must survive verbatim in the
    translation. Order is NOT enforced: tag masking reinserts blocks where
    the model placed their markers, so an inline tag legitimately moves with
    the word it wraps."""
    src = event.source_text or ""
    tgt = event.translated_text or ""

    src_blocks, src_malformed = _ass_override_blocks(src)
    tgt_blocks, tgt_malformed = _ass_override_blocks(tgt)

    details: dict[str, Any] = {}
    failed = False

    if Counter(src_blocks) != Counter(tgt_blocks):
        details["source_blocks"] = src_blocks
        details["translated_blocks"] = tgt_blocks
        failed = True

    if src_malformed or tgt_malformed:
        if src_malformed:
            details["source_unclosed_override_block"] = True
        if tgt_malformed:
            details["translated_unclosed_override_block"] = True
        details["unclosed_block"] = True
        failed = True

    if failed:
        return [("formatting_tag_mismatch",
                 "ASS formatting tags are missing or malformed.",
                 details)]
    return []


_TAG_NAME_RE = re.compile(r"\\([A-Za-z]+)")


def _tag_name_multiset(text: str) -> Counter:
    """Count ASS override tag names (ignoring numeric params and order)."""
    blocks, _ = _ass_override_blocks(text)
    names: list[str] = []
    for block in blocks:
        names.extend(_TAG_NAME_RE.findall(block))
    return Counter(names)


def _check_formatting_tag_mismatch_relaxed(event: SubtitleEvent) -> list[tuple[str, str, dict]]:
    """Like _check_formatting_tag_mismatch but tolerant of reordering/regrouping
    of tags — used for sign/song content, where reflowing on-screen or lyric
    text can legitimately change tag block boundaries without changing meaning.
    Still requires the same multiset of tag names and catches unclosed blocks.
    """
    src = event.source_text or ""
    tgt = event.translated_text or ""

    _, src_malformed = _ass_override_blocks(src)
    _, tgt_malformed = _ass_override_blocks(tgt)

    details: dict[str, Any] = {}
    failed = False

    if src_malformed or tgt_malformed:
        if src_malformed:
            details["source_unclosed_override_block"] = True
        if tgt_malformed:
            details["translated_unclosed_override_block"] = True
        details["unclosed_block"] = True
        failed = True

    src_multiset = _tag_name_multiset(src)
    tgt_multiset = _tag_name_multiset(tgt)
    if src_multiset != tgt_multiset:
        details["source_tag_counts"] = dict(src_multiset)
        details["translated_tag_counts"] = dict(tgt_multiset)
        failed = True

    if failed:
        return [("formatting_tag_mismatch",
                 "ASS formatting tags are missing or malformed.",
                 details)]
    return []


# \h is deliberately absent: hard spaces are presentation padding whose
# count legitimately changes with translated word widths (column alignment).
_ASS_ESCAPES = [r"\N", r"\n"]


_ESCAPE_LABELS = {r"\N": r"hard breaks (\N)", r"\n": r"soft breaks (\n)"}


def check_escape_mismatch(source_text: str, translated_text: str) -> list[tuple[str, str, dict]]:
    """Shared with review_chunk_final, which re-runs this after auto line
    breaking may have changed the row count validate_chunk originally saw.
    """
    src = source_text or ""
    tgt = translated_text or ""

    details: dict[str, Any] = {}
    parts: list[str] = []

    for esc in _ASS_ESCAPES:
        src_count = src.count(esc)
        tgt_count = tgt.count(esc)
        if src_count != tgt_count:
            details[esc] = {"source": src_count, "translated": tgt_count}
            # Name the actual difference and the row count a reviewer sees on
            # screen — this is a reflow notice, not a markup syntax defect.
            parts.append(
                f"{_ESCAPE_LABELS[esc]} {src_count} → {tgt_count}, "
                f"rendering on {tgt_count + 1} rows instead of {src_count + 1}"
            )

    if parts:
        return [("escape_mismatch",
                 f"Translation reflowed the line breaks: {'; '.join(parts)}. "
                 "Check that the on-screen layout still fits.",
                 details)]
    return []


def _check_escape_mismatch(event: SubtitleEvent) -> list[tuple[str, str, dict]]:
    return check_escape_mismatch(event.source_text, event.translated_text)


def _check_locked_line_modified(event: SubtitleEvent) -> list[tuple[str, str, dict]]:
    if not event.is_locked:
        return []
    if event.translated_text != event.source_text:
        return [("locked_line_modified",
                 "Locked event was modified.",
                 {"source_text": event.source_text,
                  "translated_text": event.translated_text})]
    return []


def _check_text_corruption(event: SubtitleEvent) -> list[tuple[str, str, dict]]:
    text = event.translated_text or ""
    text_for_prefix_checks = _LEADING_ASS_OVERRIDE_BLOCKS.sub("", text)
    reasons = []

    if _CORRUPTION_PREFIXES.match(text_for_prefix_checks):
        reasons.append("assistant_prefix")
    if _MARKDOWN_FENCE.search(text):
        reasons.append("markdown_fence")
    if _is_json_output(text_for_prefix_checks):
        reasons.append("json_like_output")
    if _BROKEN_CONTROL.search(text):
        reasons.append("broken_control_characters")

    if reasons:
        return [("text_corruption",
                 "Translated text appears corrupted or contains non-subtitle output.",
                 {"reasons": reasons})]
    return []


def _is_json_output(text: str) -> bool:
    stripped = text.strip()
    if not stripped or stripped[0] not in "[{":
        return False
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        return False
    return isinstance(parsed, (dict, list))


# escape_mismatch (⏎/␤ line-break count changed) is surfaced for manual
# review rather than auto-repaired: on-screen sign/song reflow and
# legitimate stylistic rewrites can change line-break counts without the
# translation being wrong, unlike a genuine markup/syntax defect. It does
# not fail the event, block chunk progression, or trigger repair_chunk;
# AUTO_ACCEPT_POLICY (no_blockers) lets it through auto-accept too.
_NON_BLOCKING_QA_TYPES = {"escape_mismatch"}

# Severity for non-blocking types; anything not listed defaults to "warning".
# A reflowed line is a notice about layout, not a defect in the translation,
# so it lands at "info" — its message names the actual row-count change.
_NON_BLOCKING_SEVERITY = {"escape_mismatch": "info"}


def _is_blocking(qa_type: str) -> bool:
    return qa_type not in _NON_BLOCKING_QA_TYPES


def _severity_for(qa_type: str) -> str:
    if _is_blocking(qa_type):
        return "blocker"
    return _NON_BLOCKING_SEVERITY.get(qa_type, "warning")


_CHECKS = [
    _check_missing_translation,
    _check_formatting_tag_mismatch,
    _check_escape_mismatch,
    _check_locked_line_modified,
    _check_text_corruption,
]

# sign/song: on-screen or lyric text can legitimately reflow tag blocks when
# translated, so tag-order/grouping is not enforced — only that the same set
# of tag names survives (see _check_formatting_tag_mismatch_relaxed).
_CHECKS_RELAXED_TAGS = [
    _check_missing_translation,
    _check_formatting_tag_mismatch_relaxed,
    _check_escape_mismatch,
    _check_locked_line_modified,
    _check_text_corruption,
]

# karaoke: per-syllable \k timing tags cannot be preserved 1:1 across a
# translation (target language syllable structure differs) — this is
# expected, not a translation error, so the tag check is skipped entirely.
_CHECKS_NO_TAG_CHECK = [
    _check_missing_translation,
    _check_escape_mismatch,
    _check_locked_line_modified,
    _check_text_corruption,
]


def _checks_for_content_type(content_type: str) -> list:
    if content_type == "karaoke":
        return _CHECKS_NO_TAG_CHECK
    if content_type in ("sign", "song"):
        return _CHECKS_RELAXED_TAGS
    return _CHECKS


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------

@register_job_handler("validate_chunk")
def validate_chunk(
    payload: dict[str, Any],
    ctx: JobContext,
    progress: ProgressFn,
) -> JobResult:
    file_id: int = payload["file_id"]
    chunk_index: int = payload["chunk_index"]
    now = datetime.utcnow().isoformat()

    progress(0.05, "Loading chunk definition")

    with SyncSessionLocal() as session:
        chunk = session.scalar(
            select(SubtitleChunk)
            .where(SubtitleChunk.file_id == file_id)
            .where(SubtitleChunk.chunk_index == chunk_index)
        )
        if chunk is None:
            return JobResult(status="failed", result=None,
                             error_code="CHUNK_NOT_FOUND",
                             error_message=f"Chunk {chunk_index} for file {file_id} not found")

        translate_from = chunk.translate_from_line
        translate_to = chunk.translate_to_line
        content_type = chunk.content_type or "dialogue"

        target_events: list[SubtitleEvent] = list(session.scalars(
            select(SubtitleEvent)
            .where(SubtitleEvent.file_id == file_id)
            .where(SubtitleEvent.line_index >= translate_from)
            .where(SubtitleEvent.line_index <= translate_to)
            .where(SubtitleEvent.event_type == "dialogue")
            .where(SubtitleEvent.content_type == content_type)
            .order_by(SubtitleEvent.line_index)
        ).all())

        # Snapshot data before closing session
        events_snapshot = [
            {
                "id": e.id,
                "line_index": e.line_index,
                "source_text": e.source_text,
                "translated_text": e.translated_text,
                "is_user_edited": e.is_user_edited,
                "is_locked": e.is_locked,
            }
            for e in target_events
        ]

    if not events_snapshot:
        return JobResult(status="failed", result=None,
                         error_code="NO_TARGET_EVENTS",
                         error_message=f"No dialogue events in target range for chunk {chunk_index}")

    progress(0.2, f"Validating {len(events_snapshot)} events")

    # Run checks in-memory — no DB access needed
    collected_errors: list[dict] = []
    failed_event_ids: set[int] = set()
    checks = _checks_for_content_type(content_type)

    for snap in events_snapshot:
        event_errors: list[tuple[str, str, dict]] = []

        # Build a lightweight proxy object for check functions
        class _Proxy:
            translated_text = snap["translated_text"]
            source_text = snap["source_text"]
            is_locked = snap["is_locked"]

        proxy = _Proxy()

        for check_fn in checks:
            event_errors.extend(check_fn(proxy))  # type: ignore[arg-type]

        if event_errors:
            if (allows_ai_edit(snap["is_user_edited"], snap["is_locked"])
                    and any(_is_blocking(qa_type) for qa_type, _, _ in event_errors)):
                failed_event_ids.add(snap["id"])
            for qa_type, message, details in event_errors:
                collected_errors.append(dict(
                    file_id=file_id,
                    subtitle_event_id=snap["id"],
                    severity=_severity_for(qa_type),
                    qa_type=qa_type,
                    message=message,
                    details_json=json.dumps(details) if details else None,
                    is_resolved=0,
                    created_at=now,
                ))

    target_event_ids = [s["id"] for s in events_snapshot]
    # Only blocking errors reject the event / fail the chunk and drive
    # repair_chunk — non-blocking types (see _NON_BLOCKING_QA_TYPES) still
    # produce a QA item for review but let the chunk proceed normally.
    has_errors = len(failed_event_ids) > 0
    error_types = sorted({e["qa_type"] for e in collected_errors})

    progress(0.6, f"Writing results ({len(collected_errors)} errors)")

    with SyncSessionLocal() as session:
        # Delete all unresolved qa_items for target events (validation resets
        # the full review state). Multi-fragment typeset notices come from
        # translate_chunk, which runs before this reset — preserve them.
        if target_event_ids:
            session.execute(
                delete(QaItem).where(
                    QaItem.subtitle_event_id.in_(target_event_ids),
                    QaItem.is_resolved == 0,
                    QaItem.qa_type != FRAGMENT_QA_TYPE,
                )
            )

        # Recheck protection flags in the current transaction. A user can
        # protect a row while validation is running; such a row may retain a
        # QA finding, but must never be routed into automatic Repair.
        active_failed_event_ids: set[int] = set()
        for snap in events_snapshot:
            event = session.get(SubtitleEvent, snap["id"])
            if event is None:
                continue
            failed = (
                snap["id"] in failed_event_ids
                and allows_ai_edit(event.is_user_edited, event.is_locked)
            )
            if failed:
                active_failed_event_ids.add(snap["id"])
            event.translation_status = "rejected" if failed else "validated"
            event.updated_at = now

        # Insert new qa_items
        if collected_errors:
            session.execute(QaItem.__table__.insert(), collected_errors)

        # Update chunk status
        chunk = session.scalar(
            select(SubtitleChunk)
            .where(SubtitleChunk.file_id == file_id)
            .where(SubtitleChunk.chunk_index == chunk_index)
        )
        if chunk is not None:
            if active_failed_event_ids:
                # First repair attempt: allow repair; further failures stop here.
                if chunk.repair_attempt_count == 0:
                    chunk.status = "validate_trans_failed"
                else:
                    chunk.status = "validate_repair_failed"
            else:
                chunk.status = "validated"
            chunk.updated_at = now

        session.commit()

    has_errors = bool(active_failed_event_ids)

    progress(1.0, "Done")
    return JobResult(
        status="succeeded",
        result={
            "valid": not has_errors,
            "validated_events": len(events_snapshot),
            "error_count": len(collected_errors),
            "error_types": error_types,
        },
        error_code=None,
        error_message=None,
    )
