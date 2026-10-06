"""Dialogue reflow: balanced wrap, auto-join, Czech break rules, ASS safety,
content-type scope and idempotency (``app.subs.line_breaking.reflow_dialogue``
plus its wiring in ``review_chunk_final``)."""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.db.models import File, Project, SubtitleChunk, SubtitleEvent
from app.db.options import AppOptions
from app.jobs.context import JobContext
from app.jobs.handlers import review_chunk_final as rcf
from app.jobs.handlers.review_chunk_final import _rebalance_dialogue_text
from app.subs.line_breaking import reflow_dialogue

MAX = 55
JOIN = 45


def reflow(text, **kw):
    kw.setdefault("join_under", JOIN)
    kw.setdefault("auto_join", True)
    kw.setdefault("auto_break", True)
    return reflow_dialogue(text, kw.pop("max_row_chars", MAX), **kw)


def rows(text):
    return text.split("\\N")


def vis(text):
    return re.sub(r"\{[^}]*\}", "", text)


# ---------------------------------------------------------------------------
# Wrap: balance
# ---------------------------------------------------------------------------

LONG = "Nikdy jsem nevěřil, že tohle místo opravdu existuje a že se do něj někdy dostanu s tebou"


def test_long_dialogue_wraps_into_two_rows_within_limit():
    out = reflow(LONG)
    assert out is not None
    r1, r2 = rows(out)
    assert len(r1) <= MAX and len(r2) <= MAX
    assert " ".join((r1, r2)) == LONG


def test_wrap_is_balanced_not_threshold_tail():
    out = reflow(LONG)
    r1, r2 = rows(out)
    # The old behaviour filled row 1 up to the limit and spilled the tail.
    old_style_tail = len(LONG) - MAX
    assert abs(len(r1) - len(r2)) < abs(MAX - old_style_tail)
    assert abs(len(r1) - len(r2)) <= 10


def test_existing_poor_54_12_split_is_rebalanced():
    text = "Nikdy jsem nevěřil, že tohle místo opravdu existuje a že se do\\Nněj dostanu"
    first, second = rows(text)
    assert abs(len(first) - len(second)) > 20  # clearly unbalanced to begin with
    out = reflow(text)
    assert out is not None
    r1, r2 = rows(out)
    assert abs(len(r1) - len(r2)) < abs(len(first) - len(second))
    assert " ".join((r1, r2)) == " ".join((first, second))


def test_good_existing_split_is_kept():
    text = "Nikdy jsem nevěřil, že tohle místo opravdu\\Nexistuje a že se do něj někdy dostanu s tebou"
    assert reflow(text) is None


def test_fitting_single_row_is_untouched():
    assert reflow("Ahoj, jak se máš?") is None


# ---------------------------------------------------------------------------
# Wrap: Czech break rules
# ---------------------------------------------------------------------------

def _every_break(text, width):
    """Rows of the reflowed text, or None when it needs no change/has no fit."""
    out = reflow(text, max_row_chars=width, auto_join=False)
    return None if out is None else rows(out)


def test_never_breaks_before_punctuation():
    # Spaced punctuation would be the only "perfectly balanced" split.
    text = "aaaa bbbb cccc dddd , eeee ffff gggg hhhh"
    r = _every_break(text, 25)
    assert r is not None
    assert not r[1].startswith(",")


@pytest.mark.parametrize("letter", list("aiksuvzo"))
def test_no_break_after_one_letter_word(letter):
    # Balance alone would put the break right after the one-letter word.
    text = f"Nechtěl jsem tam zůstat {letter} dívat se na všechny ostatní lidi okolo"
    r = _every_break(text, 45)
    assert r is not None
    assert r[0].split()[-1].casefold() != letter


@pytest.mark.parametrize("prep", ["do", "na", "od", "po", "za", "ze", "ke", "ve"])
def test_no_break_after_short_preposition(prep):
    text = f"Rozhodli jsme se dnes večer jít {prep} velkého starého domu u řeky"
    r = _every_break(text, 36)
    assert r is not None
    assert r[0].split()[-1] != prep


@pytest.mark.parametrize("clitic", ["se", "si", "by", "bych", "bys", "jsi", "jsem"])
def test_clitic_does_not_open_second_row(clitic):
    text = f"Naši nejlepší přátelé určitě {clitic} rozhodli odejít domů"
    r = _every_break(text, 34)
    assert r is not None
    assert r[1].split()[0] != clitic


