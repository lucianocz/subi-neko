"""Subtitle editor API: paginated events, global summary, authoritative CPS."""
from __future__ import annotations

from datetime import datetime

import pytest

from app.api.routes.projects import (
    SubtitleEventUpdateIn,
    list_file_subtitle_events as _list_events,
    revert_file_subtitle_event,
    update_file_subtitle_event,
)
from app.db import options as options_store
from app.db.models import ProjectWatchedWord, QaItem, SubtitleEvent
from app.subs.watched_words import matching_watched_words
from tests.test_orchestrator import _create_file, _create_project, db_session  # noqa: F401



async def list_file_subtitle_events(
    project_id, file_id, page=1, page_size=1000,
    show_info=True, show_resolved=False, issues_only=False,
):
    """Endpoint called directly: FastAPI Query() defaults only resolve over
    HTTP, so spell the defaults out."""
    return await _list_events(
        project_id, file_id, page=page, page_size=page_size,
        show_info=show_info, show_resolved=show_resolved, issues_only=issues_only)


NOW = datetime.utcnow().isoformat()
CONTENT_CYCLE = ["dialogue", "sign", "song", "karaoke", "other"]


async def _add_event(
    session, file_id, line_index, *, source="Hello", translated="Ahoj", ai="Ahoj",
    start_ms=0, end_ms=1000, content_type="dialogue", event_type="dialogue",
) -> SubtitleEvent:
    event = SubtitleEvent(
        file_id=file_id, line_index=line_index, event_type=event_type,
        content_type=content_type, layer=0, start_ms=start_ms, end_ms=end_ms,
        style="Default", source_text=source, translated_text=translated,
        original_ai_translated_text=ai, translation_status="translated",
        created_at=NOW, updated_at=NOW,
    )
    session.add(event)
    await session.commit()
    return event


async def _add_qa(session, file_id, event_id, *, severity="warning", qa_type="x", resolved=0):
    item = QaItem(
        file_id=file_id, subtitle_event_id=event_id, severity=severity, qa_type=qa_type,
        message="m", is_resolved=resolved, created_at=NOW,
    )
    session.add(item)
    await session.commit()
    return item


async def _add_watched(session, project_id, word, word_type):
    session.add(ProjectWatchedWord(
        project_id=project_id, word=word, word_type=word_type,
        created_at=NOW, updated_at=NOW,
    ))
    await session.commit()


async def _setup(session, n=0):
    project = await _create_project(session, status="processing")
    file = await _create_file(session, project.id, status="review_required")
    events = [
        await _add_event(session, file.id, i, content_type=CONTENT_CYCLE[i % 5])
        for i in range(n)
    ]
    return project, file, events


# ---------------------------------------------------------------------------
# CPS in the DTO and edit endpoints
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_page_items_carry_authoritative_raw_cps(db_session):
    project, file, _ = await _setup(db_session)
    await _add_event(db_session, file.id, 0, translated="a" * 13, start_ms=0, end_ms=600)
    await _add_event(db_session, file.id, 1, translated="Ahoj", start_ms=500, end_ms=500)
    await _add_event(db_session, file.id, 2, translated=None)
    await _add_event(db_session, file.id, 3, translated="{\\an8}")
    await _add_event(db_session, file.id, 4, translated="Ahoj\\Nsvět", start_ms=0, end_ms=1000)

    page = await list_file_subtitle_events(project.id, file.id)
    cps = [item.cps for item in page.items]
    assert cps[0] == 13 / 0.6  # raw, not rounded
    assert cps[1] is None  # zero duration
    assert cps[2] is None  # no translation
    assert cps[3] is None  # tags only
    assert cps[4] == 9.0  # "Ahoj svět"


@pytest.mark.asyncio
async def test_page_summary_exposes_effective_cps_limit(db_session):
    project, file, _ = await _setup(db_session, 1)
    await options_store.aset("CPS_LIMIT", "27")
    page = await list_file_subtitle_events(project.id, file.id)
    assert page.summary.cps_limit == 27.0


