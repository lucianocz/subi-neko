"""Deterministic Czech-quality and readability flaggers.

Most run in review_chunk_final after the polish pass; check_polish_drift runs
inside polish_chunk, on the pass's own before/after pairs. They are
*flaggers*, never auto-fixers: a hit either routes the line into the targeted
polish re-pass (first time) or surfaces as a warning QaItem for the reviewer.

Czech grammar tooling is not available, so these checks are deliberately
narrow, high-precision patterns rather than a grammar model.
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher

from app.subs.readability import char_budget, compute_cps, visible_len, visible_rows
from app.subs.tag_masking import plain_text

# Findings: (qa_type, message, details)
Finding = tuple[str, str, dict]

# ---------------------------------------------------------------------------
# Gender agreement — first-person past tense / conditional
#
# Czech past-tense participles agree with the speaker:
#   male:   "šel jsem", "byl bych", "viděl jsem"
#   female: "šla jsem", "byla bych", "viděla jsem"
# We check the participle adjacent to 1st-person auxiliaries (jsem/bych),
# in both orders ("viděl jsem" and "jsem viděl").
# ---------------------------------------------------------------------------

# Short words that routinely sit between the auxiliary and its participle
# ("jsem to udělal", "jsi se jí zeptal", "jsi mi to neřekla"). They are all
# clitics or short pronouns, so whatever follows the run is still the
# participle — allowing a couple of them is what makes the check fire on
# ordinary word order instead of only the textbook case.
_INTERVENING_CLITICS = "se|si|to|ho|mu|mi|ti|ji|jí|mě|tě|je|tam|už|ještě|nikdy"


def _participle_patterns(auxiliaries: str) -> tuple[re.Pattern, re.Pattern]:
    return (
        re.compile(rf"\b([\w]{{2,}}?)(la|lo|li|ly|l)\s+(?:{auxiliaries})\b",
                   re.IGNORECASE | re.UNICODE),
        re.compile(
            rf"\b(?:{auxiliaries})\s+(?:(?:{_INTERVENING_CLITICS})\s+){{0,2}}"
            rf"([\w]{{2,}}?)(la|lo|li|ly|l)\b",
            re.IGNORECASE | re.UNICODE),
    )


# 1st person singular — the participle agrees with the SPEAKER.
_PARTICIPLE_BEFORE_AUX, _PARTICIPLE_AFTER_AUX = _participle_patterns("jsem|bych")

# 2nd person singular — the participle agrees with the ADDRESSEE. At least as
# common in dialogue as the first person ("byl jsi" / "byla jsi"), and the
# error is invisible to the speaker-gender check above.
_PARTICIPLE_BEFORE_AUX_2SG, _PARTICIPLE_AFTER_AUX_2SG = _participle_patterns("jsi|bys")


def _participle_candidates(text: str, patterns: tuple[re.Pattern, re.Pattern]) -> list[tuple[str, str]]:
    candidates: list[tuple[str, str]] = []  # (word, ending)
    for pattern in patterns:
        for match in pattern.finditer(text):
            candidates.append((match.group(1) + match.group(2), match.group(2).lower()))
    return candidates


def _agreement_finding(
    candidates: list[tuple[str, str]],
    gender: str,
    message: str,
    details: dict,
) -> list[Finding]:
    if not candidates:
        return []

    expected = "l" if gender == "male" else "la"
    wrong = "la" if gender == "male" else "l"

    # A noun ending in -l/-la can sit next to the auxiliary ("stůl jsem
    # koupila"), so only flag when NO correctly-gendered participle candidate
    # exists — precision over recall.
    has_correct = any(ending == expected for _, ending in candidates)
    mismatched = [word for word, ending in candidates if ending == wrong]

    if mismatched and not has_correct:
        return [("gender_agreement", message, {**details, "words": mismatched[:5]})]
    return []


def check_gender_agreement(translated: str, speaker_gender: str | None) -> list[Finding]:
    if speaker_gender not in ("male", "female"):
        return []
    return _agreement_finding(
        _participle_candidates(plain_text(translated),
                               (_PARTICIPLE_BEFORE_AUX, _PARTICIPLE_AFTER_AUX)),
        speaker_gender,
        f"Past-tense form does not match speaker gender ({speaker_gender}).",
        {"speaker_gender": speaker_gender, "person": "speaker"},
    )


def check_addressee_gender_agreement(
    translated: str, addressee_gender: str | None, addressee: str | None = None,
) -> list[Finding]:
    """Second-person past-tense agreement against the gender of the person
    being addressed ("byl jsi" to a woman). The addressee is identified from
    the line itself — see infer_addressee."""
    if addressee_gender not in ("male", "female"):
        return []
    who = f" ({addressee})" if addressee else ""
    return _agreement_finding(
        _participle_candidates(plain_text(translated),
                               (_PARTICIPLE_BEFORE_AUX_2SG, _PARTICIPLE_AFTER_AUX_2SG)),
        addressee_gender,
        f"Second-person past-tense form does not match the gender of the "
        f"person being addressed{who}: {addressee_gender}.",
        {"addressee_gender": addressee_gender, "addressee": addressee, "person": "addressee"},
    )


# ---------------------------------------------------------------------------
# T–V mixing inside a single line ("můžeš mi říct, co vás sem přivádí?")
# Cross-line consistency needs addressee tracking (phase 2); a single line
# that mixes tykání and vykání towards the same addressee is almost always
# a translation error, so only that narrow case is flagged.
# ---------------------------------------------------------------------------

_QUOTED_TEXT = re.compile(r'"[^"\n]*"|„[^“\n]*“|‚[^‘\n]*‘|«[^»\n]*»')

# Pronouns other than bare ``ty`` are useful singular-informal evidence.  Ty
# itself is deliberately absent: in ordinary Czech ``ty dvě`` / ``ty knihy``
# is a demonstrative, and an isolated pronoun is not enough evidence for a
# deterministic warning.
_INFORMAL_PRONOUNS = re.compile(
    r"\b(tě|ti|tebe|tobě|tvůj|tvoje|tvá|tvé|tvého|tvou)\b", re.IGNORECASE)
_INFORMAL_AUXILIARIES = re.compile(r"\b(jsi|bys|abys)\b", re.IGNORECASE)

# A deliberately small lexicon of high-frequency, unmistakable 2sg finite
# forms.  A generic -š suffix rule also matches nouns and foreign names, so it
# is too broad for a high-precision subtitle flagger.  Prefixes are included
# only where the resulting form remains unambiguous.
_INFORMAL_VERBS = re.compile(
    r"\b(?:"
    r"můžeš|nemůžeš|musíš|nemusíš|chceš|nechceš|víš|nevíš|máš|nemáš|"
    r"jdeš|nejdeš|smíš|nesmíš|umíš|neumíš|jsi|nejsi|"
    r"uděláš|neuděláš|vypadáš|nevypadáš|pozveš|nepozveš|řekneš|neřekneš|"
    r"půjdeš|nepůjdeš|dokážeš|nedokážeš|vidíš|nevidíš|slyšíš|neslyšíš"
    r")\b",
    re.IGNORECASE,
)

# Plural-looking pronouns and finite verbs are ambiguous between polite
# singular and genuine plural.  Formal singular is only established when a
# plural auxiliary/conditional is paired with singular gender/number
# agreement ("jste ukradl", "měla byste").
_FORMAL_OR_PLURAL_MARKERS = re.compile(
    r"\b(vy|vás|vám|vámi|váš|vaše|vašeho|vaší|vaši|jste|byste|abyste)\b",
    re.IGNORECASE,
)
_EXPLICIT_PLURAL = re.compile(
    r"\b(?:vy|vás|vám|jste|byste|abyste)\s+"
    r"(?:dva|dvě|oba|obě|všichni|všechny)\b",
    re.IGNORECASE,
)
_FORMAL_SINGULAR_PATTERNS = (
    re.compile(
        r"\b(?:jste|byste|abyste)\b(?:\s+\w+){0,3}\s+"
        r"(\w{2,}(?:l|la|ný|ná|tý|tá|vý|vá))\b",
        re.IGNORECASE | re.UNICODE,
    ),
    re.compile(
        r"\b(\w{2,}(?:l|la|ný|ná|tý|tá|vý|vá))\b"
        r"(?:\s+\w+){0,3}\s+(?:jste|byste|abyste)\b",
        re.IGNORECASE | re.UNICODE,
    ),
)


def _tv_evidence(translated: str) -> dict[str, list[str]]:
    """Return grammatical evidence without deciding who is addressed.

    ``formal_or_plural`` is intentionally not promoted to ``formal_singular``
    unless singular agreement independently disambiguates it.  That keeps
    forms such as ``můžete``/``vás``/``běžte`` from manufacturing a T-V fact.
    Quoted speech is removed because it need not be addressed to the current
    interlocutor.
    """
    text = _QUOTED_TEXT.sub(" ", plain_text(translated))
    informal = (
        _INFORMAL_PRONOUNS.findall(text)
        + _INFORMAL_AUXILIARIES.findall(text)
        + _INFORMAL_VERBS.findall(text)
    )
    formal_singular: list[str] = []
    if not _EXPLICIT_PLURAL.search(text):
        for pattern in _FORMAL_SINGULAR_PATTERNS:
            formal_singular.extend(pattern.findall(text))
    return {
        "informal_singular": informal,
        "formal_singular": formal_singular,
        "formal_or_plural": _FORMAL_OR_PLURAL_MARKERS.findall(text),
        "explicit_plural": _EXPLICIT_PLURAL.findall(text),
    }


def check_tv_mixed_in_line(translated: str) -> list[Finding]:
    evidence = _tv_evidence(translated)
    t_hits = evidence["informal_singular"]
    v_hits = evidence["formal_singular"]
    if t_hits and v_hits:
        return [(
            "tv_address_mixed",
            "Line mixes informal (tykání) and formal (vykání) address.",
            {"informal": t_hits[:3], "formal": v_hits[:3]},
        )]
    return []


def check_tv_against_pairs(
    translated: str,
    speaker_pair_modes: set[str],
    addressee: str | None = None,
    speaker: str | None = None,
) -> list[Finding]:
    """Compare a line's T/V markers against the stored address pairs.

    A deterministic mismatch requires an exact directed pair and independent
    addressee evidence.  The grammatical form being checked is never used to
    infer the addressee.
    """
    if not addressee or speaker_pair_modes not in ({"tykani"}, {"vykani"}):
        return []
    expected = next(iter(speaker_pair_modes))
    evidence = _tv_evidence(translated)
    t_hits = evidence["informal_singular"]
    v_hits = evidence["formal_singular"]
    details_base = {
        "expected": expected,
        "speaker": speaker,
        "addressee": addressee,
    }

    if expected == "tykani" and v_hits and not t_hits:
        whom = f"toward {addressee}, whom {speaker or 'this speaker'} addresses informally"
        return [(
            "tv_address_mismatch",
            f"Formal address (vykání) used {whom}.",
            {**details_base, "found": v_hits[:3]},
        )]
    if expected == "vykani" and t_hits and not v_hits:
        whom = f"toward {addressee}, whom {speaker or 'this speaker'} addresses formally"
        return [(
            "tv_address_mismatch",
            f"Informal address (tykání) used {whom}.",
            {**details_base, "found": t_hits[:3]},
        )]
    return []


# ---------------------------------------------------------------------------
# Vocative — names in direct-address position must use the glossary's
# vocative form ("Ahoj, Tomáši!" not "Ahoj, Tomáš!")
# ---------------------------------------------------------------------------

def _direct_address_pattern(name: str) -> re.Pattern:
    """Line-initial "Name, …"/"Name!" or after a comma "…, Name." — the
    positions where a name is being used to address someone rather than to
    talk about them."""
    return re.compile(rf"(?:^|,\s+){re.escape(name)}(?:\s*[,.!?…]|$)", re.IGNORECASE)


def infer_addressee(translated: str, name_forms: dict[str, str]) -> str | None:
    """Who this line is spoken TO, when the line says so itself.

    name_forms maps a surface form (nominative or vocative, casefolded by the
    caller's construction) to the canonical name. Returns the canonical name
    of the first form found in a direct-address position, else None.

    This deliberately reads the translated text rather than the event's
    speaker field: real-world ASS files often carry no speaker attribution at
    all, but a line that addresses someone by name says so in the text.
    """
    text = plain_text(translated)
    best: tuple[int, str] | None = None
    for form, canonical in name_forms.items():
        if not form:
            continue
        match = _direct_address_pattern(form).search(text)
        if match is not None and (best is None or match.start() < best[0]):
            best = (match.start(), canonical)
    return best[1] if best is not None else None


def check_vocative(translated: str, vocatives: dict[str, str]) -> list[Finding]:
    """vocatives: nominative name → vocative form (from the glossary).
    Flags the nominative appearing in a direct-address position."""
    text = plain_text(translated)
    findings: list[Finding] = []
    for name, vocative in vocatives.items():
        if not name or not vocative or name == vocative:
            continue
        pattern = re.compile(
            rf"(?:^|,\s+){re.escape(name)}(?:\s*[,.!?…]|$)"
        )
        if pattern.search(text):
            findings.append((
                "vocative_missing",
                f'"{name}" appears in direct address — expected vocative "{vocative}".',
                {"name": name, "vocative": vocative},
            ))
    return findings


# ---------------------------------------------------------------------------
# Readability — CPS, row length, row count
# ---------------------------------------------------------------------------

def check_readability(
    translated: str,
    duration_ms: int,
    cps_limit: float,
    max_row_chars: int,
    source_text: str | None = None,
) -> list[Finding]:
    findings: list[Finding] = []
    rows = visible_rows(translated)

    cps = compute_cps(translated, 0, duration_ms)
    if cps is not None and cps > cps_limit:
        budget = char_budget(0, duration_ms, cps_limit, source_text)
        if budget is not None and visible_len(translated) > budget:
            findings.append((
                "high_cps",
                f"Reading speed {cps:.1f} CPS exceeds limit {cps_limit:.0f} "
                f"(fits in ~{budget} chars).",
                {"cps": round(cps, 1), "limit": cps_limit, "char_budget": budget},
            ))

    long_rows = [row for row in rows if len(row) > max_row_chars]
    if long_rows:
        findings.append((
            "long_row",
            f"Row exceeds {max_row_chars} characters.",
            {"row_lengths": [len(r) for r in rows], "limit": max_row_chars},
        ))

    if len([row for row in rows if row]) > 2:
        findings.append((
            "too_many_rows",
            "Subtitle wraps to more than 2 rows.",
            {"rows": len(rows)},
        ))

    return findings


# ---------------------------------------------------------------------------
# Untranslated English (ported from the retired rules review)
# ---------------------------------------------------------------------------

_ENGLISH_MARKERS = {
    "the", "and", "but", "that", "with", "have", "this", "from", "they",
    "what", "when", "your", "would", "about", "there", "their", "which",
    "could", "should", "where", "while", "because", "although", "however",
    "therefore", "moreover", "furthermore", "anyway", "something", "nothing",
    "everything", "everyone", "someone", "anyone",
}


# Unicode-aware word tokens; \d and _ excluded. Czech diacritic words count
# as translated-language evidence in the ratio denominator.
_WORD_RE = re.compile(r"\b[^\W\d_]{3,}\b", re.UNICODE)

# Name-honorific compounds ("Ako-neechan", "Kiryuu-sensei") are preserved
# verbatim by design and must not count as untranslated-English evidence.
_HYPHEN_COMPOUND_RE = re.compile(r"\b\w+(?:-\w+)+\b", re.UNICODE)


def check_untranslated_english(
    source: str,
    translated: str,
    exclude_terms: set[str] | None = None,
) -> list[Finding]:
    src_clean = _HYPHEN_COMPOUND_RE.sub(" ", plain_text(source))
    tgt_clean = _HYPHEN_COMPOUND_RE.sub(" ", plain_text(translated))

    src_tokens = _WORD_RE.findall(src_clean)
    tgt_tokens = _WORD_RE.findall(tgt_clean)

    exclude = {t.lower() for t in (exclude_terms or set())}
    # Proper names survive translation on purpose: a token capitalized in
    # both source and target is a name, not untranslated English.
    exclude |= (
        {t.lower() for t in src_tokens if t[0].isupper()}
        & {t.lower() for t in tgt_tokens if t[0].isupper()}
    )

    src_words = {t.lower() for t in src_tokens if t.lower() not in exclude}
    tgt_words = {t.lower() for t in tgt_tokens if t.lower() not in exclude}
    # Only ASCII target tokens can be untranslated English; Czech words in
    # tgt_words still dilute the overlap ratio via the denominator.
    ascii_tgt = {w for w in tgt_words if w.isascii()}

    english_in_tgt = ascii_tgt & _ENGLISH_MARKERS

    overlap_ratio = 0.0
    if src_words and tgt_words:
        overlap = src_words & ascii_tgt
        overlap_ratio = len(overlap) / max(len(src_words), len(tgt_words))

    issues: list[str] = []
    if len(english_in_tgt) >= 2:
        issues.append("english_markers")
    if overlap_ratio > 0.5 and len(src_words) >= 4:
        issues.append("high_source_overlap")

    if issues:
        return [(
            "untranslated_english",
            "Translation may still contain untranslated English.",
            {
                "english_markers_found": sorted(english_in_tgt),
                "source_target_overlap_ratio": round(overlap_ratio, 2),
                "issues": issues,
            },
        )]
    return []


# ---------------------------------------------------------------------------
# Polish drift — did the rewrite change what the line MEANS?
#
# The polish pass runs the better model over every translated line and may
# rewrite it freely. Nothing downstream checks that meaning survived:
# validate_chunk is markup-only and the checks above are surface-level. These
# four signals are the cheap, deterministic part of that gap — they do not
# judge style, only flag rewrites that changed something a rewrite has no
# business changing.
# ---------------------------------------------------------------------------

_DIGIT_RUN_RE = re.compile(r"\d+")

# A deliberately small set of unambiguous, high-frequency finite verb forms.
# Czech ``ne-`` is productive, but treating every ``ne...`` token as a verb
# also treats ordinary words such as ``nebe`` and ``nemoc`` as negations.
# Precision matters more than recall here; broader semantic comparison belongs
# to Final QA.
_POLARITY_VERB_FORMS = {
    "chci", "chceš", "chce", "chceme", "chcete",
    "mám", "máš", "má", "máme", "máte", "mají",
    "vím", "víš", "ví", "víme", "víte",
    "zvládnu", "zvládneš", "zvládne", "zvládneme", "zvládnete",
}
_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)


def _negation_flip(old: str, new: str) -> list[dict[str, str]]:
    """Return narrow evidence of an aligned finite verb gaining/losing ``ne``.

    Sequence alignment and a shared neighbouring token keep a positive form
    in one clause from being paired with a negative form elsewhere. Complete
    word tokens are compared; arbitrary substrings never participate.
    """
    old_words = [w.casefold() for w in _WORD_RE.findall(old)]
    new_words = [w.casefold() for w in _WORD_RE.findall(new)]
    changes: list[dict[str, str]] = []

    for tag, old_start, old_end, new_start, new_end in SequenceMatcher(
        None, old_words, new_words, autojunk=False,
    ).get_opcodes():
        if tag != "replace" or old_end - old_start != new_end - new_start:
            continue
        for offset, (old_word, new_word) in enumerate(zip(
            old_words[old_start:old_end], new_words[new_start:new_end],
        )):
            if old_word == "ne" + new_word and new_word in _POLARITY_VERB_FORMS:
                positive, negative, direction = new_word, old_word, "removed"
            elif new_word == "ne" + old_word and old_word in _POLARITY_VERB_FORMS:
                positive, negative, direction = old_word, new_word, "added"
            else:
                continue

            old_index = old_start + offset
            new_index = new_start + offset
            left_matches = (
                old_index > 0 and new_index > 0
                and old_words[old_index - 1] == new_words[new_index - 1]
            )
            right_matches = (
                old_index + 1 < len(old_words) and new_index + 1 < len(new_words)
                and old_words[old_index + 1] == new_words[new_index + 1]
            )
            only_tokens = len(old_words) == len(new_words) == 1
            if left_matches or right_matches or only_tokens:
                changes.append({
                    "positive": positive,
                    "negative": negative,
                    "direction": direction,
                })
    return changes

# Below this many visible characters, a large relative length change is not
# evidence of anything — one word in a three-word line moves it.
_DRIFT_MIN_LENGTH = 20
_DRIFT_LENGTH_RATIO = 0.6


def check_polish_drift(
    before: str,
    after: str,
    reason: str | None = None,
    glossary_targets: list[str] | None = None,
) -> list[Finding]:
    """Compare a polish edit's before/after for meaning-bearing changes."""
    old = plain_text(before or "")
    new = plain_text(after or "")
    if not old.strip() or not new.strip():
        return []

    reasons: list[str] = []
    details: dict = {}

    old_digits = _DIGIT_RUN_RE.findall(old)
    new_digits = _DIGIT_RUN_RE.findall(new)
    if sorted(old_digits) != sorted(new_digits):
        reasons.append("numbers_changed")
        details["numbers"] = {"before": old_digits, "after": new_digits}

    polarity_changes = _negation_flip(old, new)
    if polarity_changes:
        reasons.append("negation_changed")
        details["polarity_changes"] = polarity_changes

    dropped = [
        term for term in (glossary_targets or [])
        if term and term.casefold() in old.casefold()
        and term.casefold() not in new.casefold()
    ]
    if dropped:
        reasons.append("glossary_term_dropped")
        details["dropped_terms"] = dropped[:5]

    # A length rule would double-report an edit the model itself labelled as
    # condensing, which is the one case where a big change is the point.
    if reason != "length" and max(len(old), len(new)) >= _DRIFT_MIN_LENGTH:
        change = abs(len(new) - len(old)) / max(len(old), 1)
        if change > _DRIFT_LENGTH_RATIO:
            reasons.append("length_jump")
            details["length"] = {"before": len(old), "after": len(new),
                                 "change": round(change, 2)}

    if not reasons:
        return []

    details["reasons"] = reasons
    details["before"] = old
    details["after"] = new
    return [(
        "polish_drift",
        "Polish edit may have changed the line's polarity or protected details ("
        + ", ".join(reasons) + ").",
        details,
    )]
