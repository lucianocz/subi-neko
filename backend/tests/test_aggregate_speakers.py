from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.db.models import File, Project, ProjectSpeaker, SubtitleEvent
from app.db.options import AppOptions
from app.jobs.context import JobContext
from app.jobs.handlers import aggregate_speakers as agg


def _progress(_value: float, _message: str) -> None:
    pass


def _make_project_with_events(factory, now, dialogue_specs):
    """dialogue_specs: list of (name_or_none,) tuples, one per dialogue line."""
    with factory() as session:
        project = Project(
            name="Test",
            source_directory="test",
            anime_provider="test",
            anime_external_id="test-1",
            speaker_mapping_status="awaiting_discovery",
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
                name=name,
                source_text=f"Line {i}",
                translation_status="pending",
                created_at=now,
                updated_at=now,
            )
            for i, (name,) in enumerate(dialogue_specs)
        ])
        session.commit()
        return project.id


def _run(factory, project_id):
    ctx = JobContext(
        import_root=Path("."),
        output_root=Path("."),
        options=AppOptions(),
    )
    return agg.aggregate_speakers({"project_id": project_id}, ctx, _progress)


def _make_engine_and_factory(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(agg, "SyncSessionLocal", factory)
    return factory


def test_complete_when_no_dialogue_lines(monkeypatch):
    """Nothing to map → inference would be a no-op, skip straight to complete."""
    factory = _make_engine_and_factory(monkeypatch)
    now = datetime.utcnow().isoformat()
    project_id = _make_project_with_events(factory, now, [])

    result = _run(factory, project_id)

    assert result["status"] == "succeeded"
    with factory() as session:
        project = session.get(Project, project_id)
        assert project.speaker_mapping_status == "complete"
        assert project.speaker_coverage == 0.0


def test_complete_when_no_names_populated(monkeypatch):
    factory = _make_engine_and_factory(monkeypatch)
    now = datetime.utcnow().isoformat()
    project_id = _make_project_with_events(factory, now, [(None,)] * 20)

    result = _run(factory, project_id)

    assert result["status"] == "succeeded"
    with factory() as session:
        project = session.get(Project, project_id)
        assert project.speaker_mapping_status == "complete"
        assert project.speaker_coverage == 0.0


def test_speakers_found_sets_aggregated_for_inference(monkeypatch):
    """Any named speakers → status 'aggregated'; infer_character_mapping
    takes it from there. There is no manual gate and no coverage bypass."""
    factory = _make_engine_and_factory(monkeypatch)
    now = datetime.utcnow().isoformat()
    # Even very low coverage goes through inference now.
    specs = [("Aria",)] + [(None,)] * 19
    project_id = _make_project_with_events(factory, now, specs)

    result = _run(factory, project_id)

    assert result["status"] == "succeeded"
    assert result["result"]["speaker_coverage"] == 0.05
    with factory() as session:
        project = session.get(Project, project_id)
        assert project.speaker_mapping_status == "aggregated"
        speakers = list(session.scalars(select(ProjectSpeaker)))
        assert [s.name for s in speakers] == ["Aria"]


def test_speakers_carry_line_count_and_sample_lines(monkeypatch):
    factory = _make_engine_and_factory(monkeypatch)
    now = datetime.utcnow().isoformat()
    specs = [("Aria",)] * 10 + [("Bob",)] * 2 + [(None,)] * 5
    project_id = _make_project_with_events(factory, now, specs)

    _run(factory, project_id)

    with factory() as session:
        speakers = {s.name: s for s in session.scalars(select(ProjectSpeaker))}
        assert speakers["Aria"].line_count == 10
        assert speakers["Bob"].line_count == 2
        aria_samples = json.loads(speakers["Aria"].sample_lines_json)
        assert 0 < len(aria_samples) <= 8
        assert all(isinstance(line, str) for line in aria_samples)


def test_rerun_updates_counts_without_duplicating_speakers(monkeypatch):
    factory = _make_engine_and_factory(monkeypatch)
    now = datetime.utcnow().isoformat()
    project_id = _make_project_with_events(factory, now, [("Aria",)] * 3)

    _run(factory, project_id)
    result = _run(factory, project_id)

    assert result["status"] == "succeeded"
    with factory() as session:
        speakers = list(session.scalars(select(ProjectSpeaker)))
        assert len(speakers) == 1
        assert speakers[0].line_count == 3


def test_sample_selection_prefers_informative_lines_and_stays_chronological():
    lines = [
        "OK.",
        "I'm Marie's brother.",
        "Yes.",
        "Call me Luxion.",
        "Huh?",
        "My daughter is waiting at home.",
        "No.",
        "Leon sent me.",
        "Right.",
        "You promised my sister you would return.",
        "What?",
        "I met Captain Olivia at the eastern gate.",
    ]

    first = agg._select_samples(lines, count=6)
    second = agg._select_samples(lines, count=6)

    assert first == second
    assert first == [
        "I'm Marie's brother.",
        "Call me Luxion.",
        "My daughter is waiting at home.",
        "Leon sent me.",
        "You promised my sister you would return.",
        "I met Captain Olivia at the eastern gate.",
    ]
    assert len(first) <= 6


def test_short_identity_rich_lines_are_informative():
    assert agg._information_score("Call me Luxion.") >= 5
    assert agg._information_score("Leon sent me.") >= 5
    assert agg._information_score("My daughter is...") >= 5


def test_sample_selection_falls_back_when_all_lines_are_low_information():
    lines = ["OK.", "Yes.", "No.", "Huh?", "What?", "Right."]

    assert agg._select_samples(lines, count=4) == [
        "OK.", "No.", "Huh?", "Right.",
    ]


def test_sample_spread_includes_first_and_last_occurrences():
    lines = [f"Line {index}" for index in range(20)]

    samples = agg._sample_spread(lines, count=8)

    assert samples[0] == "Line 0"
    assert samples[-1] == "Line 19"
    assert samples == sorted(samples, key=lines.index)


def test_sign_speaker_is_classified_during_aggregation(monkeypatch):
    factory = _make_engine_and_factory(monkeypatch)
    now = datetime.utcnow().isoformat()
    project_id = _make_project_with_events(factory, now, [(" SIGN   CENTER ",)])

    _run(factory, project_id)

    with factory() as session:
        speaker = session.scalar(select(ProjectSpeaker))
        assert speaker.is_extra == 1
        assert speaker.character_id is None
        assert speaker.content_tag == "sign"
