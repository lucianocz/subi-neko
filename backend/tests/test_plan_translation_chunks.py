from __future__ import annotations

from datetime import datetime
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.db.models import File, Project, SubtitleChunk, SubtitleEvent
from app.db.options import AppOptions
from app.jobs.context import JobContext
from app.jobs.handlers import plan_translation_chunks as planner


def _progress(_value: float, _message: str) -> None:
    pass


def test_effective_chunk_size_eliminates_small_tail_chunk():
    assert planner._effective_chunk_size(total_lines=167, configured_chunk_size=80) == 84


def test_effective_chunk_size_keeps_tail_at_ten_percent():
    assert planner._effective_chunk_size(total_lines=168, configured_chunk_size=80) == 80


def test_plan_translation_chunks_redistributes_small_tail(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(planner, "SyncSessionLocal", factory)

    now = datetime.utcnow().isoformat()
    with factory() as session:
        project = Project(
            name="Test",
            source_directory="test",
            anime_provider="test",
            anime_external_id="test-1",
            speaker_mapping_status="mapping_complete",
            status="processing",
            created_at=now,
            updated_at=now,
        )
        session.add(project)
        session.flush()

        file = File(
            project_id=project.id,
            filename="test.mkv",
            relative_path="test.mkv",
            status="ready",
            created_at=now,
            updated_at=now,
        )
        session.add(file)
        session.flush()

        session.add_all([
            SubtitleEvent(
                file_id=file.id,
                line_index=i,
                event_type="dialogue",
                layer=0,
                start_ms=i * 1000,
                end_ms=i * 1000 + 500,
                original_start_ms=i * 1000, original_end_ms=i * 1000 + 500,
                style="Default",
                source_text=f"Line {i}",
                translation_status="pending",
                created_at=now,
                updated_at=now,
            )
            for i in range(167)
        ])
        session.commit()
        file_id = file.id

    ctx = JobContext(
        import_root=Path("."),
        output_root=Path("."),
        options=AppOptions(chunk_size=80, prepend_context_size=5),
    )

    result = planner.plan_translation_chunks({"file_id": file_id}, ctx, _progress)

    assert result["status"] == "succeeded"
    assert result["result"] == {"chunks_created": 2}

    with factory() as session:
        chunks = list(session.scalars(
            select(SubtitleChunk).order_by(SubtitleChunk.chunk_index)
        ))

    assert [(c.translate_from_line, c.translate_to_line) for c in chunks] == [
        (0, 83),
        (84, 166),
    ]


# ---------------------------------------------------------------------------
# Speaker content tags
# ---------------------------------------------------------------------------

def _setup_project_file(factory, now):
    from app.db.models import Project
    with factory() as session:
        project = Project(
            name="Test",
            source_directory="test",
            anime_provider="test",
            anime_external_id="test-1",
            speaker_mapping_status="mapping_complete",
            status="processing",
            created_at=now,
            updated_at=now,
        )
        session.add(project)
        session.flush()
        file = File(
            project_id=project.id,
            filename="test.mkv",
            relative_path="test.mkv",
            status="ready",
            created_at=now,
            updated_at=now,
        )
        session.add(file)
        session.commit()
        return project.id, file.id


def _add_event(session, file_id, line_index, name, now, *,
               content_type="dialogue", content_type_reason=None,
               translated_text=None, is_user_edited=0):
    session.add(SubtitleEvent(
        file_id=file_id,
        line_index=line_index,
        event_type="dialogue",
        content_type=content_type,
        content_type_reason=content_type_reason,
        layer=0,
        start_ms=line_index * 1000,
        end_ms=line_index * 1000 + 500,
        original_start_ms=line_index * 1000, original_end_ms=line_index * 1000 + 500,
        style="Default",
        name=name,
        source_text=f"Line {line_index}",
        translated_text=translated_text,
        original_ai_translated_text=translated_text,
        translation_status="translated" if translated_text else "pending",
        is_user_edited=is_user_edited,
        created_at=now,
        updated_at=now,
    ))


def _ctx():
    return JobContext(
        import_root=Path("."),
        output_root=Path("."),
        options=AppOptions(chunk_size=80, prepend_context_size=5),
    )


def _make_factory(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(planner, "SyncSessionLocal", factory)
    return factory


def test_tagged_speaker_events_get_own_partition(monkeypatch):
    from app.db.models import ProjectSpeaker

    factory = _make_factory(monkeypatch)
    now = datetime.utcnow().isoformat()
    project_id, file_id = _setup_project_file(factory, now)

    with factory() as session:
        for i in range(4):
            _add_event(session, file_id, i, "Alice" if i % 2 == 0 else "OP-lyrics", now)
        session.add(ProjectSpeaker(
            project_id=project_id, name="OP-lyrics", content_tag="karaoke",
            created_at=now, updated_at=now,
        ))
        session.commit()

    result = planner.plan_translation_chunks({"file_id": file_id}, _ctx(), _progress)
    assert result["status"] == "succeeded"

    with factory() as session:
        chunks = list(session.scalars(
            select(SubtitleChunk).order_by(SubtitleChunk.chunk_index)))
        assert [c.content_type for c in chunks] == ["dialogue", "karaoke"]

        tagged = list(session.scalars(
            select(SubtitleEvent).where(SubtitleEvent.name == "OP-lyrics")))
        assert all(e.content_type == "karaoke" for e in tagged)
        assert all(e.content_type_reason == planner.SPEAKER_TAG_REASON for e in tagged)

        untouched = list(session.scalars(
            select(SubtitleEvent).where(SubtitleEvent.name == "Alice")))
        assert all(e.content_type == "dialogue" for e in untouched)


def test_untag_reverts_to_heuristic_classification(monkeypatch):
    factory = _make_factory(monkeypatch)
    now = datetime.utcnow().isoformat()
    _project_id, file_id = _setup_project_file(factory, now)

    with factory() as session:
        # Previously tagged (reason=speaker_tag) but the tag row is gone.
        _add_event(session, file_id, 0, "Bob", now,
                   content_type="karaoke",
                   content_type_reason=planner.SPEAKER_TAG_REASON)
        session.commit()

    result = planner.plan_translation_chunks({"file_id": file_id}, _ctx(), _progress)
    assert result["status"] == "succeeded"

    with factory() as session:
        event = session.scalars(select(SubtitleEvent)).one()
        assert event.content_type == "dialogue"
        assert event.content_type_reason is None
        chunks = list(session.scalars(select(SubtitleChunk)))
        assert [c.content_type for c in chunks] == ["dialogue"]


def test_retag_resets_machine_translation_but_not_user_edits(monkeypatch):
    from app.db.models import ProjectSpeaker

    factory = _make_factory(monkeypatch)
    now = datetime.utcnow().isoformat()
    project_id, file_id = _setup_project_file(factory, now)

    with factory() as session:
        _add_event(session, file_id, 0, "SIGN CENTER", now, translated_text="Stroj")
        _add_event(session, file_id, 1, "SIGN CENTER", now, translated_text="Člověk",
                   is_user_edited=1)
        session.add(ProjectSpeaker(
            project_id=project_id, name="SIGN CENTER", content_tag="sign",
            created_at=now, updated_at=now,
        ))
        session.commit()

    result = planner.plan_translation_chunks({"file_id": file_id}, _ctx(), _progress)
    assert result["status"] == "succeeded"

    with factory() as session:
        machine = session.scalars(
            select(SubtitleEvent).where(SubtitleEvent.line_index == 0)).one()
        assert machine.content_type == "sign"
        assert machine.translated_text is None
        assert machine.translation_status == "pending"

        edited = session.scalars(
            select(SubtitleEvent).where(SubtitleEvent.line_index == 1)).one()
        assert edited.content_type == "sign"
        assert edited.translated_text == "Člověk"

        chunks = list(session.scalars(select(SubtitleChunk)))
        assert [chunk.content_type for chunk in chunks] == ["sign"]


def test_speaker_tag_plan_is_idempotent(monkeypatch):
    from app.db.models import ProjectSpeaker

    factory = _make_factory(monkeypatch)
    now = datetime.utcnow().isoformat()
    project_id, file_id = _setup_project_file(factory, now)

    with factory() as session:
        _add_event(session, file_id, 0, "OP", now)
        _add_event(session, file_id, 1, "Alice", now)
        session.add(ProjectSpeaker(
            project_id=project_id, name="OP", content_tag="song",
            created_at=now, updated_at=now,
        ))
        session.commit()

    planner.plan_translation_chunks({"file_id": file_id}, _ctx(), _progress)

    # Simulate the first chunk pass having produced a translation.
    with factory() as session:
        event = session.scalars(
            select(SubtitleEvent).where(SubtitleEvent.line_index == 0)).one()
        event.translated_text = "Píseň"
        event.translation_status = "translated"
        session.commit()

    result = planner.plan_translation_chunks({"file_id": file_id}, _ctx(), _progress)
    assert result["status"] == "succeeded"

    with factory() as session:
        event = session.scalars(
            select(SubtitleEvent).where(SubtitleEvent.line_index == 0)).one()
        # Unchanged tag on a re-plan must not clear the translation.
        assert event.translated_text == "Píseň"
        chunks = list(session.scalars(
            select(SubtitleChunk).order_by(SubtitleChunk.chunk_index)))
        assert [c.content_type for c in chunks] == ["dialogue", "song"]
