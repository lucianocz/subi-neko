from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.db.models import Project, ProjectCharacter, ProjectSpeaker
from app.db.options import AppOptions
from app.jobs.context import JobContext
from app.jobs.handlers import infer_character_mapping as icm
from app.llm.schemas import MappingResponse, SpeakerMatch


def _progress(_value: float, _message: str) -> None:
    pass


def _now() -> str:
    return datetime.utcnow().isoformat()


@pytest.fixture
def factory(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(icm, "SyncSessionLocal", factory)
    yield factory
    engine.dispose()


def _seed(session, speakers: list[dict], characters: list[dict]) -> int:
    project = Project(
        name="Test Anime", source_directory="t", anime_provider="anidb",
        anime_external_id="1", speaker_mapping_status="aggregated",
        status="discovering", created_at=_now(), updated_at=_now(),
    )
    session.add(project)
    session.flush()
    for c in characters:
        session.add(ProjectCharacter(
            project_id=project.id, name=c["name"],
            external_id=c.get("external_id"), gender=c.get("gender"),
            aliases=c.get("aliases"),
            role=c.get("role"), description=c.get("description"),
            voice_actor=c.get("voice_actor"), character_type=c.get("character_type"),
            social_position=c.get("social_position"), note=c.get("note"),
            created_at=_now(), updated_at=_now(),
        ))
    for s in speakers:
        session.add(ProjectSpeaker(
            project_id=project.id, name=s["name"],
            gender=s.get("gender"),
            character_id=s.get("character_id"),
            match_origin=s.get("match_origin"),
            line_count=s.get("line_count", 5),
            sample_lines_json=json.dumps(s.get("samples", ["Hello there."])),
            created_at=_now(), updated_at=_now(),
        ))
    session.commit()
    return project.id


def _run(project_id: int, options: AppOptions | None = None):
    ctx = JobContext(import_root=Path("."), output_root=Path("."), options=options or AppOptions())
    return icm.infer_character_mapping({"project_id": project_id}, ctx, _progress)


# ---------------------------------------------------------------------------
# Normalization / detection units
# ---------------------------------------------------------------------------

def test_normalize_strips_honorifics_diacritics_and_parentheticals():
    assert icm.normalize_name("Aria-san") == "aria"
    assert icm.normalize_name("Tomáš") == "tomas"
    assert icm.normalize_name("Rin (young)") == "rin"
    assert icm.normalize_name("Rin's voice") == "rin"


def test_detect_extra_patterns():
    assert icm.detect_extra("Boy A") == (True, "male")
    assert icm.detect_extra("Girl 2") == (True, "female")
    assert icm.detect_extra("Crowd") == (True, None)
    assert icm.detect_extra("TV") == (True, None)
    assert icm.detect_extra("Narrator") == (True, None)
    assert icm.detect_extra("Aria") == (False, None)


# ---------------------------------------------------------------------------
# Stage 0 — deterministic matching
# ---------------------------------------------------------------------------

def test_fuzzy_exact_match_maps_with_full_confidence(factory, monkeypatch):
    def _no_llm(**kwargs):
        raise AssertionError("LLM must not be called when everything matched deterministically")
    monkeypatch.setattr(icm.llm_client, "complete", _no_llm)

    with factory() as session:
        project_id = _seed(
            session,
            speakers=[{"name": "Aria-san"}],
            characters=[{"name": "Aria Vermillion", "external_id": "c1", "gender": "female"}],
        )

    result = _run(project_id)

    assert result["status"] == "succeeded"
    assert result["result"]["fuzzy_matched"] == 1
    with factory() as session:
        speaker = session.scalar(select(ProjectSpeaker))
        character = session.scalar(select(ProjectCharacter))
        assert speaker.character_id == character.id
        assert speaker.match_origin == "fuzzy"
        assert speaker.match_confidence == 1.0
        assert speaker.gender == "female"  # inherited from the character
        project = session.get(Project, project_id)
        assert project.speaker_mapping_status == "complete"


def test_ambiguous_token_is_not_fuzzy_matched(factory, monkeypatch):
    captured = {}

    def _fake_complete(**kwargs):
        captured["called"] = True
        return MappingResponse(matches=[]), None
    monkeypatch.setattr(icm.llm_client, "complete", _fake_complete)

    with factory() as session:
        # "Sato" is a family-name token of two different characters → ambiguous.
        project_id = _seed(
            session,
            speakers=[{"name": "Sato"}],
            characters=[
                {"name": "Kenji Sato", "external_id": "c1"},
                {"name": "Yumi Sato", "external_id": "c2"},
            ],
        )

    result = _run(project_id)

    assert result["result"]["fuzzy_matched"] == 0
    assert captured.get("called") is True  # went to the LLM instead


def test_extras_marked_and_never_sent_to_llm(factory, monkeypatch):
    def _no_llm(**kwargs):
        raise AssertionError("Extras must not reach the LLM")
    monkeypatch.setattr(icm.llm_client, "complete", _no_llm)

    with factory() as session:
        project_id = _seed(
            session,
            speakers=[{"name": "Boy A"}, {"name": "Crowd"}],
            characters=[{"name": "Aria", "external_id": "c1"}],
        )

    result = _run(project_id)

    assert result["result"]["extras_detected"] == 2
    with factory() as session:
        speakers = {s.name: s for s in session.scalars(select(ProjectSpeaker))}
        assert speakers["Boy A"].is_extra == 1
        assert speakers["Boy A"].gender == "male"
        assert speakers["Boy A"].character_id is None
        assert speakers["Crowd"].is_extra == 1


def test_exact_role_like_character_name_wins_before_generic_extra(factory, monkeypatch):
    monkeypatch.setattr(icm.llm_client, "complete", lambda **kwargs: (_ for _ in ()).throw(
        AssertionError("exact character must not reach LLM")))
    with factory() as session:
        project_id = _seed(session, [{"name": "Doctor"}], [
            {"name": "Doctor", "external_id": "doctor", "gender": "female"},
        ])
    _run(project_id)
    with factory() as session:
        speaker = session.scalar(select(ProjectSpeaker))
        assert speaker.character_id is not None
        assert speaker.is_extra == 0


def test_manual_mapping_never_overwritten(factory, monkeypatch):
    def _no_llm(**kwargs):
        raise AssertionError("Manual rows must not reach the LLM")
    monkeypatch.setattr(icm.llm_client, "complete", _no_llm)

    with factory() as session:
        project_id = _seed(
            session,
            speakers=[{"name": "Aria", "match_origin": "manual", "character_id": None}],
            characters=[{"name": "Aria", "external_id": "c1"}],
        )

    _run(project_id)

    with factory() as session:
        speaker = session.scalar(select(ProjectSpeaker))
        # The exact-name fuzzy match would have mapped it — manual wins.
        assert speaker.character_id is None
        assert speaker.match_origin == "manual"


# ---------------------------------------------------------------------------
# Stage 1 — LLM inference
# ---------------------------------------------------------------------------

def test_llm_matches_written_with_confidence_and_gender(factory, monkeypatch):
    def _fake_complete(**kwargs):
        return MappingResponse(matches=[
            SpeakerMatch(speaker="Ary", character_external_id="c1",
                         confidence=0.72, inferred_gender="female",
                         rationale="nickname of Aria"),
            SpeakerMatch(speaker="Mystery Man", character_external_id=None,
                         confidence=0.3, inferred_gender="male",
                         rationale="no roster match"),
        ]), None
    monkeypatch.setattr(icm.llm_client, "complete", _fake_complete)

    with factory() as session:
        project_id = _seed(
            session,
            speakers=[{"name": "Ary"}, {"name": "Mystery Man"}],
            characters=[{"name": "Aria Vermillion", "external_id": "c1", "gender": "female"}],
        )

    result = _run(project_id)

    assert result["result"]["llm_matched"] == 1
    with factory() as session:
        speakers = {s.name: s for s in session.scalars(select(ProjectSpeaker))}
        assert speakers["Ary"].character_id is not None
        assert speakers["Ary"].match_origin == "llm"
        assert speakers["Ary"].match_confidence == 0.72
        assert speakers["Ary"].gender == "female"
        assert speakers["Mystery Man"].character_id is None
        assert speakers["Mystery Man"].gender == "male"  # gender kept even unmatched
        project = session.get(Project, project_id)
        assert project.speaker_mapping_status == "complete"


def test_canonical_gender_precedes_conflicting_llm_gender(factory, monkeypatch):
    def _fake_complete(**kwargs):
        return MappingResponse(matches=[SpeakerMatch(
            speaker="Captain", character_external_id="c1", confidence=0.9,
            inferred_gender="female", rationale="dialogue guess")]), None
    monkeypatch.setattr(icm.llm_client, "complete", _fake_complete)
    with factory() as session:
        project_id = _seed(session, [{"name": "Captain"}], [
            {"name": "Alex Morgan", "external_id": "c1", "gender": "male"},
        ])
    _run(project_id)
    with factory() as session:
        assert session.scalar(select(ProjectSpeaker)).gender == "male"


def test_low_confidence_character_does_not_override_llm_gender(factory, monkeypatch):
    def _fake_complete(**kwargs):
        return MappingResponse(matches=[SpeakerMatch(
            speaker="Captain", character_external_id="c1", confidence=0.4,
            inferred_gender="female", rationale="uncertain match")]), None
    monkeypatch.setattr(icm.llm_client, "complete", _fake_complete)
    with factory() as session:
        project_id = _seed(session, [{"name": "Captain"}], [
            {"name": "Alex Morgan", "external_id": "c1", "gender": "male"},
        ])
    _run(project_id)
    with factory() as session:
        assert session.scalar(select(ProjectSpeaker)).gender == "female"


def test_existing_speaker_gender_is_not_destructively_overwritten(factory, monkeypatch):
    def _fake_complete(**kwargs):
        return MappingResponse(matches=[SpeakerMatch(
            speaker="Captain", character_external_id="c1", confidence=0.9,
            inferred_gender="male", rationale="guess")]), None
    monkeypatch.setattr(icm.llm_client, "complete", _fake_complete)
    with factory() as session:
        project_id = _seed(session, [{"name": "Captain", "gender": "female"}], [
            {"name": "Alex Morgan", "external_id": "c1", "gender": "male"},
        ])
    _run(project_id)
    with factory() as session:
        assert session.scalar(select(ProjectSpeaker)).gender == "female"


def test_cryptic_label_does_not_propagate_character_or_llm_gender(factory, monkeypatch):
    def _fake_complete(**kwargs):
        return MappingResponse(matches=[SpeakerMatch(
            speaker="20", character_external_id="c1", confidence=0.99,
            inferred_gender="female", rationale="guess")]), None
    monkeypatch.setattr(icm.llm_client, "complete", _fake_complete)
    with factory() as session:
        project_id = _seed(session, [{"name": "20"}], [
            {"name": "Aria", "external_id": "c1", "gender": "female"},
        ])
    _run(project_id)
    with factory() as session:
        speaker = session.scalar(select(ProjectSpeaker))
        assert speaker.match_confidence == 0.5
        assert speaker.gender is None


def test_mapping_payload_has_aliases_metadata_and_configured_description_limits(factory, monkeypatch):
    captured = {}
    def _fake_complete(**kwargs):
        captured.update(kwargs)
        return MappingResponse(matches=[]), None
    monkeypatch.setattr(icm.llm_client, "complete", _fake_complete)
    with factory() as session:
        project_id = _seed(session, [{"name": "Unknown"}], [{
            "name": "Aria Vermillion", "external_id": "c1", "gender": "female",
            "aliases": "Heroine, Aria V", "role": "MAIN", "voice_actor": "Jane",
            "character_type": "Human", "social_position": "Princess", "note": "Formal",
            "description": "One sentence. " + "x" * 100,
        }])
    options = AppOptions(mapping_character_description_max=30,
                         mapping_character_description_budget=30)
    _run(project_id, options)
    user = captured["user"]
    assert "id=c1: Aria Vermillion" in user
    assert "aliases: Heroine, Aria V" in user
    assert "type: Human" in user and "VA: Jane" in user
    assert "social position: Princess" in user and "note: Formal" in user
    assert "x" * 30 not in user


@pytest.mark.parametrize(("count", "expected"), [(1, 1184), (20, 4224), (100, 16000)])
def test_mapping_completion_budget_scales_and_is_bounded(count, expected):
    assert icm.mapping_completion_budget(count, 32768) == expected


def test_mapping_completion_budget_respects_configured_maximum():
    assert icm.mapping_completion_budget(100, 6000) == 6000


def test_llm_failure_fails_job_and_keeps_mapping_aggregated(factory, monkeypatch):
    """An LLM error fails the job so the project blocks at the context gate
    (visible + retryable); Stage 0 results committed beforehand survive."""
    from app.llm.client import LlmError

    def _fail(**kwargs):
        raise LlmError("OPENAI_API_ERROR", "boom")
    monkeypatch.setattr(icm.llm_client, "complete", _fail)

    with factory() as session:
        project_id = _seed(
            session,
            speakers=[{"name": "Ary"}, {"name": "Boy A"}],
            characters=[{"name": "Aria Vermillion", "external_id": "c1"}],
        )

    result = _run(project_id)

    assert result["status"] == "failed"
    assert result["error_code"] == "OPENAI_API_ERROR"
    with factory() as session:
        project = session.get(Project, project_id)
        assert project.speaker_mapping_status == "aggregated"  # gate stays closed
        speakers = {s.name: s for s in session.scalars(select(ProjectSpeaker))}
        assert speakers["Boy A"].is_extra == 1  # deterministic stage persisted