def test_prefers_comma_boundary_over_perfect_balance():
    text = "Od dneška musíš vstávat dřív, když máš ranní doplňkové hodiny, ne?"
    r = _every_break(text, 42)
    assert r[0].endswith("dřív,")


def test_unbreakable_text_returns_none():
    assert reflow("Supercalifragilisticexpialidocious slovo", max_row_chars=20) is None


# ---------------------------------------------------------------------------
# Join
# ---------------------------------------------------------------------------

def test_short_wrapped_dialogue_joins():
    assert reflow("Ahoj,\\Njak se máš?") == "Ahoj, jak se máš?"


def test_join_inserts_exactly_one_space():
    out = reflow("Ahoj,\\Njak se máš?")
    assert "Ahoj,jak" not in out and "  " not in out


def test_existing_boundary_space_is_not_duplicated():
    assert reflow("Ahoj, \\Njak se máš?") == "Ahoj, jak se máš?"
    assert reflow("Ahoj,\\N jak se máš?") == "Ahoj, jak se máš?"
    assert reflow("Ahoj, \\N jak se máš?") == "Ahoj, jak se máš?"


def test_join_keeps_tag_at_boundary_with_single_space():
    assert reflow("Ahoj,\\N{\\i1}jak se máš?") == "Ahoj, {\\i1}jak se máš?"


def test_join_does_not_double_space_around_tag():
    out = reflow("Ahoj, {\\i0}\\N jak se máš?")
    assert "  " not in vis(out)
    assert vis(out).count(" ") == vis("Ahoj, jak se máš?").count(" ")
    assert "{\\i0}" in out


def test_join_respects_threshold_and_toggle():
    text = "Tohle je docela dlouhá věta,\\Nkterá se ale ještě vejde"
    joined_len = len(vis(text).replace("\\N", " "))
    assert reflow(text, join_under=joined_len) is not None
    assert reflow(text, join_under=joined_len - 1) is None   # over the join limit
    assert reflow(text, auto_join=False) is None             # toggle off


def test_join_capped_by_row_limit():
    # join_under larger than the row limit must never create an over-long row.
    text = "aaaa bbbb cccc\\Ndddd eeee ffff"
    out = reflow(text, max_row_chars=20, join_under=100)
    assert out is None or len(out) <= 20


def test_joined_boundary_uses_only_tag_text_changes():
    text = "{\\an8}Ahoj,\\N{\\i1}jak{\\i0} se máš?"
    out = reflow(text)
    assert out == "{\\an8}Ahoj, {\\i1}jak{\\i0} se máš?"


def test_multi_speaker_dash_dialogue_is_not_joined():
    assert reflow("- Ahoj.\\N- Nazdar.") is None
    assert reflow("– Ahoj.\\N– Nazdar.") is None
    assert reflow("{\\i1}- Ahoj.\\N- Nazdar.") is None


def test_single_dash_line_can_still_join():
    assert reflow("- Ahoj,\\Njak se máš?") == "- Ahoj, jak se máš?"


# ---------------------------------------------------------------------------
# ASS tag safety
# ---------------------------------------------------------------------------

def test_tags_survive_wrap_unchanged_and_in_order():
    text = "{\\an8}Šel jsem k tomu domu u lesa a pak jsem zabouchal na dveře {\\i1}hlasitě{\\i0} a dlouho"
    out = reflow(text, max_row_chars=42)
    assert out is not None
    assert re.findall(r"\{[^}]*\}", out) == re.findall(r"\{[^}]*\}", text)
    assert out.replace("\\N", " ") == text
    # never inside a block
    for block in re.findall(r"\{[^}]*\}", out):
        assert "\\N" not in block


def test_break_never_lands_inside_tag_with_spaces():
    text = "Tohle je jako pozdrav {\\fscx237\\fn Arial Black}strčit{\\r} mi jazyk do pusy, fakt hodně dlouhá věta"
    out = reflow(text, max_row_chars=42)
    assert out is not None
    assert "{\\fscx237\\fn Arial Black}" in out
    assert out.replace("\\N", " ") == text


@pytest.mark.parametrize("unsafe", [
    r"{\pos(10,20)}Dlouhá věta, která určitě přeteče přes padesát pět znaků na řádku ano",
    r"{\p1}m 0 0 l 100 0 100 100{\p0}\Nm 0 0",
    r"{\move(0,0,5,5)}Ahoj,\Njak se máš?",
    r"Ahoj\hsvěte,\Njak se máš?",
    r"Ahoj,\njak se máš?",
])
def test_unsafe_lines_are_left_alone(unsafe):
    assert reflow(unsafe) is None


