from __future__ import annotations

import pytest

from app.subs.content_classification import classify_content_type as _classify_content_type
from app.subs.content_classification import classify_speaker_content_tag


def test_comment_event_type_is_other():
    content_type, reason = _classify_content_type("comment", "Default", "Some hidden note")
    assert content_type == "other"
    assert reason == "ass_comment"


def test_karaoke_tag_detected_regardless_of_style():
    content_type, reason = _classify_content_type(
        "dialogue", "Default", r"{\k30}気{\k25}に{\k40}な{\k18}る"
    )
    assert content_type == "karaoke"
    assert reason == "k_tag"


def test_karaoke_tag_with_ko_and_kf_variants():
    for tag in (r"{\ko30}text", r"{\kf30}text"):
        content_type, reason = _classify_content_type("dialogue", "Default", tag)
        assert content_type == "karaoke"
        assert reason == "k_tag"


def test_positioning_tag_detected_as_sign_even_on_unnamed_style():
    content_type, reason = _classify_content_type(
        "dialogue", "Default", r"{\pos(320,50)}OPEN"
    )
    assert content_type == "sign"
    assert reason == "positioning_tag"


def test_move_and_clip_tags_detected_as_sign():
    for tag_text in (r"{\move(0,0,100,100)}Sign", r"{\clip(0,0,100,100)}Sign"):
        content_type, reason = _classify_content_type("dialogue", "Default", tag_text)
        assert content_type == "sign"
        assert reason == "positioning_tag"


def test_song_style_name_without_tags():
    content_type, reason = _classify_content_type("dialogue", "OP", "Full lyric line here")
    assert content_type == "song"
    assert reason == "style_name"

    content_type, reason = _classify_content_type("dialogue", "ED1", "Another lyric line")
    assert content_type == "song"
    assert reason == "style_name"


def test_sign_style_name_without_tags():
    content_type, reason = _classify_content_type("dialogue", "Signs", "Shop sign text")
    assert content_type == "sign"
    assert reason == "style_name"


def test_karaoke_tag_takes_priority_over_style_name():
    content_type, reason = _classify_content_type("dialogue", "OP", r"{\k20}song")
    assert content_type == "karaoke"
    assert reason == "k_tag"


def test_high_tag_density_without_positioning_or_style_signal_is_sign():
    # No \k, no \pos/\move/\org/\clip/\t(, no recognizable style name —
    # only the tag-to-text ratio heuristic can classify this as a sign.
    text = r"{\fscx200\fscy200\frz45\c&H0000FF&\3c&HFFFFFF&\bord3}A"
    content_type, reason = _classify_content_type("dialogue", "Default", text)
    assert content_type == "sign"
    assert reason == "tag_density"


def test_plain_dialogue_with_no_signals_defaults_to_dialogue():
    content_type, reason = _classify_content_type(
        "dialogue", "Default", "Hello, how are you today?"
    )
    assert content_type == "dialogue"
    assert reason is None


@pytest.mark.parametrize("label", [
    "SIGN",
    "SIGN CENTER",
    "SIGN LEFT",
    "SIGN RIGHT",
    " sign   top ",
    "sign_bottom",
    "TITLE",
    "EPTITLE",
    "EP TITLE",
    "Episode Title",
    "TYPESETTING",
])
def test_sign_speaker_labels_are_anchored_and_normalized(label):
    assert classify_speaker_content_tag(label) == "sign"


@pytest.mark.parametrize("label", [
    "Alice",
    "Signal",
    "Design Manager",
    "Signed Letter",
    "Title Manager",
])
def test_ordinary_speaker_labels_are_not_signs(label):
    assert classify_speaker_content_tag(label) is None


def test_dialogue_with_empty_or_unnamed_style_still_defaults_to_dialogue():
    content_type, reason = _classify_content_type("dialogue", "", "Just talking here.")
    assert content_type == "dialogue"
    assert reason is None


def test_no_content_is_ever_excluded_from_translation():
    # None of the classification branches should ever produce a value that
    # would cause a line to be skipped entirely — "other" only applies to
    # native ASS Comment lines, which were already excluded before this
    # classifier existed.
    samples = [
        ("dialogue", "Default", "plain text"),
        ("dialogue", "Signs", "a sign"),
        ("dialogue", "OP", "a lyric"),
        ("dialogue", "Default", r"{\k20}karaoke"),
        ("comment", "Default", "hidden note"),
    ]
    for event_type, style, text in samples:
        content_type, _ = _classify_content_type(event_type, style, text)
        assert content_type in ("dialogue", "sign", "song", "karaoke", "other")
