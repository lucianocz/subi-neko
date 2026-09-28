import re


def allows_ai_edit(is_user_edited: object, is_locked: object) -> bool:
    """Return whether an event may be modified by an AI pipeline stage."""
    return not bool(is_user_edited or is_locked)

_VALID_AFTER_BACKSLASH = frozenset('"\\\\/bfnrtu')

# Matches a trailing comma before a closing bracket or brace (with optional whitespace)
_TRAILING_COMMA_RE = re.compile(r',(\s*[\]\}])')

# Matches a JSON block wrapped in markdown code fences
_CODE_FENCE_RE = re.compile(r'```(?:json)?\s*([\s\S]*?)\s*```')


def sanitize_llm_json(raw: str) -> str:
    """Fix common JSON issues in LLM-generated output.

    1. Extracts JSON from markdown code fences if present.
    2. Removes trailing commas before ] or }.
    3. Fixes invalid backslash escapes.

    Matches \\\\ (valid pair, keep) before \\x (single, check validity).
    Without this precedence, \\\\N would corrupt to \\\\\\N.
    """
    # Unwrap markdown code fences if present
    fence_match = _CODE_FENCE_RE.search(raw)
    if fence_match:
        raw = fence_match.group(1)

    # Remove trailing commas before closing brackets/braces
    raw = _TRAILING_COMMA_RE.sub(r'\1', raw)

    def _fix(m: re.Match) -> str:
        s = m.group(0)
        if s == '\\\\':
            return s  # valid escape pair, keep as-is
        c = s[1]
        if c in _VALID_AFTER_BACKSLASH:
            return s  # valid JSON escape (\", \\, \/, \b, \f, \n, \r, \t, \uXXXX)
        return '\\\\' + c  # fix: double the stray backslash

    return re.sub(r'\\\\|\\[\s\S]', _fix, raw)
