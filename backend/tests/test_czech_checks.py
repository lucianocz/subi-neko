from __future__ import annotations

import pytest

from app.subs.czech_checks import (
    check_addressee_gender_agreement,
    check_gender_agreement,
    check_polish_drift,
    check_readability,
    check_tv_against_pairs,
    check_tv_mixed_in_line,
    check_untranslated_english,
    check_vocative,
    infer_addressee,
)


# ---------------------------------------------------------------------------
# Gender agreement
# ---------------------------------------------------------------------------

def test_gender_agreement_flags_masculine_form_for_female_speaker():
    findings = check_gender_agreement("Šel jsem domů.", "female")
    assert findings
    assert findings[0][0] == "gender_agreement"


def test_gender_agreement_flags_feminine_form_for_male_speaker():
    findings = check_gender_agreement("Viděla jsem to.", "male")
    assert findings
    assert findings[0][0] == "gender_agreement"


def test_gender_agreement_accepts_correct_feminine_form():
    assert check_gender_agreement("Šla jsem domů.", "female") == []


def test_gender_agreement_accepts_correct_masculine_form():
    assert check_gender_agreement("Byl bych rád.", "male") == []


def test_gender_agreement_handles_inverted_order():
    findings = check_gender_agreement("Já jsem šel domů.", "female")
    assert findings


def test_gender_agreement_ignores_unknown_gender():
    assert check_gender_agreement("Šel jsem domů.", None) == []
    assert check_gender_agreement("Šel jsem domů.", "non_binary") == []


def test_gender_agreement_noun_before_aux_not_flagged_when_correct_form_present():
    # "stůl" ends in -l but the real participle "koupila" agrees — precision
    # heuristic: any correctly-gendered candidate suppresses the flag.
    assert check_gender_agreement("Ten stůl jsem koupila včera.", "female") == []


def test_gender_agreement_ignores_ass_markup():
    findings = check_gender_agreement(r"{\i1}Šel jsem{\i0} domů.", "female")
    assert findings


# ---------------------------------------------------------------------------
# T–V mixing within one line
# ---------------------------------------------------------------------------

def test_tv_mixed_in_line_flags_mixture():
    findings = check_tv_mixed_in_line("Můžeš mi říct, co jste udělal?")
    assert findings
    assert findings[0][0] == "tv_address_mixed"


def test_tv_consistent_informal_not_flagged():
    assert check_tv_mixed_in_line("Můžeš mi říct, co tě sem přivádí?") == []


def test_tv_consistent_formal_not_flagged():
    assert check_tv_mixed_in_line("Můžete mi říct, co vás sem přivádí?") == []


def test_tv_against_pairs_flags_formal_for_known_informal_addressee():
    findings = check_tv_against_pairs(
        "Slyšela jsem, že jste ukradl náhrdelník.",
        {"tykani"},
        addressee="Leon",
        speaker="Luxion",
    )
    assert findings
    assert findings[0][0] == "tv_address_mismatch"


@pytest.mark.parametrize("text", [
    "Slyšela jsem, že jste ukradl náhrdelník.",
    "Měl byste odejít.",
    "Abyste byl připravený, přijďte včas.",
])
def test_tv_against_informal_pair_detects_disambiguated_formal_singular(text):
    assert check_tv_against_pairs(
        text, {"tykani"}, addressee="Leon", speaker="Luxion")


def test_tv_against_pairs_flags_informal_for_known_formal_addressee():
    findings = check_tv_against_pairs(
        "Co tě sem přivádí?", {"vykani"}, addressee="Leon", speaker="Luxion")
    assert findings


def test_tv_against_pairs_silent_for_mixed_relationships():
    assert check_tv_against_pairs(
        "Co tě sem přivádí?", {"tykani", "vykani"}, addressee="Leon") == []
    assert check_tv_against_pairs("Co tě sem přivádí?", {"mixed"}, addressee="Leon") == []


