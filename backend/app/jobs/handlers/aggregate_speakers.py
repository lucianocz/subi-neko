from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.core.database import SyncSessionLocal
from app.db.models import File, Project, ProjectSpeaker, SubtitleEvent
from app.jobs.context import JobContext, JobResult, ProgressFn
from app.jobs.registry import register_job_handler
from app.subs.content_classification import classify_speaker_content_tag
from app.subs.tag_masking import plain_text

logger = logging.getLogger(__name__)

_SAMPLES_PER_SPEAKER = 8

_WORD_RE = re.compile(r"[A-Za-z]+(?:['’][A-Za-z]+)?")
_LOW_INFORMATION_PHRASES = {
    "ah", "eh", "fine", "good", "great", "ha", "hey", "hm", "hmm",
    "huh", "no", "nope", "oh", "ok", "okay", "please", "really",
    "right", "sorry", "sure", "thanks", "thank you", "uh", "um", "wait",
    "what", "who", "why", "wow", "yeah", "yep", "yes",
}
_REFERENCE_WORDS = {
    "he", "her", "hers", "him", "his", "i", "i'm", "i’m", "me", "mine",
    "my", "our", "ours", "she", "their", "theirs", "them", "they", "us",
    "we", "you", "your", "yours",
}
_RELATIONSHIP_WORDS = {
    "aunt", "brother", "child", "children", "cousin", "dad", "daughter",
    "father", "friend", "husband", "master", "mom", "mother", "parent",
    "parents", "sister", "son", "teacher", "uncle", "wife",
}


def _information_score(line: str) -> int:
    """Small deterministic signal for identity/context-bearing dialogue."""
    words = _WORD_RE.findall(line)
    if not words:
        return -10

    lowered = [word.casefold() for word in words]
    phrase = " ".join(lowered)
    if len(words) <= 2 and phrase in _LOW_INFORMATION_PHRASES:
        return -8

    score = min(len(words), 5)
    if len(words) >= 3:
        score += 2
    if any(word in _REFERENCE_WORDS for word in lowered):
        score += 2
    if any(word in _RELATIONSHIP_WORDS for word in lowered):
        score += 3
    if re.search(r"[A-Za-z]+['’]s\b", line):
        score += 2
    # A capitalized token beyond a sentence-initial article/pronoun is often
    # a name. This also recognizes compact lines such as "Leon sent me."
    if any(
        word[:1].isupper() and word.casefold() not in _REFERENCE_WORDS
        for word in words[1:]
    ):
        score += 2
    return score


def _sample_spread(lines: list[str], count: int) -> list[str]:
    """Even chronological spread that includes both endpoints."""
    if count <= 0:
        return []
    if len(lines) <= count:
        return list(lines)
    if count == 1:
        return [lines[0]]
    return [
        lines[round(i * (len(lines) - 1) / (count - 1))]
        for i in range(count)
    ]


def _select_samples(lines: list[str], count: int = _SAMPLES_PER_SPEAKER) -> list[str]:
    """Prefer useful dialogue, fall back to reactions, and keep chronology."""
    if len(lines) <= count:
        return list(lines)

    informative = [
        (index, line) for index, line in enumerate(lines)
        if _information_score(line) >= 5
    ]
    if len(informative) >= count:
        spread = _sample_spread(informative, count)
        return [line for _index, line in spread]

    selected = list(informative)
    selected_indexes = {index for index, _line in selected}
    fallback = [
        (index, line) for index, line in enumerate(lines)
        if index not in selected_indexes
    ]
    selected.extend(_sample_spread(fallback, count - len(selected)))
    selected.sort(key=lambda item: item[0])
    return [line for _index, line in selected]


@register_job_handler("aggregate_speakers")
def aggregate_speakers(
    payload: dict[str, Any],
    ctx: JobContext,
    progress: ProgressFn,
) -> JobResult:
    """Collect distinct speaker names across the project, with per-speaker
    line counts and sample dialogue lines for the mapping-inference job.
    Ends with speaker_mapping_status='aggregated' (or 'complete' when there
    is nothing to map) — infer_character_mapping takes it from there."""
    project_id: int = payload["project_id"]
    now = datetime.utcnow().isoformat()

    progress(0.1, "Scanning subtitle events for speaker names")

    with SyncSessionLocal() as session:
        rows = session.execute(
            select(SubtitleEvent.name, SubtitleEvent.source_text)
            .join(File, SubtitleEvent.file_id == File.id)
            .where(
                File.project_id == project_id,
                SubtitleEvent.event_type == "dialogue",
                SubtitleEvent.name.isnot(None),
                SubtitleEvent.name != "",
            )
            .order_by(SubtitleEvent.file_id, SubtitleEvent.line_index)
        ).all()

        total_dialogue_count: int = session.scalar(
            select(func.count(SubtitleEvent.id))
            .join(File, SubtitleEvent.file_id == File.id)
            .where(
                File.project_id == project_id,
                SubtitleEvent.event_type == "dialogue",
            )
        ) or 0

    lines_by_speaker: dict[str, list[str]] = {}
    for name, source_text in rows:
        text = plain_text(source_text)
        lines_by_speaker.setdefault(name, [])
        if text:
            lines_by_speaker[name].append(text)

    named_dialogue_count = len(rows)
    coverage = (named_dialogue_count / total_dialogue_count) if total_dialogue_count > 0 else 0.0

    progress(0.5, f"Upserting {len(lines_by_speaker)} speaker(s)")

    created = 0
    with SyncSessionLocal() as session:
        for name, lines in lines_by_speaker.items():
            samples = _select_samples(lines)
            content_tag = classify_speaker_content_tag(name)
            insert_values: dict[str, Any] = {
                "project_id": project_id,
                "name": name,
                "line_count": len(lines),
                "sample_lines_json": json.dumps(samples, ensure_ascii=False),
                "created_at": now,
                "updated_at": now,
            }
            update_values: dict[str, Any] = {
                "line_count": len(lines),
                "sample_lines_json": json.dumps(samples, ensure_ascii=False),
                "updated_at": now,
            }
            if content_tag is not None:
                deterministic_values = {
                    "character_id": None,
                    "is_extra": 1,
                    "content_tag": content_tag,
                    "match_origin": "fuzzy",
                    "match_confidence": 1.0,
                    "match_rationale": "sign/typesetting source",
                }
                insert_values.update(deterministic_values)
                update_values.update(deterministic_values)
            stmt = (
                sqlite_insert(ProjectSpeaker)
                .values(**insert_values)
                .on_conflict_do_update(
                    index_elements=["project_id", "name"],
                    set_=update_values,
                )
            )
            result = session.execute(stmt)
            if result.rowcount > 0:
                created += 1

        project = session.get(Project, project_id)
        if project is not None:
            project.speaker_coverage = coverage
            if lines_by_speaker:
                project.speaker_mapping_status = "aggregated"
            else:
                # Nothing to map — inference would be a no-op.
                project.speaker_mapping_status = "complete"
            project.updated_at = now

        session.commit()

    progress(1.0, "Done")
    return JobResult(
        status="succeeded",
        result={
            "speakers_created": created,
            "speakers_total": len(lines_by_speaker),
            "speaker_coverage": coverage,
        },
        error_code=None,
        error_message=None,
    )
