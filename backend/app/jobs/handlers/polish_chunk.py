"""Full-coverage naturalness pass on the better model.

First pass (chunk.polish_attempt_count == 0): every translated line in the
chunk is offered for editing against the Czech-editor checklist in
polish.txt. Second pass (needs_polish): only lines flagged by
review_chunk_final, each carrying an explicit "fix:" instruction.

User-edited and locked lines are never touched.
"""
from __future__ import annotations

import json
import logging
import re

from datetime import datetime
from typing import Any

from sqlalchemy import delete, select, update

from app.core.database import SyncSessionLocal
from app.db.models import File, ProjectCharacter, ProjectSpeaker, QaItem, SubtitleChunk, SubtitleEvent
from app.jobs.context import JobContext, JobResult, ProgressFn
from app.jobs.handlers.prompt_context import (
    AnalysisContext,
    StyleContext,
    build_character_block,
    build_glossary_block,
    build_lookahead_lines,
    build_scene_block,
    build_speaker_identity_map,
    build_style_block,
    build_tricky_notes_block,
    build_unmapped_speaker_block,
    char_budget,
    load_analysis_context,
    load_episode_context,
    load_following_context_events,
    load_glossary_terms,
    load_preceding_context_events,
    load_prompt_characters,
    load_style_context,
    load_unmapped_gendered_speakers,
)
from app.jobs.handlers.review_chunk_final import FINAL_REVIEW_QA_TYPES
from app.jobs.handlers.utils import allows_ai_edit
from app.jobs.registry import register_job_handler
from app.llm import client as llm_client
from app.llm.schemas import PolishResponse
from app.subs.czech_checks import check_polish_drift
from app.subs.tag_masking import MaskedLine, mask_line, plain_text, unmask_line

logger = logging.getLogger(__name__)

_CONTEXT_LINES = 5
_TARGET_CONTEXT_RADIUS = 2
_VALID_ISSUE_SEVERITIES = {"warning", "info"}
_CLEAR_UTTERANCE_END_RE = re.compile(r"[.!?…](?:[\"'”’)\]]+)?$")


def _identity_suffix(identity: tuple[str | None, str | None]) -> str:
    name, gender = identity
    if name and gender:
        return f" ({name}, {gender})"
    if name:
        return f" ({name})"
    return ""


def _speaker_key(
    event: dict[str, Any],
    identities: dict[str, tuple[str | None, str | None]],
) -> str:
    """Return the canonical speaker when known, otherwise the raw ASS name."""
    raw = (event.get("name") or "").strip()
    canonical = identities.get(raw, (raw, None))[0] or raw
    return canonical.casefold()


def _clearly_ends_utterance(source_text: str | None) -> bool:
    """A deliberately small English boundary test for adjacent ASS events."""
    return bool(_CLEAR_UTTERANCE_END_RE.search(plain_text(source_text or "").rstrip()))


def select_targeted_support_events(
    events: list[dict[str, Any]],
    primary_lines: set[int],
    identities: dict[str, tuple[str | None, str | None]],
) -> tuple[set[int], dict[int, set[int]]]:
    """Select at most the immediate neighbour on either side of each primary.

    Continuity is established from the English source and canonical speaker
    identity.  Protected/untranslated neighbours remain context, not edit
    targets.  The returned association lets response validation require a
    supporting rewrite to accompany a real correction to its primary.
    """
    if not primary_lines:
        return set(), {}

    ordered = sorted(events, key=lambda e: e["line_index"])
    positions = {event["line_index"]: pos for pos, event in enumerate(ordered)}
    support_to_primaries: dict[int, set[int]] = {}

    for primary_line in sorted(primary_lines):
        pos = positions.get(primary_line)
        if pos is None:
            continue
        primary = ordered[pos]
        primary_speaker = _speaker_key(primary, identities)
        for direction in (-1, 1):
            neighbour_pos = pos + direction
            if neighbour_pos < 0 or neighbour_pos >= len(ordered):
                continue
            neighbour = ordered[neighbour_pos]
            neighbour_line = neighbour["line_index"]
            if neighbour_line in primary_lines:
                continue
            if not (
                neighbour.get("translated_text")
                and allows_ai_edit(neighbour.get("is_user_edited"), neighbour.get("is_locked"))
            ):
                continue
            if _speaker_key(neighbour, identities) != primary_speaker:
                continue

            # For a left neighbour, it must continue into the primary.  For a
            # right neighbour, the primary must continue into it.  This uses
            # source English rather than a potentially defective Czech draft.
            preceding = neighbour if direction < 0 else primary
            if _clearly_ends_utterance(preceding.get("source_text")):
                continue

            support_to_primaries.setdefault(neighbour_line, set()).add(primary_line)

    return set(support_to_primaries), support_to_primaries