def test_tv_against_pairs_silent_when_matching():
    assert check_tv_against_pairs("Co tě sem přivádí?", {"tykani"}, addressee="Leon") == []


@pytest.mark.parametrize("text", [
    "ty dvě se mi nezamlouvají",
    "Svatá, ty dvě mi nejsou po chuti.",
    "vás dva tu nepotřebuju",
    "Nepotřebuju vás. Klidně běžte.",
    "Pane Leone, vypadáš unaveně.",
    'Řekl: „Ty dvě se mi nezamlouvají.“',
])
def test_tv_mixed_avoids_demonstrative_plural_title_and_quoted_false_positives(text):
    assert check_tv_mixed_in_line(text) == []


@pytest.mark.parametrize("text", [
    "Nikoho nepozveš?",
    "Jen abys je naštval.",
    "Říkal jsem si, že to uděláš.",
    "Pane Leone, vypadáš unaveně.",
    "Ty jsi to věděl.",
    "Byl bys rád.",
])
def test_tv_against_formal_pair_detects_clear_singular_informal_morphology(text):
    assert check_tv_against_pairs(
        text, {"vykani"}, addressee="Leon Fou Bartfort", speaker="Luxion")


@pytest.mark.parametrize("text", [
    "ty dvě se mi nezamlouvají",
    "Svatá, ty dvě mi nejsou po chuti.",
    "vás dva tu nepotřebuju",
    "Nepotřebuju vás. Klidně běžte.",
    "Můžete odejít.",
    'Řekl: „Ty jsi to věděl.“',
])
def test_tv_against_pairs_ignores_non_address_plural_quoted_and_ambiguous_forms(text):
    assert check_tv_against_pairs(
        text, {"vykani"}, addressee="Leon Fou Bartfort", speaker="Luxion") == []
    assert check_tv_against_pairs(
        text, {"tykani"}, addressee="Leon Fou Bartfort", speaker="Luxion") == []


def test_tv_against_pairs_requires_independently_known_addressee():
    assert check_tv_against_pairs("Nikoho nepozveš?", {"vykani"}) == []


def test_tv_mismatch_details_identify_directed_relationship():
    finding = check_tv_against_pairs(
        "Jen abys je naštval.",
        {"vykani"},
        addressee="Leon Fou Bartfort",
        speaker="Luxion",
    )[0]
    assert finding[2] == {
        "expected": "vykani",
        "speaker": "Luxion",
        "addressee": "Leon Fou Bartfort",
        "found": ["abys"],
    }


# ---------------------------------------------------------------------------
# Vocative
# ---------------------------------------------------------------------------

def test_vocative_flags_nominative_in_direct_address():
    findings = check_vocative("Ahoj, Tomáš!", {"Tomáš": "Tomáši"})
    assert findings
    assert findings[0][0] == "vocative_missing"


def test_vocative_flags_line_initial_address():
    findings = check_vocative("Tomáš, pojď sem.", {"Tomáš": "Tomáši"})
    assert findings


def test_vocative_ignores_name_in_normal_position():
    assert check_vocative("Tomáš šel domů.", {"Tomáš": "Tomáši"}) == []


def test_vocative_accepts_correct_vocative_form():
    assert check_vocative("Ahoj, Tomáši!", {"Tomáš": "Tomáši"}) == []


def test_vocative_skips_names_without_distinct_form():
    assert check_vocative("Ahoj, Aria!", {"Aria": "Aria"}) == []


# ---------------------------------------------------------------------------
# Readability
# ---------------------------------------------------------------------------

def test_readability_flags_high_cps():
    text = "Tohle je opravdu velmi dlouhý titulek, který nelze přečíst."
    findings = check_readability(text, duration_ms=1000, cps_limit=20.0, max_row_chars=42)
    assert any(f[0] == "high_cps" for f in findings)


def test_readability_accepts_normal_line():
    findings = check_readability("Ahoj.", duration_ms=1500, cps_limit=20.0, max_row_chars=42)
    assert findings == []


