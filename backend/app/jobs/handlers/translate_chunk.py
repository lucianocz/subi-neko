from __future__ import annotations

import hashlib
import logging
import re

from datetime import datetime
from typing import Any

from sqlalchemy import select

from app.core.database import SyncSessionLocal
from app.db.models import File, ProjectCharacter, ProjectSpeaker, QaItem, SubtitleChunk, SubtitleEvent
from app.jobs.context import JobContext, JobResult, ProgressFn
from app.jobs.handlers.prompt_context import (
    AnalysisContext,
    StyleContext,
    build_character_block,
    build_glossary_block,
    build_scene_block,
    build_speaker_identity_map,
    build_lookahead_lines,
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
from app.jobs.handlers.utils import allows_ai_edit
from app.jobs.registry import register_job_handler
from app.llm import client as llm_client
from app.llm.schemas import TranslateResponse
from app.subs import translation_memory as tm
from app.subs.tag_masking import MaskedLine, force_unmask, mask_line, plain_text, unmask_line

logger = logging.getLogger(__name__)

# Content types where an AI-origin TM hit is safe to auto-apply: OP/ED
# lyrics and signs repeat verbatim across episodes and have no
# conversation-dependent meaning. Dialogue AI hits are suggestions only.
_TM_AI_AUTO_APPLY_CONTENT_TYPES = {"sign", "song", "karaoke"}

# qa_type for multi-fragment typesets; validate_chunk preserves it when it
# resets the review state for a chunk.
FRAGMENT_QA_TYPE = "multi_fragment_sign"

_FRAGMENT_TOKEN_RE = re.compile(r"^[^\W\d_]+$", re.UNICODE)


def _detect_sign_fragments(tgt_snapshot: list[dict]) -> tuple[set[int], dict[str, int]]:
    """Detect typesets split into positioned fragments of one word.

    Sign events sharing exact timestamps whose plain texts are single
    alphabetic tokens with a lowercase-initial continuation ("For" +
    "bidden") render as one on-screen word; translating the fragments
    independently produces broken overlays. Returns the affected line
    indexes and, per concatenated word, the event id to anchor a QA item on.
    """
    by_time: dict[tuple[int, int], list[dict]] = {}
    for e in tgt_snapshot:
        text = plain_text(e["source_text"]).strip()
        if text and _FRAGMENT_TOKEN_RE.match(text):
            by_time.setdefault((e["start_ms"], e["end_ms"]), []).append(e)

    fragment_lines: set[int] = set()
    anchors: dict[str, int] = {}
    for events in by_time.values():
        if len(events) < 2:
            continue
        texts = [plain_text(e["source_text"]).strip() for e in events]
        # Identical texts are layered copies of one word (blur/outline/fill
        # layers), not a mid-word split — safe to translate; the identical-
        # sign grouping propagates one translation to every layer.
        if len(set(texts)) == 1:
            continue
        # A lowercase-initial continuation marks a mid-word split — a
        # standalone sign starts its own word.
        if not any(t[0].islower() for t in texts[1:]):
            continue
        fragment_lines.update(e["line_index"] for e in events)
        anchors.setdefault("".join(texts), events[0]["id"])
    return fragment_lines, anchors


def _prompt_version(system_prompt: str, model: str) -> str:
    return hashlib.sha1(f"{model}\n{system_prompt}".encode("utf-8")).hexdigest()[:12]


def _identity_suffix(identity: tuple[str | None, str | None]) -> str:
    name, gender = identity
    if name and gender:
        return f" ({name}, {gender})"
    if name:
        return f" ({name})"
    return ""


def _build_context_lines(
    context_events: list[dict],
    identities: dict[str, tuple[str | None, str | None]],
) -> list[str]:
    lines = []
    for e in context_events:
        identity = identities.get(e["name"] or "", (e["name"], None))
        suffix = _identity_suffix((identity[0], None))
        src = plain_text(e["source_text"])
        if e["translated_text"]:
            lines.append(f"[CONTEXT] {e['line_index']}{suffix}: {src} => {plain_text(e['translated_text'])}")
        else:
            lines.append(f"[CONTEXT] {e['line_index']}{suffix}: {src}")
    return lines


def _tm_hint(match: tm.TmMatch) -> str:
    """One [TM] hint line.

    An approximate match is labelled as such and shown WITH its own source
    text: at the 92 % fuzzy cutoff the differing few percent can be the part
    that carries the meaning, so the model has to be able to see what it is
    being offered and how it differs — an unlabelled hint reads like an
    established translation and invites a verbatim copy.
    """
    target = plain_text(match.target_text)
    if match.score >= 100.0:
        return f'  [TM] this line previously translated as: "{target}"'
    return (f'  [TM ~{match.score:.0f}% match] the similar line '
            f'"{plain_text(match.tm_source_text)}" was translated as "{target}"')


def _build_target_lines(
    target_events: list[dict],
    masked: dict[int, MaskedLine],
    identities: dict[str, tuple[str | None, str | None]],
    with_identity: bool,
    tm_suggestions: dict[int, tm.TmMatch] | None = None,
    budgets: dict[int, int] | None = None,
) -> list[str]:
    lines = []
    for e in target_events:
        suffix = ""
        if with_identity:
            identity = identities.get(e["name"] or "", (e["name"], None))
            suffix = _identity_suffix(identity)
        budget = (budgets or {}).get(e["line_index"])
        budget_part = f" | max {budget} chars" if budget is not None else ""
        lines.append(
            f"[TARGET] {e['line_index']}{suffix}{budget_part}: {masked[e['line_index']].text}")
        suggestion = (tm_suggestions or {}).get(e["line_index"])
        if suggestion is not None:
            lines.append(_tm_hint(suggestion))
    return lines


@register_job_handler("translate_chunk")
def translate_chunk(
    payload: dict[str, Any],
    ctx: JobContext,
    progress: ProgressFn,
) -> JobResult:
    file_id: int = payload["file_id"]
    chunk_index: int = payload["chunk_index"]
    model: str = payload.get("model") or ctx.options.openai_model_cheap
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
        chunk_id = chunk.id
        content_type = chunk.content_type or "dialogue"

        # Only dialogue chunks get project character/speaker/style context —
        # signs, karaoke and song lyrics are not attributed to a speaker.
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

        # Load subtitle events for target range (scoped to this chunk's own
        # content_type partition — partitions can interleave in line_index space)
        target_events = list(session.scalars(
            select(SubtitleEvent)
            .where(SubtitleEvent.file_id == file_id)
            .where(SubtitleEvent.event_type == "dialogue")
            .where(SubtitleEvent.content_type == content_type)
            .where(SubtitleEvent.line_index >= translate_from)
            .where(SubtitleEvent.line_index <= translate_to)
            .order_by(SubtitleEvent.line_index)
        ).all())

        # Chronological context: the preceding events of this chunk's own
        # partition, with their translations when already available. For
        # dialogue the gate guarantees they are polished, so the window
        # carries the wording that actually ships.
        context_events_rows = load_preceding_context_events(
            session, file_id, content_type,
            before_line=translate_from,
            limit=max(0, ctx.options.prepend_context_size),
        )

        # Source-only lookahead: the lines after this chunk are still
        # untranslated (the dialogue chain runs strictly forward), but their
        # English is enough to stop the chunk's tail being translated blind.
        lookahead_lines = build_lookahead_lines(load_following_context_events(
            session, file_id, content_type,
            after_line=translate_to,
            limit=ctx.options.lookahead_context_size,
        ))

        char_snapshot = list(characters)
        speaker_snapshot = list(unmapped_speakers)
        ctx_snapshot = [
            {
                "line_index": e.line_index,
                "name": e.name,
                "source_text": e.source_text,
                "translated_text": e.translated_text,
            }
            for e in context_events_rows
        ]
        tgt_snapshot = [
            {
                "id": e.id,
                "line_index": e.line_index,
                "name": e.name,
                "source_text": e.source_text,
                "translated_text": e.translated_text,
                "is_user_edited": e.is_user_edited,
                "is_locked": e.is_locked,
                "start_ms": e.start_ms,
                "end_ms": e.end_ms,
            }
            for e in target_events
        ]

        # Translation memory lookup for the whole target range, plus fuzzy
        # near-match suggestions for the lines the exact lookup missed.
        tm_matches: dict[int, tm.TmMatch] = {}
        if project_id:
            source_by_line = {
                e["line_index"]: e["source_text"] for e in tgt_snapshot
                if allows_ai_edit(e["is_user_edited"], e["is_locked"])
            }
            tm_matches = tm.lookup(session, project_id, source_by_line)
            fuzzy_candidates = {
                li: text for li, text in source_by_line.items() if li not in tm_matches
            }
            if fuzzy_candidates:
                tm_matches.update(tm.fuzzy_suggest(session, project_id, fuzzy_candidates))

    if not tgt_snapshot:
        return JobResult(status="failed", result=None,
                         error_code="NO_TARGET_EVENTS",
                         error_message=f"No dialogue events found in target range for chunk {chunk_index}")

    # Partition TM hits: auto-apply vs suggestion. Auto-apply requires the
    # raw source to match exactly (stored markup is guaranteed valid) and
    # either a human-origin entry or non-dialogue content.
    tm_applied: dict[int, tm.TmMatch] = {}
    tm_suggest: dict[int, tm.TmMatch] = {}
    for line_index, match in tm_matches.items():
        if match.exact_raw and (
            match.origin == "human" or content_type in _TM_AI_AUTO_APPLY_CONTENT_TYPES
        ):
            tm_applied[line_index] = match
        else:
            tm_suggest[line_index] = match

    llm_targets = [
        e for e in tgt_snapshot
        if allows_ai_edit(e["is_user_edited"], e["is_locked"])
        and e["line_index"] not in tm_applied
    ]

    # Lines with no translatable text (empty or markup-only) are copied
    # through verbatim — there is nothing to translate and the model can
    # only hallucinate content for them.
    passthrough: dict[int, str] = {
        e["line_index"]: e["source_text"]
        for e in llm_targets if not plain_text(e["source_text"])
    }
    llm_targets = [e for e in llm_targets if e["line_index"] not in passthrough]

    # Multi-fragment typesets pass through untranslated (overriding even a
    # TM hit — a fragment translation in TM is itself broken) and surface
    # once per word to the reviewer.
    fragment_qa: list[dict] = []
    if content_type == "sign":
        editable_snapshot = [
            e for e in tgt_snapshot
            if allows_ai_edit(e["is_user_edited"], e["is_locked"])
        ]
        fragment_lines, fragment_anchors = _detect_sign_fragments(editable_snapshot)
        if fragment_lines:
            for e in tgt_snapshot:
                if e["line_index"] in fragment_lines:
                    passthrough[e["line_index"]] = e["source_text"]
                    tm_applied.pop(e["line_index"], None)
            llm_targets = [e for e in llm_targets if e["line_index"] not in passthrough]
            fragment_qa = [
                dict(
                    file_id=file_id,
                    subtitle_event_id=event_id,
                    severity="info",
                    qa_type=FRAGMENT_QA_TYPE,
                    message=(f'Typeset "{word}" is split into positioned fragments; '
                             "left untranslated — needs manual typesetting."),
                    details_json=None,
                    is_resolved=0,
                    created_at=now,
                )
                for word, event_id in sorted(fragment_anchors.items())
            ]

    progress(0.2, f"Building prompt ({len(llm_targets)} target, {len(tm_applied)} from TM, "
                  f"{len(ctx_snapshot)} context, {len(lookahead_lines)} lookahead lines)")

    if content_type == "sign":
        system_prompt = ctx.options.resolved_sign_translation_prompt().strip()
    elif content_type in ("karaoke", "song"):
        system_prompt = ctx.options.resolved_song_translation_prompt().strip()
    else:
        system_prompt = ctx.options.resolved_translation_prompt().strip()

    prompt_version = _prompt_version(system_prompt, model)

    translation_map: dict[int, str] = {}     # line_index → final unmasked text
    confidence_map: dict[int, float | None] = {}
    marker_failed: dict[int, str] = {}       # line_index → raw masked-space text
    stats = None

    # Identical signs (per-frame animation, repeated captions) are
    # translated once and the result propagated. Grouping is keyed by
    # masked text, so every member's markers match its representative's
    # and the model output can be unmasked against each member's markup.
    group_members: dict[int, list[int]] = {}

    if llm_targets:
        # Deterministic masking: the model never sees raw ASS markup.
        masked: dict[int, MaskedLine] = {
            e["line_index"]: mask_line(e["source_text"]) for e in llm_targets
        }

        if content_type == "sign":
            rep_by_text: dict[str, int] = {}
            for e in llm_targets:
                li = e["line_index"]
                rep = rep_by_text.setdefault(masked[li].text, li)
                if rep != li:
                    group_members.setdefault(rep, []).append(li)
            if group_members:
                member_set = {m for ms in group_members.values() for m in ms}
                llm_targets = [e for e in llm_targets if e["line_index"] not in member_set]

        with_identity = content_type == "dialogue"

        # Reading-speed budget per line, so the first draft is already close
        # to what fits on screen. Without it the cheap model overruns and the
        # polish pass spends its one full-coverage edit on condensing instead
        # of naturalness. Signs and lyrics are glanced at, not read at a
        # sustained rate — no budget for them.
        budgets: dict[int, int] = {}
        if content_type == "dialogue":
            for e in tgt_snapshot:
                budget = char_budget(
                    e["start_ms"], e["end_ms"], ctx.options.cps_limit, e["source_text"])
                if budget is not None:
                    budgets[e["line_index"]] = budget

        dialogue_lines = _build_context_lines(ctx_snapshot, identities)
        # TM-applied lines appear as read-only context in their position so
        # the model keeps continuity with them.
        applied_context = [
            {
                "line_index": e["line_index"],
                "name": e["name"],
                "source_text": e["source_text"],
                "translated_text": tm_applied[e["line_index"]].target_text,
            }
            for e in tgt_snapshot if e["line_index"] in tm_applied
        ]
        protected_context = [
            {
                "line_index": e["line_index"],
                "name": e["name"],
                "source_text": e["source_text"],
                "translated_text": e["translated_text"],
            }
            for e in tgt_snapshot
            if not allows_ai_edit(e["is_user_edited"], e["is_locked"])
        ]
        in_chunk_lines: list[str] = []
        applied_by_line = {c["line_index"]: c for c in applied_context}
        protected_by_line = {c["line_index"]: c for c in protected_context}
        target_by_line_order = {e["line_index"]: e for e in llm_targets}
        for e in tgt_snapshot:
            li = e["line_index"]
            if li in applied_by_line:
                in_chunk_lines += _build_context_lines([applied_by_line[li]], identities)
            elif li in protected_by_line:
                in_chunk_lines += _build_context_lines([protected_by_line[li]], identities)
            elif li in target_by_line_order:
                in_chunk_lines += _build_target_lines(
                    [target_by_line_order[li]], masked, identities, with_identity,
                    tm_suggest, budgets)
        dialogue_lines += in_chunk_lines
        dialogue_lines += lookahead_lines
        dialogue_block = "\n".join(dialogue_lines)

        user_parts = []
        if content_type == "dialogue":
            char_block = build_character_block(char_snapshot)
            speaker_block = build_unmapped_speaker_block(speaker_snapshot)
            if char_block:
                user_parts.append(f"## Characters\n{char_block}")
            if speaker_block:
                user_parts.append(f"## Unmapped Speakers\n{speaker_block}")
            if style is not None and style.has_content:
                speakers_present = {e["name"] for e in tgt_snapshot if e["name"]}
                style_block = build_style_block(style, speakers_present, identities)
                if style_block:
                    user_parts.append(f"## Style\n{style_block}")

        glossary_block = build_glossary_block(
            glossary_terms, [e["source_text"] for e in tgt_snapshot])
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
                analysis, [e["line_index"] for e in llm_targets])
            if tricky_block:
                user_parts.append(f"## Translator Notes\n{tricky_block}")

        user_parts.append(f"## Dialogue\n{dialogue_block}")
        user_message = "\n\n".join(user_parts)

        progress(0.35, f"Calling LLM ({model})")

        source_chars = sum(len(masked[e["line_index"]].text) for e in llm_targets)
        max_completion_tokens = llm_client.completion_budget(source_chars, len(llm_targets))

        try:
            response, stats = llm_client.complete(
                task="translate",
                model=model,
                system=system_prompt,
                user=user_message,
                schema=TranslateResponse,
                options=ctx.options,
                max_completion_tokens=max_completion_tokens,
                project_id=project_id,
                file_id=file_id,
                chunk_id=chunk_id,
            )
        except llm_client.LlmError as exc:
            return JobResult(status="failed", result=None,
                             error_code=exc.code, error_message=exc.message)

        progress(0.7, "Unmasking and verifying markup")

        target_by_line = {e["line_index"]: e for e in llm_targets}
        raw_output: dict[int, str] = {}  # line_index → model output in masked space

        for item in response.translations:
            if item.i not in target_by_line:
                continue
            raw_output[item.i] = item.t
            final_text, errors = unmask_line(item.t, masked[item.i])
            if errors:
                marker_failed[item.i] = item.t
                logger.info("Marker verification failed for line %d: %s", item.i, errors)
            else:
                translation_map[item.i] = final_text
            if item.c is not None:
                confidence_map[item.i] = max(0.0, min(1.0, item.c))

        missing = [
            li for li in target_by_line
            if li not in translation_map and li not in marker_failed
        ]

        # One corrective pass for missing lines and marker-corrupted lines.
        retry_lines = sorted(set(missing) | set(marker_failed))
        if retry_lines:
            progress(0.75, f"Corrective retry for {len(retry_lines)} line(s)")
            retry_targets = [target_by_line[li] for li in retry_lines]
            retry_block = "\n".join(_build_target_lines(
                retry_targets, masked, identities, with_identity, None, budgets))
            retry_message = (
                "The following lines from your previous batch were missing or had "
                "corrupted formatting markers. Retranslate exactly these lines. "
                "Every ⟦n⟧ marker from the source must appear exactly once, and "
                "the counts of ⏎ ␤ ␣ must match the source.\n\n"
                f"{retry_block}"
            )
            try:
                retry_response, _ = llm_client.complete(
                    task="translate",
                    model=model,
                    system=system_prompt,
                    user=retry_message,
                    schema=TranslateResponse,
                    options=ctx.options,
                    max_completion_tokens=max_completion_tokens,
                    project_id=project_id,
                    file_id=file_id,
                    chunk_id=chunk_id,
                )
            except llm_client.LlmError:
                retry_response = None
                logger.warning("Corrective translate retry failed for chunk %d", chunk_index)

            if retry_response is not None:
                for item in retry_response.translations:
                    if item.i not in target_by_line or item.i not in retry_lines:
                        continue
                    final_text, errors = unmask_line(item.t, masked[item.i])
                    if not errors:
                        raw_output[item.i] = item.t
                        translation_map[item.i] = final_text
                        marker_failed.pop(item.i, None)
                        if item.c is not None:
                            confidence_map[item.i] = max(0.0, min(1.0, item.c))
                    elif item.i in marker_failed:
                        raw_output[item.i] = item.t
                        marker_failed[item.i] = item.t

        # Last resort for marker-corrupted lines: best-effort assembly that never
        # loses a tag block; validation will flag anything still wrong.
        for line_index, raw_text in marker_failed.items():
            translation_map[line_index] = force_unmask(raw_text, masked[line_index])

        # Propagate representative translations to their identical-sign
        # group members, unmasking against each member's own markup.
        for rep, members in group_members.items():
            raw = raw_output.get(rep)
            if raw is None:
                continue  # representative never translated; members stay missing
            for member in members:
                if rep in marker_failed:
                    translation_map[member] = force_unmask(raw, masked[member])
                else:
                    final_text, errors = unmask_line(raw, masked[member])
                    translation_map[member] = (
                        final_text if not errors else force_unmask(raw, masked[member])
                    )
                confidence_map[member] = confidence_map.get(rep)

    translation_map.update(passthrough)

    progress(0.85, "Writing translations")

    with SyncSessionLocal() as session:
        for e in tgt_snapshot:
            line_index = e["line_index"]
            if not allows_ai_edit(e["is_user_edited"], e["is_locked"]):
                # Retranslation (stale chunks, mapping corrections) flows
                # around user-touched and explicitly locked lines.
                continue
            tm_match = tm_applied.get(line_index)
            text = tm_match.target_text if tm_match is not None else translation_map.get(line_index)
            if text is None:
                logger.warning("No translation returned for line_index=%d (chunk %d, file %d)",
                               line_index, chunk_index, file_id)
                continue
            event = session.get(SubtitleEvent, e["id"])
            if event is None or not allows_ai_edit(event.is_user_edited, event.is_locked):
                continue
            event.translated_text = text
            if event.original_ai_translated_text is None:
                event.original_ai_translated_text = text
            event.translation_status = "translated"
            event.translation_confidence = confidence_map.get(line_index)
            if tm_match is not None and tm_match.origin == "human":
                # Human-quality reuse: protect it from the polish pass.
                event.is_approved = 1
            event.updated_at = now

        tm.record_uses(session, [m.entry_id for m in tm_applied.values()], now)

        # Fragment notices survive validation resets; guard against
        # duplicates when the chunk is retranslated.
        if fragment_qa:
            anchor_ids = [row["subtitle_event_id"] for row in fragment_qa]
            existing_anchors = set(session.scalars(
                select(QaItem.subtitle_event_id)
                .where(QaItem.subtitle_event_id.in_(anchor_ids))
                .where(QaItem.qa_type == FRAGMENT_QA_TYPE)
            ).all())
            new_rows = [r for r in fragment_qa if r["subtitle_event_id"] not in existing_anchors]
            if new_rows:
                session.execute(QaItem.__table__.insert(), new_rows)

        # Mark chunk as translated
        chunk = session.scalar(
            select(SubtitleChunk)
            .where(SubtitleChunk.file_id == file_id)
            .where(SubtitleChunk.chunk_index == chunk_index)
        )
        if chunk is not None:
            chunk.status = "translated"
            chunk.model = model if llm_targets else "tm"
            chunk.prompt_version = prompt_version
            chunk.updated_at = now

        session.commit()

    progress(1.0, "Done")
    return JobResult(
        status="succeeded",
        result={
            "translated_events": len(translation_map),
            "tm_applied_events": len(tm_applied),
            "tm_suggested_events": len(tm_suggest),
            "context_events": len(ctx_snapshot),
            "lookahead_events": len(lookahead_lines),
            "marker_fallback_events": len(marker_failed),
            "sign_group_propagated": sum(len(m) for m in group_members.values()),
            "fragment_words": len(fragment_qa),
            "model_used": model,
            "response_mode": stats.response_mode if stats else "tm_only",
            "prompt_tokens": stats.prompt_tokens if stats else 0,
            "completion_tokens": stats.completion_tokens if stats else 0,
        },
        error_code=None,
        error_message=None,
    )