@pytest.mark.asyncio
async def test_save_returns_recalculated_cps_and_revert_restores(db_session):
    project, file, _ = await _setup(db_session)
    event = await _add_event(db_session, file.id, 0, translated="a" * 10, ai="a" * 10)

    saved = await update_file_subtitle_event(
        project.id, file.id, event.id, SubtitleEventUpdateIn(translated_text="b" * 25))
    assert saved.cps == 25.0
    assert saved.is_user_edited is True

    cleared = await update_file_subtitle_event(
        project.id, file.id, event.id, SubtitleEventUpdateIn(translated_text=None))
    assert cleared.cps is None

    reverted = await revert_file_subtitle_event(project.id, file.id, event.id)
    assert reverted.cps == 10.0


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pagination_boundaries_and_totals(db_session):
    project, file, _ = await _setup(db_session, 25)

    first = await list_file_subtitle_events(project.id, file.id, page=1, page_size=10)
    assert [i.line_index for i in first.items] == list(range(10))
    assert (first.page, first.page_size, first.total_pages) == (1, 10, 3)
    assert first.total_events == first.filtered_events == 25

    last = await list_file_subtitle_events(project.id, file.id, page=3, page_size=10)
    assert [i.line_index for i in last.items] == list(range(20, 25))

    beyond = await list_file_subtitle_events(project.id, file.id, page=99, page_size=10)
    assert beyond.page == 3
    assert [i.line_index for i in beyond.items] == list(range(20, 25))


@pytest.mark.asyncio
async def test_default_page_size_is_1000_and_empty_file_has_one_page(db_session):
    project, file, _ = await _setup(db_session)
    page = await list_file_subtitle_events(project.id, file.id)
    assert page.page_size == 1000
    assert (page.items, page.total_events, page.total_pages, page.page) == ([], 0, 1, 1)


@pytest.mark.asyncio
async def test_pagination_preserves_all_event_types(db_session):
    project, file, _ = await _setup(db_session, 20)
    page1 = await list_file_subtitle_events(project.id, file.id, page=1, page_size=10)
    page2 = await list_file_subtitle_events(project.id, file.id, page=2, page_size=10)
    ids = [i.id for i in page1.items + page2.items]
    assert len(ids) == len(set(ids)) == 20
    rows = (await db_session.execute(
        SubtitleEvent.__table__.select().where(SubtitleEvent.file_id == file.id)
    )).all()
    assert {r.content_type for r in rows} == set(CONTENT_CYCLE)
    assert set(ids) == {r.id for r in rows}


# ---------------------------------------------------------------------------
# Filters before pagination + global summary
# ---------------------------------------------------------------------------

async def _issue_fixture(session, n=30):
    """30 events; issues on events 3 (blocker), 12 (warning), 25 (resolved
    warning), 28 (info)."""
    project, file, events = await _setup(session, n)
    await _add_qa(session, file.id, events[3].id, severity="blocker", qa_type="a")
    await _add_qa(session, file.id, events[12].id, severity="warning", qa_type="high_cps")
    await _add_qa(session, file.id, events[25].id, severity="warning", qa_type="b", resolved=1)
    await _add_qa(session, file.id, events[28].id, severity="info", qa_type="c")
    return project, file, events


@pytest.mark.asyncio
async def test_issues_only_filters_whole_file_before_paginating(db_session):
    project, file, events = await _issue_fixture(db_session)

    page = await list_file_subtitle_events(
        project.id, file.id, page=1, page_size=2, issues_only=True)
    # unresolved blocker + warning + info visible; resolved hidden by default
    assert page.filtered_events == 3
    assert page.total_pages == 2
    assert page.total_events == 30
    assert [i.line_index for i in page.items] == [3, 12]

    page2 = await list_file_subtitle_events(
        project.id, file.id, page=2, page_size=2, issues_only=True)
    assert [i.line_index for i in page2.items] == [28]


@pytest.mark.asyncio
async def test_issue_filter_flags_match_editor_semantics(db_session):
    project, file, events = await _issue_fixture(db_session)

    async def lines(**kw):
        page = await list_file_subtitle_events(project.id, file.id, issues_only=True, **kw)
        return [i.line_index for i in page.items]

    assert await lines(show_info=False) == [3]  # blockers only
    assert await lines(show_info=True, show_resolved=True) == [3, 12, 25, 28]
    assert await lines(show_info=False, show_resolved=True) == [3]


