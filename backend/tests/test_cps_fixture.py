"""Shared CPS cases — the frontend (`frontend/tests/qcLiveCps.test.ts`) asserts
the same fixture against its port in `frontend/src/utils/cps.ts`."""
import json
from pathlib import Path

import pytest

from app.subs.readability import compute_cps

_CASES = json.loads((Path(__file__).parent / "fixtures" / "cps_cases.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", _CASES, ids=[c["name"] for c in _CASES])
def test_compute_cps_matches_shared_fixture(case):
    got = compute_cps(case["text"], case["start_ms"], case["end_ms"])
    if case["cps"] is None:
        assert got is None
    else:
        assert got == pytest.approx(case["cps"])