@register_job_handler("polish_chunk")
def polish_chunk(
    payload: dict[str, Any],
    ctx: JobContext,
    progress: ProgressFn,
) -> JobResult:
    file_id: int = payload["file_id"]
    chunk_index: int = payload["chunk_index"]
    model: str = payload.get("model") or ctx.options.openai_model_better or ctx.options.openai_model_cheap
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
        if chunk.status not in ("validated", "needs_polish"):
            return JobResult(status="failed", result=None,
                             error_code="CHUNK_NOT_VALIDATED",
                             error_message=(
                                 f"Chunk {chunk_index} has status '{chunk.status}'; "
                                 "polish_chunk requires 'validated' or 'needs_polish'"
                             ))

        translate_from = chunk.translate_from_line
        translate_to = chunk.translate_to_line
        chunk_id = chunk.id
        content_type = chunk.content_type or "dialogue"
        targeted = chunk.polish_attempt_count > 0

        file = session.get(File, file_id)
        project_id = file.project_id if file is not None else None
        characters: list[ProjectCharacter] = []
        unmapped_speakers: list[ProjectSpeaker] = []
        identities: dict[str, tuple[str | None, str | None]] = {}
        style: StyleContext | None = None
        if file is not None and content_type == "dialogue":
            characters = load_prompt_characters(session, file.project_id)
            unmapped_speakers = load_unmapped_gendered_speakers(session, file.project_id)
            identities = build_speaker_identity_map(session, file.project_id)
            style = load_style_context(session, file.project_id)

        glossary_terms = load_glossary_terms(session, project_id) if project_id else []
        analysis: AnalysisContext | None = load_analysis_context(session, file_id)
        episode_line = load_episode_context(session, file) if file is not None else None

        target_events = list(session.scalars(
            select(SubtitleEvent)
            .where(SubtitleEvent.file_id == file_id)
            .where(SubtitleEvent.event_type == "dialogue")
            .where(SubtitleEvent.content_type == content_type)
            .where(SubtitleEvent.line_index >= translate_from)
            .where(SubtitleEvent.line_index <= translate_to)
            .order_by(SubtitleEvent.line_index)
        ).all())

        tgt_snapshot = [
            {
                "id": e.id,
                "line_index": e.line_index,
                "name": e.name,
                "source_text": e.source_text,
                "translated_text": e.translated_text,
                "start_ms": e.start_ms,
                "end_ms": e.end_ms,
                "is_user_edited": e.is_user_edited,
                "is_locked": e.is_locked,
            }
            for e in target_events
        ]

        # Targeted re-pass: restrict to lines flagged by review_chunk_final
        # and carry the flag messages as explicit fix instructions.
        fix_notes: dict[int, list[str]] = {}
        if targeted:
            event_ids = [e["id"] for e in tgt_snapshot]
            flagged = list(session.scalars(
                select(QaItem)
                .where(QaItem.subtitle_event_id.in_(event_ids))
                .where(QaItem.is_resolved == 0)
                .where(QaItem.qa_type.in_(sorted(FINAL_REVIEW_QA_TYPES)))
            ).all()) if event_ids else []
            id_to_line = {e["id"]: e["line_index"] for e in tgt_snapshot}
            for qa in flagged:
                line = id_to_line.get(qa.subtitle_event_id)
                if line is not None:
                    fix_notes.setdefault(line, []).append(f"{qa.qa_type}: {qa.message}")

        # Preceding lines of this chunk's own partition as continuity context.
        context_rows = load_preceding_context_events(
            session, file_id, content_type,
            before_line=translate_from,
            limit=_CONTEXT_LINES,
        )
        ctx_snapshot = [
            {
                "line_index": e.line_index,
                "name": e.name,
                "source_text": e.source_text,
                "translated_text": e.translated_text,
            }
            for e in context_rows
        ]

        # Source-only lookahead — the lines after the chunk, so an edit to
        # its last line can still account for what follows.
        lookahead_lines = build_lookahead_lines(load_following_context_events(
            session, file_id, content_type,
            after_line=translate_to,
            limit=ctx.options.lookahead_context_size,
        ))

    eligible = [
        e for e in tgt_snapshot
        if e["translated_text"] and allows_ai_edit(e["is_user_edited"], e["is_locked"])
    ]
    primary_lines: set[int] = set()
    support_lines: set[int] = set()
    support_to_primaries: dict[int, set[int]] = {}
    if targeted:
        primary_lines = {e["line_index"] for e in eligible if e["line_index"] in fix_notes}
        if content_type == "dialogue":
            support_lines, support_to_primaries = select_targeted_support_events(
                tgt_snapshot, primary_lines, identities,
            )
        authorized_lines = primary_lines | support_lines
        editable = [e for e in eligible if e["line_index"] in authorized_lines]
    else:
        editable = eligible

    if not editable:
        # Nothing to polish (all lines user-edited/locked, or targeted pass
        # with no remaining flags) — advance without an LLM call.
        _advance_chunk(file_id, chunk_index, model, now)
        progress(1.0, "Nothing to polish")
        return JobResult(status="succeeded",
                         result={"polished_events": 0, "edits_applied": 0, "issues_created": 0},
                         error_code=None, error_message=None)

    progress(0.2, f"Building polish prompt ({len(editable)} lines, targeted={targeted})")

    system_prompt = ctx.options.resolved_polish_prompt().strip()
    cps_limit = ctx.options.cps_limit

    # The DRAFT (current translation) is what the model edits, so masking is
    # keyed to the draft's own markup.
    masked: dict[int, MaskedLine] = {
        e["line_index"]: mask_line(e["translated_text"]) for e in editable
    }

    # Identical signs (per-frame animation, repeated captions) are polished
    # once and the edit propagated. Grouping is keyed by masked draft text,
    # so every member's markers match its representative's.
    all_editable_by_line = {e["line_index"]: e for e in editable}
    group_members: dict[int, list[int]] = {}
    if content_type == "sign":
        rep_by_text: dict[str, int] = {}
        for e in editable:
            li = e["line_index"]
            rep = rep_by_text.setdefault(masked[li].text, li)
            if rep != li:
                group_members.setdefault(rep, []).append(li)
        if group_members:
            member_set = {m for ms in group_members.values() for m in ms}
            editable = [e for e in editable if e["line_index"] not in member_set]

    lines: list[str] = []
    for e in ctx_snapshot:
        identity = identities.get(e["name"] or "", (e["name"], None))
        suffix = _identity_suffix((identity[0], None))
        if e["translated_text"]:
            lines.append(f"[CONTEXT] {e['line_index']}{suffix}: "
                         f"{plain_text(e['source_text'])} => {plain_text(e['translated_text'])}")
        else:
            lines.append(f"[CONTEXT] {e['line_index']}{suffix}: {plain_text(e['source_text'])}")

    # A targeted request also shows a narrow read-only window inside the
    # chunk.  Those rows are useful dialogue context but are never accepted
    # from the response.  Normal full-coverage payloads remain unchanged.
    targeted_context_lines: set[int] = set()
    if targeted:
        ordered_lines = [e["line_index"] for e in tgt_snapshot]
        line_positions = {line: pos for pos, line in enumerate(ordered_lines)}
        for editable_line in primary_lines | support_lines:
            pos = line_positions[editable_line]
            start = max(0, pos - _TARGET_CONTEXT_RADIUS)
            end = min(len(ordered_lines), pos + _TARGET_CONTEXT_RADIUS + 1)
            targeted_context_lines.update(ordered_lines[start:end])
        targeted_context_lines -= primary_lines | support_lines

    editable_by_index = {e["line_index"]: e for e in editable}
    target_by_index = {e["line_index"]: e for e in tgt_snapshot}
    payload_lines = (
        sorted(set(editable_by_index) | targeted_context_lines)
        if targeted else [e["line_index"] for e in editable]
    )
    for line_index in payload_lines:
        if targeted and line_index in targeted_context_lines:
            e = target_by_index[line_index]
            identity = identities.get(e["name"] or "", (e["name"], None))
            suffix = _identity_suffix((identity[0], None))
            if e["translated_text"]:
                lines.append(f"[CONTEXT] {line_index}{suffix}: "
                             f"{plain_text(e['source_text'])} => {plain_text(e['translated_text'])}")
            else:
                lines.append(f"[CONTEXT] {line_index}{suffix}: {plain_text(e['source_text'])}")
            continue

        e = editable_by_index[line_index]
        identity = identities.get(e["name"] or "", (e["name"], None))
        suffix = _identity_suffix(identity)
        # Signs/lyrics have no CPS reading constraint — they are glanced at,
        # and short animation frames would yield absurd budgets.
        budget = (
            char_budget(e["start_ms"], e["end_ms"], cps_limit, e["source_text"])
            if content_type == "dialogue" else None
        )
        budget_part = f" | max {budget} chars" if budget is not None else ""
        lines.append(f"[LINE] {e['line_index']}{suffix}{budget_part}:")
        lines.append(f"  SOURCE: {plain_text(e['source_text'])}")
        lines.append(f"  DRAFT: {masked[e['line_index']].text}")
        for note in fix_notes.get(e["line_index"], []):
            lines.append(f"  fix: {note}")
        if targeted and e["line_index"] in support_lines:
            lines.append("  support: Adjacent part of a flagged utterance. "
                         "Edit only if necessary to make the primary correction coherent.")

    lines += lookahead_lines

    user_parts = []
    if targeted:
        user_parts.append(
            "## Targeted correction rules\n"
            "Primary [LINE] events with a `fix:` note are the requested corrections. "
            "A [LINE] event marked `support:` is optional: change it only when directly "
            "necessary to make a primary correction coherent, and otherwise leave it "
            "unchanged. Any supporting edit must preserve source meaning and formatting. "
            "Never return edits for [CONTEXT] or [AHEAD] events."
        )
    if content_type == "dialogue":
        char_block = build_character_block(characters)
        speaker_block = build_unmapped_speaker_block(unmapped_speakers)
        if char_block:
            user_parts.append(f"## Characters\n{char_block}")
        if speaker_block:
            user_parts.append(f"## Unmapped Speakers\n{speaker_block}")
        if style is not None and style.has_content:
            speakers_present = {e["name"] for e in editable if e["name"]}
            style_block = build_style_block(style, speakers_present, identities)
            if style_block:
                user_parts.append(f"## Style\n{style_block}")

    # Distinctive established terms, for the drift check below. Short ones
    # are skipped: they match inside other words and would only add noise.
    glossary_targets = [
        t.target_term for t in glossary_terms
        if t.target_term and len(t.target_term) >= 3
    ]

    glossary_block = build_glossary_block(
        glossary_terms, [e["source_text"] for e in editable])
    if glossary_block:
        user_parts.append(
            "## Glossary\nEstablished translations — follow them exactly, "
            f"including vocative forms:\n{glossary_block}")

    if episode_line:
        user_parts.append(f"## Episode\n{episode_line}")

    if analysis is not None:
        scene_block = build_scene_block(analysis, translate_from, translate_to)
        if scene_block:
            user_parts.append(f"## Story Context\n{scene_block}")
        tricky_block = build_tricky_notes_block(
            analysis, [e["line_index"] for e in editable])
        if tricky_block:
            user_parts.append(f"## Translator Notes\n{tricky_block}")

    user_parts.append("## Lines\n" + "\n".join(lines))
    user_message = "\n\n".join(user_parts)

    progress(0.4, f"Calling LLM ({model})")

    draft_chars = sum(len(masked[e["line_index"]].text) for e in editable)
    max_completion_tokens = llm_client.completion_budget(draft_chars, len(editable))

    try:
        response, stats = llm_client.complete(
            task="polish",
            model=model,
            system=system_prompt,
            user=user_message,
            schema=PolishResponse,
            options=ctx.options,
            max_completion_tokens=max_completion_tokens,
            project_id=project_id,
            file_id=file_id,
            chunk_id=chunk_id,
        )
    except llm_client.LlmError as exc:
        return JobResult(status="failed", result=None,
                         error_code=exc.code, error_message=exc.message)

    if targeted:
        returned_lines = [edit.i for edit in response.edits]
        duplicate_lines = sorted({line for line in returned_lines if returned_lines.count(line) > 1})
        unauthorized_lines = sorted(set(returned_lines) - set(editable_by_index))
        if duplicate_lines or unauthorized_lines:
            details = []
            if duplicate_lines:
                details.append(f"duplicate edit indices: {duplicate_lines}")
            if unauthorized_lines:
                details.append(f"unauthorized edit indices: {unauthorized_lines}")
            return JobResult(
                status="failed", result=None,
                error_code="INVALID_POLISH_RESPONSE",
                error_message="Targeted Polish response contains " + "; ".join(details),
            )

    progress(0.75, "Applying edits")

    editable_by_line = {e["line_index"]: e for e in editable}
    edits_applied = 0
    edits_skipped = 0
    drift_flagged = 0
    audit_rows: list[dict] = []
    issue_rows: list[dict] = []

    edit_map: dict[int, tuple[str, str]] = {}  # line_index → (final_text, reason)
    for edit in response.edits:
        e = editable_by_line.get(edit.i)
        if e is None:
            continue
        # Line-break counts are not enforced: rows are re-flowed by the
        # auto line-breaker later in the pipeline.
        final_text, errors = unmask_line(edit.t, masked[edit.i], check_line_breaks=False)
        if errors:
            # A polish edit that corrupts markup is dropped — the validated
            # draft stays in place.
            logger.info("Polish edit for line %d dropped (marker errors: %s)", edit.i, errors)
            if targeted:
                return JobResult(
                    status="failed", result=None,
                    error_code="INVALID_POLISH_RESPONSE",
                    error_message=(
                        f"Targeted Polish edit for line {edit.i} has invalid formatting markers"
                    ),
                )
            edits_skipped += 1
            continue
        if final_text == e["translated_text"]:
            continue
        edit_map[edit.i] = (final_text, edit.reason)
        # Propagate the representative's edit to its identical-sign group.
        for member in group_members.get(edit.i, []):
            member_text, member_errors = unmask_line(
                edit.t, masked[member], check_line_breaks=False)
            if member_errors:
                continue
            if member_text != all_editable_by_line[member]["translated_text"]:
                edit_map[member] = (member_text, edit.reason)

    if targeted:
        effective_primary_lines = set(edit_map) & primary_lines
        edit_map = {
            line: value for line, value in edit_map.items()
            if line in primary_lines
            or bool(support_to_primaries.get(line, set()) & effective_primary_lines)
        }

    for issue in response.issues:
        e = editable_by_line.get(issue.i)
        if e is None:
            continue
        if targeted and issue.i in support_lines and issue.i not in edit_map:
            continue
        severity = issue.severity if issue.severity in _VALID_ISSUE_SEVERITIES else "warning"
        if content_type != "dialogue":
            # Sign/lyric observations are review hints, never gating warnings —
            # one per group representative, not per animation frame.
            severity = "info"
        issue_rows.append(dict(
            file_id=file_id,
            subtitle_event_id=e["id"],
            severity=severity,
            qa_type=f"polish_{issue.category}" if issue.category else "polish_issue",
            message=issue.comment,
            details_json=None,
            is_resolved=0,
            created_at=now,
        ))

    with SyncSessionLocal() as session:
        # A fresh polish pass supersedes the previous pass's issue notes —
        # without this they accumulate once per attempt.
        if targeted:
            cleanup_lines = primary_lines | (set(edit_map) & support_lines)
            processed_ids = [
                e["id"] for line, e in all_editable_by_line.items()
                if line in cleanup_lines
            ]
        else:
            processed_ids = [e["id"] for e in all_editable_by_line.values()]
        if processed_ids:
            session.execute(
                delete(QaItem).where(
                    QaItem.subtitle_event_id.in_(processed_ids),
                    QaItem.qa_type.like("polish_%"),
                    QaItem.qa_type != "polish_edit",
                    QaItem.is_resolved == 0,
                )
            )

        # Re-check every target before making any write.  If a user protected
        # one while the LLM was running, fail the whole targeted correction
        # instead of committing only half of a coordinated rewrite.
        pending_events: dict[int, SubtitleEvent] = {}
        for line_index in edit_map:
            e = all_editable_by_line[line_index]
            event = session.get(SubtitleEvent, e["id"])
            if event is None or not allows_ai_edit(event.is_user_edited, event.is_locked):
                if targeted:
                    session.rollback()
                    return JobResult(
                        status="failed", result=None,
                        error_code="POLISH_TARGET_CHANGED",
                        error_message=(
                            f"Targeted Polish event {line_index} became unavailable before persistence"
                        ),
                    )
                continue
            pending_events[line_index] = event

        for line_index, (final_text, reason) in edit_map.items():
            e = all_editable_by_line[line_index]
            event = pending_events.get(line_index)
            if event is None:
                continue
            previous = event.translated_text
            event.translated_text = final_text
            # The polished text is the AI pipeline's final output — keep the
            # revert-to-AI reference in sync with it.
            event.original_ai_translated_text = final_text
            # The stored confidence was the cheap model's opinion of a draft
            # that no longer exists; keeping it would have review_chunk_final
            # flag (or clear) this line on a judgement of different text.
            event.translation_confidence = None
            event.updated_at = now
            edits_applied += 1

            # Nothing else in the pipeline checks that a polish rewrite
            # preserved the meaning — validation is markup-only and the final
            # review judges the result on its own, not against the draft.
            for qa_type, message, details in check_polish_drift(
                previous or "", final_text, reason, glossary_targets,
            ):
                drift_flagged += 1
                issue_rows.append(dict(
                    file_id=file_id,
                    subtitle_event_id=e["id"],
                    severity="warning" if content_type == "dialogue" else "info",
                    qa_type=qa_type,
                    message=message,
                    details_json=json.dumps(details),
                    is_resolved=0,
                    created_at=now,
                ))
            audit_rows.append(dict(
                file_id=file_id,
                subtitle_event_id=e["id"],
                severity="info",
                qa_type="polish_edit",
                message=f"Polish edit ({reason})",
                details_json=json.dumps({"before": previous, "after": final_text, "reason": reason}),
                is_resolved=1,
                resolution_note="auto",
                created_at=now,
                resolved_at=now,
            ))

        if audit_rows:
            session.execute(QaItem.__table__.insert(), audit_rows)
        if issue_rows:
            session.execute(QaItem.__table__.insert(), issue_rows)

        # Targeted pass: the review findings that drove an applied edit are
        # consumed — resolve them so the UI doesn't show fixed issues while
        # the follow-up review is still pending (it re-flags anything left).
        if targeted and edit_map:
            edited_ids = [
                all_editable_by_line[li]["id"]
                for li in edit_map if li in primary_lines
            ]
            session.execute(
                update(QaItem)
                .where(
                    QaItem.subtitle_event_id.in_(edited_ids),
                    QaItem.qa_type.in_(sorted(FINAL_REVIEW_QA_TYPES)),
                    QaItem.is_resolved == 0,
                )
                .values(is_resolved=1, resolution_note="superseded_by_polish",
                        resolved_at=now)
            )

        chunk = session.scalar(
            select(SubtitleChunk)
            .where(SubtitleChunk.file_id == file_id)
            .where(SubtitleChunk.chunk_index == chunk_index)
        )
        if chunk is not None:
            chunk.status = "polished"
            chunk.polish_attempt_count = (chunk.polish_attempt_count or 0) + 1
            chunk.model = model
            chunk.updated_at = now

        session.commit()

    progress(1.0, "Done")
    return JobResult(
        status="succeeded",
        result={
            "polished_events": len(editable),
            "edits_applied": edits_applied,
            "edits_skipped": edits_skipped,
            "issues_created": len(issue_rows),
            "drift_flagged": drift_flagged,
            "targeted": targeted,
            "response_mode": stats.response_mode,
        },
        error_code=None,
        error_message=None,
    )


def _advance_chunk(file_id: int, chunk_index: int, model: str, now: str) -> None:
    with SyncSessionLocal() as session:
        chunk = session.scalar(
            select(SubtitleChunk)
            .where(SubtitleChunk.file_id == file_id)
            .where(SubtitleChunk.chunk_index == chunk_index)
        )
        if chunk is not None:
            chunk.status = "polished"
            chunk.polish_attempt_count = (chunk.polish_attempt_count or 0) + 1
            chunk.updated_at = now
        session.commit()
