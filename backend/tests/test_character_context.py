from app.db.models import ProjectCharacter
from app.jobs.handlers.character_context import (
    allocate_character_descriptions,
    build_preparation_character_block,
    truncate_description,
)


def _character(cid: int, description: str | None, role: str = "SUPPORTING") -> ProjectCharacter:
    return ProjectCharacter(id=cid, project_id=1, name=f"Character {cid}",
                            role=role, description=description)


def test_small_roster_keeps_descriptions_and_structured_metadata():
    character = _character(1, "A complete description.", "MAIN")
    character.gender = "female"
    character.aliases = "Hero, H"
    character.character_type = "Human"
    character.voice_actor = "Actor"
    allocated = allocate_character_descriptions([character], 400, 24000)
    block = build_preparation_character_block([character], allocated)
    assert allocated == {1: "A complete description."}
    assert "gender: female" in block
    assert "role: MAIN" in block
    assert "aliases: Hero, H" in block
    assert "character_type: Human" in block
    assert "voice_actor: Actor" in block
    assert "description: A complete description." in block


def test_preparation_block_omits_missing_optional_values_and_keeps_mandatory_fields():
    character = _character(1, None)
    block = build_preparation_character_block([character], {})
    assert block.startswith("[CHARACTER]\n")
    assert block.endswith("\n[/CHARACTER]")
    assert "external_id: internal:1" in block
    assert "canonical_name: Character 1" in block
    for optional in ("aliases", "gender", "character_type", "voice_actor",
                     "social_position", "notes", "description"):
        assert f"{optional}:" not in block


def test_preparation_block_normalizes_whitespace_preserves_unicode_and_is_deterministic():
    character = _character(1, "  Česká\n  princezna  ")
    character.external_id = " c1 "
    character.aliases = "  Árie   Hrdinka "
    allocated = allocate_character_descriptions([character], 100, 100)
    first = build_preparation_character_block([character], allocated)
    second = build_preparation_character_block([character], allocated)
    assert first == second
    assert "external_id: c1" in first
    assert "aliases: Árie Hrdinka" in first
    assert "description: Česká princezna" in first


def test_individual_and_aggregate_limits_are_deterministic_and_fair():
    characters = [_character(i, f"description {i} " + "x" * 100) for i in range(1, 81)]
    first = allocate_character_descriptions(characters, 40, 800)
    second = allocate_character_descriptions(characters, 40, 800)
    assert first == second
    assert len(first) == 80
    assert sum(len(value) for value in first.values()) <= 800
    assert all(len(value) <= 40 for value in first.values())


def test_missing_descriptions_and_zero_budget():
    characters = [_character(1, None), _character(2, ""), _character(3, "present")]
    assert allocate_character_descriptions(characters, 50, 50) == {3: "present"}
    assert allocate_character_descriptions(characters, 0, 50) == {}
    assert allocate_character_descriptions(characters, 50, 0) == {}


def test_importance_order_receives_surplus_without_starving_later_rows():
    background = _character(1, "short", "BACKGROUND")
    supporting = _character(2, "s" * 100, "SUPPORTING")
    main = _character(3, "m" * 100, "MAIN")
    allocated = allocate_character_descriptions([background, supporting, main], 100, 35)
    assert set(allocated) == {1, 2, 3}
    assert len(allocated[3]) > len(allocated[2])  # MAIN gets stable surplus first
    assert len(allocated[2]) > 0


def test_relevance_precedes_role_for_surplus():
    main = _character(1, "m" * 100, "MAIN")
    relevant_support = _character(2, "r" * 100, "SUPPORTING")
    short = _character(3, "short", "BACKGROUND")
    allocated = allocate_character_descriptions([main, relevant_support, short], 100, 35, {2})
    assert len(allocated[2]) > len(allocated[1])


def test_sentence_aware_truncation_and_exceptionally_long_description():
    text = "First useful sentence. Second useful sentence. " + "tail " * 1000
    result = truncate_description(text, 50)
    assert result == "First useful sentence. Second useful sentence.…"
    assert len(result) <= 50


def test_allocator_marks_configured_truncation_instead_of_silently_cutting():
    character = _character(1, "x" * 100)
    allocated = allocate_character_descriptions([character], 30, 30)
    assert allocated[1] == "x" * 29 + "…"
    assert len(allocated[1]) == 30