@pytest.mark.asyncio
async def test_summary_is_global_not_page_local(db_session):
    project, file, events = await _issue_fixture(db_session)

    page = await list_file_subtitle_events(project.id, file.id, page=1, page_size=5)
    assert len(page.items) == 5  # only event 3's issue is on this page
    summary = page.summary
    assert summary.total_events == 30
    assert summary.unresolved_issue_count == 3
    counts = {(c.severity, c.qa_type): c.count for c in summary.issue_counts}
    assert counts == {("blocker", "a"): 1, ("warning", "high_cps"): 1, ("info", "c"): 1}

    # Same summary from another page / with issues-only paginating differently.
    other = await list_file_subtitle_events(
        project.id, file.id, page=6, page_size=5, issues_only=True)
    assert other.summary.model_dump() == summary.model_dump()

    with_resolved = await list_file_subtitle_events(
        project.id, file.id, page=1, page_size=5, show_resolved=True)
    assert with_resolved.summary.unresolved_issue_count == 3
    assert len(with_resolved.summary.issue_counts) == 4

    blockers = await list_file_subtitle_events(
        project.id, file.id, page=1, page_size=5, show_info=False)
    assert [(c.severity, c.qa_type) for c in blockers.summary.issue_counts] == [("blocker", "a")]


@pytest.mark.asyncio
async def test_summary_ignores_issues_without_an_event(db_session):
    project, file, events = await _setup(db_session, 2)
    await _add_qa(db_session, file.id, None, severity="blocker", qa_type="orphan")
    page = await list_file_subtitle_events(project.id, file.id)
    assert page.summary.unresolved_issue_count == 0
    assert page.summary.issue_counts == []


# ---------------------------------------------------------------------------
# Watched words
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_watched_occurrences_are_global_and_match_row_semantics(db_session):
    project, file, _ = await _setup(db_session)
    await _add_watched(db_session, project.id, "Sakra", "original")
    await _add_watched(db_session, project.id, "idiot", "translated")
    await _add_watched(db_session, project.id, "hele", "translated")
    rows = [
        # (source, translated)
        ("Damn it, SAKRA!", "Sakra, ty idiote"),   # sakra src (1) + idiot (1)
        ("sakra sakra sakra", "idiot idiot"),      # repeats count once per definition: 1 + 1
        ("nothing", "Hele, hele, idiot"),          # hele (1) + idiot (1)
        ("sakra", None),                           # 1
        ("plain", "Čau"),                          # 0
    ]
    for i, (src, tr) in enumerate(rows * 40):  # 200 events: spans several pages
        await _add_event(db_session, file.id, i, source=src, translated=tr,
                         content_type=CONTENT_CYCLE[i % 5])

    orig, trans = ["Sakra"], ["idiot", "hele"]
    expected = sum(
        len(matching_watched_words(s, orig)) + len(matching_watched_words(t, trans))
        for s, t in rows * 40
    )
    assert expected == 40 * 7

    for page_no in (1, 2, 4):
        page = await list_file_subtitle_events(
            project.id, file.id, page=page_no, page_size=50)
        assert page.summary.watched_occurrences == expected


def test_watched_matching_semantics():
    assert matching_watched_words("Ahoj IDIOT", ["idiot"]) == ["idiot"]  # case-insensitive
    assert matching_watched_words("idiotic", ["idiot"]) == ["idiot"]  # substring, no word bounds
    assert matching_watched_words("idiot idiot", ["idiot"]) == ["idiot"]  # once per definition
    assert matching_watched_words("", ["idiot"]) == []
    assert matching_watched_words(None, ["idiot"]) == []
    assert matching_watched_words("Čau ČAU", ["čau"]) == ["čau"]


@pytest.mark.asyncio
async def test_no_watched_words_means_zero(db_session):
    project, file, _ = await _setup(db_session, 3)
    page = await list_file_subtitle_events(project.id, file.id)
    assert page.summary.watched_occurrences == 0
