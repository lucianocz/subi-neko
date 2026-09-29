from __future__ import annotations

from dataclasses import dataclass

from app.jobs.handlers.validate_chunk import (
    _check_escape_mismatch,
    _check_empty_hard_break,
    _check_formatting_tag_mismatch,
    _check_formatting_tag_mismatch_relaxed,
    _check_text_corruption,
    _checks_for_content_type,
    _check_missing_translation,
    _check_locked_line_modified,
    _is_blocking,
    _severity_for,
)


@dataclass
class EventProxy:
    translated_text: str
    source_text: str = ""


def test_text_corruption_ignores_valid_leading_ass_tags():
    issues = _check_text_corruption(
        EventProxy(r"{\pos(576,223.2)}{\an7}EPISODA 6")  # type: ignore[arg-type]
    )

    assert issues == []


def test_text_corruption_still_catches_json_like_output():
    issues = _check_text_corruption(EventProxy('{"translation": "EPISODA 6"}'))  # type: ignore[arg-type]

    assert issues
    assert issues[0][0] == "text_corruption"
    assert "json_like_output" in issues[0][2]["reasons"]


def test_text_corruption_allows_ass_newline_and_numbered_episode_title():
    issues = _check_text_corruption(
        EventProxy(r"{\an8}11. epizoda\NNouzový quest: Zachraňte Enyu!")  # type: ignore[arg-type]
    )

    assert issues == []


def test_text_corruption_allows_numbered_sentence_without_ass_tag():
    issues = _check_text_corruption(
        EventProxy('11. epizoda: "Nouzový quest: Zachraňte Enyu!"')  # type: ignore[arg-type]
    )

    assert issues == []


def test_escape_mismatch_accepts_preserved_ass_newline():
    issues = _check_escape_mismatch(
        EventProxy(
            translated_text=r"{\an8}11. epizoda\NNouzový quest: Zachraňte Enyu!",
            source_text=r"{\an8}Episode 11\NEmergency Quest: Save Enya!",
        )  # type: ignore[arg-type]
    )

    assert issues == []


def test_empty_hard_break_flags_new_trailing_visual_row():
    issues = _check_empty_hard_break(
        EventProxy(
            translated_text=r"Ve hře ale byla záporačka. \N",
            source_text=r"But in the game, she was a villain.\NAnother row",
        )  # type: ignore[arg-type]
    )

    assert issues
    assert issues[0][0] == "empty_hard_break"
    assert issues[0][2]["locations"] == ["trailing"]


def test_empty_hard_break_ignores_tags_and_respects_source_placement():
    assert _check_empty_hard_break(
        EventProxy(
            translated_text=r"{\an8}\N{\i1}Text",
            source_text=r"{\an8}\N{\i1}Text",
        )  # type: ignore[arg-type]
    ) == []


def test_empty_hard_break_is_non_blocking_warning():
    assert _is_blocking("empty_hard_break") is False
    assert _severity_for("empty_hard_break") == "warning"


def test_escape_mismatch_allows_hard_space_count_change():
    # \h runs are alignment padding whose width legitimately changes with
    # the translated words — count differences must not fail validation.
    issues = _check_escape_mismatch(
        EventProxy(
            translated_text=r"Yurine\h\h\h\hZemře na dropkick.\N\h\h\hostatní",
            source_text=r"Yurine\h\h\h\h\h\h\hDies by dropkick.\N\h\h\h\h\hothers",
        )  # type: ignore[arg-type]
    )

    assert issues == []


def test_escape_mismatch_still_rejects_dropped_newline():
    issues = _check_escape_mismatch(
        EventProxy(
            translated_text=r"Jedna Dva",
            source_text=r"One\NTwo",
        )  # type: ignore[arg-type]
    )

    assert issues
    assert issues[0][0] == "escape_mismatch"
    assert r"\N" in issues[0][2]


def test_text_corruption_allows_bracketed_ass_screen_text_after_tags():
    issues = _check_text_corruption(
        EventProxy(
            translated_text=(
                r"{\fscx102\fscy118\frz9.139\pos(312.737,190.153)}"
                r"[Evergelion]\NPodrobnosti o produktu"
            ),
        )  # type: ignore[arg-type]
    )

    assert issues == []


def test_text_corruption_allows_bracketed_choice_text_after_tags():
    issues = _check_text_corruption(
        EventProxy(r"{\an1\fs16\shad0\bord0\pos(22,210)}[A/N]")  # type: ignore[arg-type]
    )

    assert issues == []


def test_formatting_check_allows_preserved_double_brace_ass_override():
    source = r"{{\shad0Z\fs15\3c&HC8A07F&\move(350.743,94.914,353.743,88.914,24,3111)}Item/Skin"
    translated = r"{{\shad0Z\fs15\3c&HC8A07F&\move(350.743,94.914,353.743,88.914,24,3111)}Předmět/Vzhled"

    issues = _check_formatting_tag_mismatch(
        EventProxy(translated_text=translated, source_text=source)  # type: ignore[arg-type]
    )

    assert issues == []


def test_text_corruption_allows_double_brace_ass_override():
    issues = _check_text_corruption(
        EventProxy(
            r"{{\shad0Z\fs15\3c&HC8A07F&\move(350.743,94.914,353.743,88.914,24,3111)}Předmět/Vzhled"
        )  # type: ignore[arg-type]
    )

    assert issues == []


def test_formatting_check_allows_moved_inline_tag_block():
    # Tag masking reinserts blocks where the model placed their markers, so
    # an inline italics span legitimately moves with the word it wraps —
    # the dialogue check compares the multiset of exact blocks, not order.
    source = r"I {\i1}really{\i0} mean it"
    translated = r"Myslím to {\i0}vážně{\i1}"

    issues = _check_formatting_tag_mismatch(
        EventProxy(translated_text=translated, source_text=source)  # type: ignore[arg-type]
    )

    assert issues == []


