"""Repo-level guard: the smoke lane's test count is a deliberate budget.

`.github/workflows/e2e-smoke.yml` runs `-m "smoke and compose"` as a
PR-blocking required check with a `timeout-minutes: 40` bound
(`tests/test_e2e_smoke_workflow_guards.py` pins both). That budget only
holds because the lane is small: each `@pytest.mark.smoke` test boots or
touches the full Compose stack, so the lane's wall-clock scales with test
count, not just with code changes in a given PR.

Before this test, "exactly 6 smoke tests" was enforced only by comments
(see the per-file docstrings in
`services/inh-public-api-svc/tests/integration/test_compose_*.py`). Nothing
stopped a future PR from silently tagging a seventh, eighth, or tenth test
`@pytest.mark.smoke` and quietly eating into the per-PR time budget one
addition at a time. Growing the smoke lane is a legitimate, deliberate
decision -- but it must be a conscious one, made by updating
`EXPECTED_SMOKE_TEST_COUNT` below in the same PR, not a side effect of
copy-pasting a marker.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

SERVICES_DIR = REPO_ROOT / "services"

# Pinned count of `@pytest.mark.smoke`-tagged tests across every service's
# test suite. If this test fails because the count went UP, that is not
# necessarily wrong -- but it means the per-PR smoke lane just got slower,
# so update this constant deliberately (and consider whether
# `timeout-minutes: 40` in `.github/workflows/e2e-smoke.yml` still holds)
# rather than bumping it reflexively to make the test pass.
EXPECTED_SMOKE_TEST_COUNT = 6

_SMOKE_MARKER = re.compile(r"^@pytest\.mark\.smoke$", re.MULTILINE)


def _count_smoke_markers() -> int:
    total = 0
    for path in sorted(SERVICES_DIR.glob("*/tests/**/*.py")):
        total += len(_SMOKE_MARKER.findall(path.read_text()))
    return total


def test_smoke_lane_size_is_pinned() -> None:
    """Fail loudly, and actionably, if the smoke lane's size drifts."""
    actual = _count_smoke_markers()
    assert actual == EXPECTED_SMOKE_TEST_COUNT, (
        f"found {actual} tests tagged `@pytest.mark.smoke` across "
        f"services/*/tests/, expected {EXPECTED_SMOKE_TEST_COUNT}. The "
        "`E2E smoke` merge gate (.github/workflows/e2e-smoke.yml) runs "
        "every one of these against a live Compose stack inside a "
        "40-minute budget on every PR, so growing this count is a "
        "deliberate decision: if you just added or removed a "
        "`@pytest.mark.smoke` test on purpose, update "
        "EXPECTED_SMOKE_TEST_COUNT in this file to match; if you did not, "
        "find the drift with `grep -rn '@pytest.mark.smoke' services/*/tests/`"
    )