def test_unclosed_brace_is_left_alone():
    assert reflow("Ahoj {\\i1 jak,\\Nse máš?") is None


def test_pipeline_placeholders_are_not_corrupted():
    # Masked markers/escape characters are plain word characters to the reflow.
    text = "⟦1⟧Ahoj,⏎jak se␣máš ⟦2⟧a tak dále a tak dále a ještě trochu dál a dál"
    out = reflow(text)
    for token in ("⟦1⟧", "⟦2⟧", "⏎", "␣"):
        assert out is None or token in out


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------

IDEMPOTENT_INPUTS = [
    LONG,
    "Ahoj,\\Njak se máš?",
    "Ahoj,\\N{\\i1}jak se máš?",
    "Nikdy jsem nevěřil, že tohle místo opravdu existuje a že se do\\Nněj dostanu",
    "{\\an8}Šel jsem k tomu domu u lesa a pak jsem zabouchal na dveře {\\i1}hlasitě{\\i0} a dlouho",
    "jedna dva tři čtyři pět šest sedm\\Nosm devět deset jedenáct dvanáct",
    "Tohle je docela dlouhá věta,\\Nkterá se ale ještě vejde",
]


@pytest.mark.parametrize("text", IDEMPOTENT_INPUTS)
@pytest.mark.parametrize("width,join", [(42, 45), (55, 45), (30, 45), (60, 20), (42, 100)])
def test_reflow_is_idempotent(text, width, join):
    once = reflow(text, max_row_chars=width, join_under=join)
    current = once if once is not None else text
    assert reflow(current, max_row_chars=width, join_under=join) is None


def test_join_wrap_never_oscillate_when_join_exceeds_row_limit():
    text = "aaaa bbbb cccc dddd eeee ffff\\Ngggg hhhh"   # joined 38 chars
    seen = [text]
    for _ in range(4):
        nxt = reflow(seen[-1], max_row_chars=30, join_under=60)
        if nxt is None:
            break
        seen.append(nxt)
    assert len(seen) <= 2


# ---------------------------------------------------------------------------
# Handler wiring: content-type scope
# ---------------------------------------------------------------------------

def test_handler_helper_joins_and_wraps():
    assert _rebalance_dialogue_text(
        "src", "Ahoj,\\Njak se máš?", MAX, join_under=JOIN, auto_join=True) == "Ahoj, jak se máš?"
    assert _rebalance_dialogue_text("src", LONG, MAX) is not None


def test_handler_helper_defaults_do_not_join():
    assert _rebalance_dialogue_text("src", "Ahoj,\\Njak se máš?", MAX) is None


def _run_review(tmp_path, monkeypatch, content_type: str, translated: str, *, options=None):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    Session = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(rcf, "SyncSessionLocal", Session)
    with Session() as s:
        project = Project(name="P", source_directory="p", anime_provider="anidb", anime_external_id="1")
        s.add(project)
        s.flush()
        file = File(project_id=project.id, filename="a.mkv", relative_path="a.mkv")
        s.add(file)
        s.flush()
        s.add(SubtitleChunk(file_id=file.id, chunk_index=0, translate_from_line=0,
                            translate_to_line=0, status="polished",
                            content_type=content_type))
        ev = SubtitleEvent(
            file_id=file.id, line_index=0, event_type="dialogue", content_type=content_type,
            layer=0, start_ms=0, end_ms=4000, original_start_ms=0, original_end_ms=4000,
            style="Default", source_text="src", translated_text=translated,
            original_ai_translated_text=translated, translation_status="validated")
        s.add(ev)
        s.commit()
        file_id, event_id = file.id, ev.id
    ctx = JobContext(Path("."), Path("."), options or AppOptions(max_row_chars=MAX, cps_limit=100.0))
    rcf.review_chunk_final({"file_id": file_id, "chunk_index": 0}, ctx, lambda *a, **k: None)
    with Session() as s:
        return s.get(SubtitleEvent, event_id).translated_text


def test_dialogue_is_reflowed_by_the_handler(tmp_path, monkeypatch):
    out = _run_review(tmp_path, monkeypatch, "dialogue", "Ahoj,\\Njak se máš?")
    assert out == "Ahoj, jak se máš?"


@pytest.mark.parametrize("content_type", ["sign", "karaoke", "song", "other"])
@pytest.mark.parametrize("text", ["Ahoj,\\Njak se máš?", LONG])
def test_non_dialogue_content_is_untouched(tmp_path, monkeypatch, content_type, text):
    assert _run_review(tmp_path, monkeypatch, content_type, text) == text