def test_formatting_check_rejects_altered_tag_block():
    source = r"{\an8}Hello"
    translated = r"{\an2}Ahoj"  # block content changed, not just moved

    issues = _check_formatting_tag_mismatch(
        EventProxy(translated_text=translated, source_text=source)  # type: ignore[arg-type]
    )

    assert issues
    assert issues[0][0] == "formatting_tag_mismatch"


def test_formatting_check_still_rejects_missing_ass_override():
    issues = _check_formatting_tag_mismatch(
        EventProxy(
            translated_text="Bez tagu",
            source_text=r"{\an8}With tag",
        )  # type: ignore[arg-type]
    )

    assert issues
    assert issues[0][0] == "formatting_tag_mismatch"


def test_relaxed_tag_check_allows_reordered_and_regrouped_tag_blocks():
    # Same set of tag names (pos, an), but reflowed into a different block
    # grouping/order — the strict check would reject this, the relaxed
    # (multiset) check used for sign/song content should accept it.
    source = r"{\pos(100,200)}{\an7}Shop Sign"
    translated = r"{\an7\pos(100,200)}Obchod"

    issues = _check_formatting_tag_mismatch_relaxed(
        EventProxy(translated_text=translated, source_text=source)  # type: ignore[arg-type]
    )

    assert issues == []


def test_relaxed_tag_check_still_catches_missing_tag():
    source = r"{\pos(100,200)}{\an7}Shop Sign"
    translated = r"{\an7}Obchod"  # \pos dropped entirely

    issues = _check_formatting_tag_mismatch_relaxed(
        EventProxy(translated_text=translated, source_text=source)  # type: ignore[arg-type]
    )

    assert issues
    assert issues[0][0] == "formatting_tag_mismatch"


def test_relaxed_tag_check_still_catches_unclosed_block():
    source = r"{\an7}Shop Sign"
    translated = r"{\an7 Obchod"  # unclosed override block

    issues = _check_formatting_tag_mismatch_relaxed(
        EventProxy(translated_text=translated, source_text=source)  # type: ignore[arg-type]
    )

    assert issues
    assert issues[0][2]["unclosed_block"] is True


def test_checks_for_content_type_dialogue_uses_strict_tag_check():
    checks = _checks_for_content_type("dialogue")
    assert _check_formatting_tag_mismatch in checks
    assert _check_formatting_tag_mismatch_relaxed not in checks
    assert _check_empty_hard_break in checks


def test_checks_for_content_type_sign_and_song_use_relaxed_tag_check():
    for content_type in ("sign", "song"):
        checks = _checks_for_content_type(content_type)
        assert _check_formatting_tag_mismatch_relaxed in checks
        assert _check_formatting_tag_mismatch not in checks
        assert _check_empty_hard_break not in checks


def test_checks_for_content_type_karaoke_skips_tag_check_entirely():
    checks = _checks_for_content_type("karaoke")
    assert _check_formatting_tag_mismatch not in checks
    assert _check_formatting_tag_mismatch_relaxed not in checks
    assert _check_empty_hard_break not in checks
    # Other structural checks (missing translation, locked line) still apply.
    assert _check_missing_translation in checks
    assert _check_locked_line_modified in checks


def test_missing_translation_ignores_empty_source_line():
    # An empty (or markup-only) source line has nothing to translate — an
    # empty translation is the correct outcome, not a missing one.
    issues = _check_missing_translation(
        EventProxy(translated_text="", source_text="")  # type: ignore[arg-type]
    )

    assert issues == []


def test_missing_translation_ignores_markup_only_source_line():
    issues = _check_missing_translation(
        EventProxy(
            translated_text=r"{\fad(0,500)}",
            source_text=r"{\fad(0,500)}",
        )  # type: ignore[arg-type]
    )

    assert issues == []


def test_escape_mismatch_is_non_blocking():
    # A dropped/added line-break can be a legitimate reflow (e.g. long sign
    # text rewritten as prose) — it must surface for manual review but not
    # reject the event or trigger repair_chunk, unlike a real syntax defect.
    assert _is_blocking("escape_mismatch") is False


def test_escape_mismatch_is_info_severity():
    assert _severity_for("escape_mismatch") == "info"


def test_blocking_types_get_blocker_severity():
    assert _severity_for("formatting_tag_mismatch") == "blocker"


def test_escape_mismatch_message_names_the_row_count_change():
    # The message must state the real reason (line breaks reflowed, and to
    # how many rows) instead of a generic "escape sequences not preserved".
    issues = _check_escape_mismatch(
        EventProxy(
            translated_text="Jedna" + chr(92) + "NDva",
            source_text="One" + chr(92) + "NTwo" + chr(92) + "NThree",
        )  # type: ignore[arg-type]
    )

    assert len(issues) == 1
    _, message, details = issues[0]
    assert "2 rows instead of 3" in message
    assert "escape sequences" not in message
    assert details[chr(92) + "N"] == {"source": 2, "translated": 1}


def test_other_check_types_remain_blocking():
    for qa_type in (
        "missing_translation",
        "formatting_tag_mismatch",
        "locked_line_modified",
        "text_corruption",
    ):
        assert _is_blocking(qa_type) is True


def test_missing_translation_still_flags_empty_translation_of_real_text():
    issues = _check_missing_translation(
        EventProxy(translated_text="", source_text="Hello there")  # type: ignore[arg-type]
    )

    assert issues
    assert issues[0][0] == "missing_translation"
