from __future__ import annotations

from app.subs.line_breaking import (
    empty_hard_break_edges,
    rebalance_empty_hard_break,
    rebalance_rows,
)


def test_short_line_untouched():
    assert rebalance_rows("Ahoj, jak se máš?", 42) is None


def test_long_line_split_at_word_boundary():
    text = "Od dneška musíš vstávat dřív, když máš ranní doplňkové hodiny, ne?"
    fixed = rebalance_rows(text, 42)
    assert fixed is not None
    rows = fixed.split("\\N")
    assert len(rows) == 2
    assert all(len(r) <= 42 for r in rows)
    assert " ".join(rows) == text


def test_split_is_balanced():
    fixed = rebalance_rows("aaaa bbbb cccc dddd eeee ffff gggg hhhh", 25)
    assert fixed is not None
    r1, r2 = fixed.split("\\N")
    assert abs(len(r1) - len(r2)) <= 5


def test_existing_break_with_long_row_rebalanced():
    text = "Je to náš HONEY BOY, náš SHY BOY a o lásku\\Nse rozdělíme"
    fixed = rebalance_rows(text, 30)
    assert fixed is not None
    assert all(len(r) <= 30 for r in fixed.split("\\N"))


def test_existing_break_that_fits_untouched():
    assert rebalance_rows("první řádek\\Ndruhý řádek", 42) is None


def test_leading_override_block_preserved():
    text = "{\\an8\\i1}Od dneška musíš vstávat dřív, když máš ranní doplňkové hodiny, ne?"
    fixed = rebalance_rows(text, 42)
    assert fixed is not None
    assert fixed.startswith("{\\an8\\i1}")
    body = fixed[len("{\\an8\\i1}"):]
    assert all(len(r) <= 42 for r in body.split("\\N"))


def test_inline_override_skipped():
    text = "Tohle je jako pozdrav {\\fscx237}-{\\r} strčit mi jazyk do pusy, fakt hodně dlouhá věta"
    assert rebalance_rows(text, 42) is None


def test_soft_break_and_hard_space_skipped():
    assert rebalance_rows("dlouhá věta se soft breakem\\na pokračováním které přeteče limit řádku", 30) is None
    assert rebalance_rows("dlouhá\\hvěta s hard\\hspace znaky která přeteče limit řádku úplně", 30) is None


def test_unbreakable_word_returns_none():
    assert rebalance_rows("Supercalifragilisticexpialidocious slovo", 20) is None


def test_needs_three_rows_returns_none():
    # Can't fit in two rows of 20 → left for the reviewer (long_row flags it).
    text = "jedna dva tři čtyři pět šest sedm osm devět deset jedenáct dvanáct třináct"
    assert rebalance_rows(text, 20) is None


def test_empty_and_markup_only():
    assert rebalance_rows("", 42) is None
    assert rebalance_rows("{\\pos(1,2)}", 42) is None


def test_empty_hard_break_edges_ignore_override_tags():
    assert empty_hard_break_edges(r"{\an8}\NText") == {"leading"}
    assert empty_hard_break_edges(r"Text\N{\i0}") == {"trailing"}
    assert empty_hard_break_edges(r"{\an8}Text\NMore{\i0}") == set()


def test_empty_hard_break_is_moved_without_changing_count():
    text = r"a s nápadníky porazí finálního bosse. \N"
    fixed = rebalance_empty_hard_break(text, 42)
    assert fixed is not None
    assert fixed.count(r"\N") == text.count(r"\N") == 1
    assert not empty_hard_break_edges(fixed)
    assert " ".join(fixed.split(r"\N")) == text.replace(r"\N", "").strip()


def test_complex_empty_hard_break_is_not_auto_fixed():
    assert rebalance_empty_hard_break(r"Text{\i0}\N", 42) is None
    assert rebalance_empty_hard_break(r"Text\hmore\N", 42) is None
    assert rebalance_empty_hard_break(r"Onlyword\N", 42) is None


# ---------------------------------------------------------------------------
# Break-point quality (cost function, not pure balance)
# ---------------------------------------------------------------------------

def _rows(text: str, width: int) -> list[str]:
    fixed = rebalance_rows(text, width)
    assert fixed is not None
    return fixed.split("\\N")


def test_prefers_clause_boundary_over_stranded_conjunction():
    # Pure balance put "když" at the end of row 1 (diff 3 vs 7); the
    # conjunction belongs with the clause it introduces.
    text = "Od dneška musíš vstávat dřív, když máš ranní doplňkové hodiny, ne?"
    row1, row2 = _rows(text, 42)
    assert row1.endswith("dřív,")
    assert row2.startswith("když")


def test_preposition_stays_with_its_noun_phrase():
    # Pure balance broke between "na" and "kuchyňském".
    text = "Nechal jsem ten dopis ležet na kuchyňském stole vedle klíčů."
    row1, row2 = _rows(text, 34)
    assert not row1.endswith(" na")
    assert row2.startswith("na ")


def test_enclitic_never_opens_the_second_row():
    # Balance alone ties here, and the tie used to go to the split that puts
    # the reflexive "se" at the head of row 2. An enclitic leans on the word
    # before it, so it has to stay on row 1.
    text = "Naši nejlepší přátelé se rozhodli odejít domů."
    row1, row2 = _rows(text, 26)
    assert row1.endswith(" se")
    assert row2.startswith("rozhodli")


def test_balance_still_decides_when_no_boundary_is_special():
    # No punctuation, no function words — the old balance rule stands.
    fixed = rebalance_rows("aaaa bbbb cccc dddd eeee ffff gggg hhhh", 25)
    assert fixed is not None
    r1, r2 = fixed.split("\\N")
    assert abs(len(r1) - len(r2)) <= 5


def test_cost_function_never_breaks_row_length_limit():
    text = "Musíš mi slíbit, že se nikdy nevrátíš do toho starého domu u řeky."
    for width in range(20, 45):
        fixed = rebalance_rows(text, width)
        if fixed is None:
            continue
        rows = fixed.split("\\N")
        assert all(len(r) <= width for r in rows), (width, rows)
        assert " ".join(rows) == text