def test_readability_flags_long_row():
    text = "x" * 60
    findings = check_readability(text, duration_ms=60000, cps_limit=20.0, max_row_chars=42)
    assert any(f[0] == "long_row" for f in findings)


def test_readability_row_split_on_ass_newline():
    text = ("x" * 30) + r"\N" + ("y" * 30)
    findings = check_readability(text, duration_ms=60000, cps_limit=20.0, max_row_chars=42)
    assert not any(f[0] == "long_row" for f in findings)


def test_readability_flags_three_rows():
    text = r"a\Nb\Nc"
    findings = check_readability(text, duration_ms=60000, cps_limit=20.0, max_row_chars=42)
    assert any(f[0] == "too_many_rows" for f in findings)


def test_readability_zero_duration_skips_cps():
    findings = check_readability("Dlouhý text " * 20, duration_ms=0, cps_limit=20.0, max_row_chars=1000)
    assert not any(f[0] == "high_cps" for f in findings)


# ---------------------------------------------------------------------------
# Untranslated English
# ---------------------------------------------------------------------------

def test_untranslated_english_flags_english_output():
    findings = check_untranslated_english(
        "But there is something about this place.",
        "But there is something about this place.",
    )
    assert findings
    assert findings[0][0] == "untranslated_english"


def test_untranslated_english_accepts_czech_output():
    assert check_untranslated_english(
        "But there is something about this place.",
        "Ale na tomhle místě něco je.",
    ) == []


def test_untranslated_english_ignores_honorific_compounds():
    # "Ako-neechan"/"Riko-neechan" are preserved by design — a translated
    # line dominated by them must not flag as untranslated.
    assert check_untranslated_english(
        "Give me a break Ako-neechan, Riko-neechan...",
        "Dejte mi pokoj, Ako-neechan, Riko-neechan...",
    ) == []


def test_untranslated_english_ignores_shared_proper_names():
    # Names capitalized in both source and target are names, not English.
    assert check_untranslated_english(
        "Ako and Riko-senpai? Kiryuu-sensei?",
        "Ako a Riko-sempai? Kiryuu-sensei?",
    ) == []


def test_untranslated_english_ignores_glossary_terms():
    assert check_untranslated_english(
        "The Honey Boy and the Shy Boy share everything.",
        "Honey Boy a Shy Boy se dělí o všechno.",
        exclude_terms={"honey", "boy", "shy"},
    ) == []


def test_untranslated_english_still_flags_verbatim_lyric():
    findings = check_untranslated_english(
        "Please don't cease that twinkling",
        "Please don't cease that twinkling",
    )
    assert findings
    assert findings[0][0] == "untranslated_english"


# ---------------------------------------------------------------------------
# Addressee inference
# ---------------------------------------------------------------------------

_FORMS = {"tomáš": "Tomáš", "tomáši": "Tomáš", "aria": "Aria", "ario": "Aria"}


def test_infer_addressee_from_line_initial_address():
    assert infer_addressee("Tomáši, pojď sem!", _FORMS) == "Tomáš"


def test_infer_addressee_after_comma():
    assert infer_addressee("Tak pojď, Ario.", _FORMS) == "Aria"


def test_infer_addressee_ignores_name_as_subject():
    # Talking ABOUT someone is not addressing them.
    assert infer_addressee("Tomáš to včera viděl.", _FORMS) is None


def test_infer_addressee_none_without_a_known_name():
    assert infer_addressee("Pojď sem, kamaráde.", _FORMS) is None


def test_infer_addressee_picks_the_earliest_address():
    assert infer_addressee("Ario, řekni to Tomáši.", _FORMS) == "Aria"


# ---------------------------------------------------------------------------
# Second-person (addressee) gender agreement
# ---------------------------------------------------------------------------

def test_addressee_agreement_flags_masculine_form_for_female_addressee():
    findings = check_addressee_gender_agreement("Kde jsi byl celou noc?", "female", "Aria")
    assert findings
    assert findings[0][0] == "gender_agreement"
    assert findings[0][2]["person"] == "addressee"
    assert findings[0][2]["addressee"] == "Aria"


