from __future__ import annotations

import pysubs2

from app.jobs.handlers.extract_subtitles import (
    _PLAINTEXT_FONT_NAME,
    _PLAINTEXT_FONT_SIZE,
    _PLAINTEXT_PLAY_RES_X,
    _PLAINTEXT_PLAY_RES_Y,
    _apply_plaintext_defaults,
)


def test_srt_parses_and_gets_ass_export_defaults():
    subs = pysubs2.SSAFile.from_string(
        "1\n"
        "00:00:09,009 --> 00:00:10,552\n"
        "Again?\n\n"
        "2\n"
        "00:00:10,677 --> 00:00:13,055\n"
        "I-I'm sorry!\n\n"
        "3\n"
        "00:00:14,097 --> 00:00:16,307\n"
        "Don't cut yourself on the shards!\n",
        format_="srt",
    )
    _apply_plaintext_defaults(subs)

    assert [(event.start, event.end, event.text) for event in subs] == [
        (9009, 10552, "Again?"),
        (10677, 13055, "I-I'm sorry!"),
        (14097, 16307, "Don't cut yourself on the shards!"),
    ]
    assert all(event.style == "Default" for event in subs)
    assert subs.info["PlayResX"] == str(_PLAINTEXT_PLAY_RES_X)
    assert subs.info["PlayResY"] == str(_PLAINTEXT_PLAY_RES_Y)
    style = subs.styles["Default"]
    assert style.fontname == _PLAINTEXT_FONT_NAME
    assert style.fontsize == _PLAINTEXT_FONT_SIZE
    assert style.alignment == 2
    assert style.marginv == 45


def test_srt_multiline_text_survives_as_ass_line_break():
    subs = pysubs2.SSAFile.from_string(
        "1\n00:00:01,000 --> 00:00:03,000\nFirst line\nSecond line\n",
        format_="srt",
    )
    _apply_plaintext_defaults(subs)

    assert subs[0].text == r"First line\NSecond line"
