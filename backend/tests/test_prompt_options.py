"""Every prompt the pipeline sends must be a DB-backed option.

These tests guard the wiring rather than the prompt text: a handler that
reads a DEFAULT_* constant directly still works, so nothing fails loudly —
the user's saved prompt is just silently ignored. That is exactly the kind
of regression worth a test.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

from app.db import default_prompts
from app.db.options import AppOptions

HANDLERS_DIR = pathlib.Path(__file__).resolve().parents[1] / "app" / "jobs" / "handlers"

# option key → (AppOptions field, default constant, resolver method)
PROMPT_OPTIONS = {
    "TRANSLATION_PROMPT": ("translation_prompt", "DEFAULT_TRANSLATION_PROMPT",
                           "resolved_translation_prompt"),
    "REPAIR_PROMPT": ("repair_prompt", "DEFAULT_REPAIR_PROMPT",
                      "resolved_repair_prompt"),
    "POLISH_PROMPT": ("polish_prompt", "DEFAULT_POLISH_PROMPT",
                      "resolved_polish_prompt"),
    "FINAL_QA_PROMPT": ("final_qa_prompt", "DEFAULT_FINAL_QA_PROMPT",
                        "resolved_final_qa_prompt"),
    "SIGN_TRANSLATION_PROMPT": ("sign_translation_prompt", "DEFAULT_SIGN_TRANSLATION_PROMPT",
                                "resolved_sign_translation_prompt"),
    "SONG_TRANSLATION_PROMPT": ("song_translation_prompt", "DEFAULT_SONG_TRANSLATION_PROMPT",
                                "resolved_song_translation_prompt"),
    "ANALYZE_PROMPT": ("analyze_prompt", "DEFAULT_ANALYZE_PROMPT",
                       "resolved_analyze_prompt"),
    "MAPPING_PROMPT": ("mapping_prompt", "DEFAULT_MAPPING_PROMPT",
                       "resolved_mapping_prompt"),
    "STYLE_BIBLE_PROMPT": ("style_bible_prompt", "DEFAULT_STYLE_BIBLE_PROMPT",
                           "resolved_style_bible_prompt"),
    "STYLE_BIBLE_UPDATE_PROMPT": ("style_bible_update_prompt", "DEFAULT_STYLE_BIBLE_UPDATE_PROMPT",
                                  "resolved_style_bible_update_prompt"),
}


@pytest.mark.parametrize("option_key", sorted(PROMPT_OPTIONS))
def test_option_defaults_to_its_constant(option_key):
    field, constant, _ = PROMPT_OPTIONS[option_key]
    assert getattr(AppOptions(), field) == getattr(default_prompts, constant)


@pytest.mark.parametrize("option_key", sorted(PROMPT_OPTIONS))
def test_stored_value_overrides_the_default(option_key):
    field, _, resolver = PROMPT_OPTIONS[option_key]
    options = AppOptions.from_dict({option_key: "CUSTOM for {TARGET_LANG_NAME}",
                                    "TARGET_LANG_NAME": "Czech"})
    assert getattr(options, field) == "CUSTOM for {TARGET_LANG_NAME}"
    # ...and the resolver substitutes the language into the user's own text.
    assert getattr(options, resolver)() == "CUSTOM for Czech"


@pytest.mark.parametrize("option_key", sorted(PROMPT_OPTIONS))
def test_blank_value_falls_back_to_the_default(option_key):
    """Clearing a prompt in the UI stores NULL — that must restore the
    built-in default rather than send an empty system prompt."""
    field, constant, _ = PROMPT_OPTIONS[option_key]
    options = AppOptions.from_dict({option_key: None})
    assert getattr(options, field) == getattr(default_prompts, constant)


def test_no_handler_imports_a_default_prompt_constant():
    """Handlers must go through ctx.options.resolved_*_prompt(). Importing
    the constant bypasses the user's saved prompt without failing."""
    offenders = []
    for path in sorted(HANDLERS_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "app.db.default_prompts":
                names = [a.name for a in node.names]
                offenders.append(f"{path.name}: {', '.join(names)}")
    assert offenders == [], (
        "handlers must read prompts from ctx.options, not from constants: "
        + "; ".join(offenders)
    )


def test_options_endpoint_exposes_every_prompt_option():
    """GET /options is an explicit allow-list: an option missing from it is
    writable but never reads back, so its Options-drawer field silently
    shows the default instead of the saved value."""
    source = (HANDLERS_DIR.parents[1] / "api" / "routes" / "options.py").read_text(encoding="utf-8")
    missing = [key for key in PROMPT_OPTIONS if f'"{key}"' not in source]
    assert missing == [], f"not returned by GET /options: {missing}"


def test_prompt_defaults_have_no_data_file_dependency():
    """The defaults are code constants; the old app/prompts/*.txt tree is
    gone and must not come back (it made a prompt default behave unlike
    every other option default)."""
    assert not (HANDLERS_DIR.parents[1] / "prompts").exists()
    for _, constant, _ in PROMPT_OPTIONS.values():
        value = getattr(default_prompts, constant)
        assert isinstance(value, str) and value.strip()
        assert value == value.strip(), f"{constant} has stray surrounding whitespace"