def test_addressee_agreement_flags_feminine_form_for_male_addressee():
    findings = check_addressee_gender_agreement("Ty jsi to viděla.", "male")
    assert findings


def test_addressee_agreement_accepts_matching_form():
    assert check_addressee_gender_agreement("Kde jsi byla celou noc?", "female") == []
    assert check_addressee_gender_agreement("Byl bys rád.", "male") == []


def test_addressee_agreement_ignores_unknown_gender():
    assert check_addressee_gender_agreement("Kde jsi byl?", None) == []
    assert check_addressee_gender_agreement("Kde jsi byl?", "unknown") == []


def test_addressee_agreement_ignores_first_person_forms():
    """The speaker's own past tense is the other check's business — a female
    speaker saying "byla jsem" to a male addressee must not be flagged."""
    assert check_addressee_gender_agreement("Byla jsem tam taky.", "male") == []


def test_speaker_agreement_ignores_second_person_forms():
    """...and symmetrically: "byl jsi" says nothing about the speaker."""
    assert check_gender_agreement("Kde jsi byl?", "female") == []


def test_agreement_sees_through_intervening_clitics():
    """Ordinary Czech word order puts clitics between the auxiliary and the
    participle. Matching only the adjacent case missed most real lines."""
    assert check_gender_agreement("Já jsem to udělala.", "male")
    assert check_gender_agreement("Nikdy jsem se jí nezeptala.", "male")
    assert check_addressee_gender_agreement("Ty jsi mi to neřekla.", "male")
    # ...without losing the precision guard.
    assert check_gender_agreement("Ten stůl jsem koupila včera.", "female") == []
    assert check_gender_agreement("Já jsem to udělal.", "male") == []


# ---------------------------------------------------------------------------
# Polish drift
# ---------------------------------------------------------------------------

def test_drift_flags_added_negation():
    findings = check_polish_drift("Zvládnu to sám.", "Nezvládnu to sám.")
    assert findings
    assert findings[0][0] == "polish_drift"
    assert "negation_changed" in findings[0][2]["reasons"]


def test_drift_flags_changed_numbers():
    findings = check_polish_drift("Musíme vydržet 12 dní.", "Musíme vydržet 13 dní.")
    assert findings
    assert "numbers_changed" in findings[0][2]["reasons"]


def test_drift_flags_dropped_glossary_term():
    findings = check_polish_drift(
        "Aria to ví moc dobře.", "Ona to ví moc dobře.", None, ["Aria"])
    assert findings
    assert findings[0][2]["dropped_terms"] == ["Aria"]


def test_drift_tolerates_inflected_glossary_term():
    # Czech inflects names; the base form still prefixes the inflected one,
    # so a normal case change must not read as a dropped term.
    assert check_polish_drift(
        "Viděl jsem Ariu včera večer.", "Ariu jsem viděl včera.", None, ["Aria"]) == []


def test_drift_quiet_on_a_faithful_rewrite():
    # Both keep exactly one negation and say the same thing.
    assert check_polish_drift("Nevím, co mám dělat.", "Netuším, co dělat.") == []


def test_drift_length_jump_not_reported_for_deliberate_condensing():
    before = "Tohle je opravdu velmi dlouhá věta, která nic neříká."
    after = "Zbytečná věta."
    assert any("length_jump" in f[2]["reasons"]
               for f in check_polish_drift(before, after))
    # ...but the polish pass saying it condensed makes the change expected.
    assert not any("length_jump" in f[2]["reasons"]
                   for f in check_polish_drift(before, after, "length"))


def test_drift_ignores_short_lines_and_markup():
    assert check_polish_drift("Ano.", "Jo.") == []
    assert check_polish_drift("{\\i1}Ano.{\\i0}", "{\\i1}Jo.{\\i0}") == []


def test_drift_ignores_empty_sides():
    assert check_polish_drift("", "Něco") == []
    assert check_polish_drift("Něco", "") == []
