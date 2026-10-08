"""Configurable source language: option, prompts, embedded track choice, download names."""
from __future__ import annotations

import pytest

from app.api.routes.projects import subtitle_download_name
from app.core.languages import track_matches_language
from app.db import default_prompts
from app.db.options import AppOptions, validate_option_changes
from app.jobs.handlers.inspect_mkv import _pick_subtitle_track
from tests.test_prompt_options import PROMPT_OPTIONS


def _track(track_id: int, *, codec_id: str = "S_TEXT/ASS", **props):
    return {"id": track_id, "type": "subtitles", "properties": {"codec_id": codec_id, **props}}


# -- option ---------------------------------------------------------------------

def test_source_language_defaults_to_english():
    opts = AppOptions.from_dict({})
    assert (opts.source_lang_code, opts.source_lang_name) == ("en", "English")
    assert (AppOptions().source_lang_code, AppOptions().source_lang_name) == ("en", "English")


def test_source_language_loaded_from_stored_options():
    opts = AppOptions.from_dict({"SOURCE_LANG_CODE": "ja", "SOURCE_LANG_NAME": "Japanese"})
    assert (opts.source_lang_code, opts.source_lang_name) == ("ja", "Japanese")


def test_source_language_name_derived_from_code_when_missing():
    assert AppOptions.from_dict({"SOURCE_LANG_CODE": "DE"}).source_lang_name == "German"


def test_unknown_source_language_falls_back_to_english():
    opts = AppOptions.from_dict({"SOURCE_LANG_CODE": "xx", "SOURCE_LANG_NAME": "Klingon"})
    assert (opts.source_lang_code, opts.source_lang_name) == ("en", "English")


def test_patch_rejects_unknown_source_language_code():
    assert validate_option_changes({"SOURCE_LANG_CODE": "xx"}, {})
    assert not validate_option_changes({"SOURCE_LANG_CODE": "ja"}, {})
    assert not validate_option_changes({"SOURCE_LANG_CODE": None}, {})


# -- prompts --------------------------------------------------------------------

@pytest.mark.parametrize("option_key", sorted(PROMPT_OPTIONS))
def test_default_prompts_do_not_assume_english(option_key):
    constant = PROMPT_OPTIONS[option_key][1]
    text = getattr(default_prompts, constant)
    # The only English left is the explicit modal example in the final-QA prompt.
    stripped = text.replace('for example English "may ... but"', "")
    assert "English" not in stripped
    assert "EN:" not in text


@pytest.mark.parametrize("option_key", sorted(PROMPT_OPTIONS))
def test_every_resolver_interpolates_source_language(option_key):
    field, _, resolver = PROMPT_OPTIONS[option_key]
    opts = AppOptions.from_dict({
        "SOURCE_LANG_CODE": "ja", "SOURCE_LANG_NAME": "Japanese", "TARGET_LANG_NAME": "Czech",
        field.upper(): "{SOURCE_LANG_NAME} -> {TARGET_LANG_NAME}",
    })
    assert getattr(opts, resolver)() == "Japanese -> Czech"


@pytest.mark.parametrize("option_key", sorted(PROMPT_OPTIONS))
def test_default_prompts_render_without_placeholders(option_key):
    _, _, resolver = PROMPT_OPTIONS[option_key]
    rendered = getattr(AppOptions.from_dict(
        {"SOURCE_LANG_CODE": "de", "TARGET_LANG_NAME": "Czech"}), resolver)()
    assert "{SOURCE_LANG_NAME}" not in rendered and "{TARGET_LANG_NAME}" not in rendered


def test_custom_prompt_without_the_variable_is_untouched():
    opts = AppOptions.from_dict({"TRANSLATION_PROMPT": "Old prompt, English to {TARGET_LANG_NAME}",
                                 "TARGET_LANG_NAME": "Czech", "SOURCE_LANG_CODE": "ja"})
    assert opts.resolved_translation_prompt() == "Old prompt, English to Czech"


# -- embedded track selection -----------------------------------------------------

def test_english_default_prefers_english_track():
    tracks = [_track(1, language="jpn"), _track(2, language="eng")]
    assert _pick_subtitle_track(tracks)["id"] == 2
    assert _pick_subtitle_track(tracks, "en")["id"] == 2


def test_source_language_changes_embedded_choice():
    tracks = [_track(1, language="jpn"), _track(2, language="eng")]
    assert _pick_subtitle_track(tracks, "ja")["id"] == 1


def test_language_still_outranks_format_for_configured_language():
    tracks = [_track(1, language="eng"), _track(2, language="ger", codec_id="S_TEXT/UTF8")]
    assert _pick_subtitle_track(tracks, "de")["id"] == 2


def test_remaining_ranking_criteria_preserved_for_other_source_language():
    tracks = [
        _track(2, language="jpn", track_name="Japanese [Signs-Songs]"),
        _track(3, language="jpn", track_name="Japanese SDH", flag_hearing_impaired=True),
        _track(4, language="jpn", track_name="Japanese", forced_track=True),
        _track(5, language="jpn", codec_id="S_TEXT/UTF8", track_name="Japanese [Full]"),
        _track(6, language="jpn", track_name="Japanese [Full]"),
        _track(7, language="jpn", track_name="Japanese [Full]"),
    ]
    assert _pick_subtitle_track(tracks, "ja")["id"] == 6  # ASS, full, lowest id
    assert _pick_subtitle_track(list(reversed(tracks)), "ja")["id"] == 6


def test_track_language_matching_handles_639_2_variants_and_ietf():
    assert track_matches_language({"language": "ger"}, "de")
    assert track_matches_language({"language": "deu"}, "de")
    assert track_matches_language({"language": "und", "language_ietf": "pt-BR"}, "pt")
    assert not track_matches_language({"language": "eng"}, "de")
    assert not track_matches_language({}, "en")


# -- download filenames -----------------------------------------------------------

@pytest.mark.parametrize("code", ["en", "ja", "de"])
def test_original_download_uses_source_code(code):
    assert subtitle_download_name("Episode.mkv", "original", code) == f"Episode.{code}.ass"


def test_original_download_defaults_to_en_and_translated_is_unchanged():
    assert subtitle_download_name("Episode.mkv", "original") == "Episode.en.ass"
    for code in ("en", "ja"):
        assert subtitle_download_name("Episode.mkv", "translated", code) == "Episode.ass"
