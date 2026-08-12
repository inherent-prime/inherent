"""Repo-level guard: the `E2E smoke` merge-gate workflow shape.

`.github/workflows/e2e-smoke.yml` is the PR-blocking end-to-end lane. Unlike
`integration.yml` — which boots the same stack but runs the full compose suite
plus the eval ratchet, and is deliberately kept off pull requests because it is
too slow to gate merge — this workflow runs only the `smoke`-tagged subset, so
it can stay inside a merge-gate time budget while still proving the real stack
boots and a document survives the ingest-to-search round trip.

Four properties make it usable as a required status check, and each is easy to
break silently:

- the job's display `name:` is the required-check context registered on `main`;
- the test selection is `-m "smoke and compose"` and must NOT drag in the
  `eval_gate` marker, whose ranking-regression baseline belongs to the nightly
  ratchet lane (`integration.yml`) and would make this gate fail a PR for a
  metric drift that has nothing to do with the PR;
- `timeout-minutes` bounds a wedged run so a required check cannot hang
  pending for the runner's 6-hour default;
- a `concurrency` group with `cancel-in-progress: true` frees runners when a
  PR is pushed again, since only the newest commit's result gates merge.

These tests pin the YAML text rather than executing the workflow (this repo has
no local GitHub Actions runner), matching the pattern in
`tests/test_integration_workflow_guards.py` and
`tests/test_conventions_workflow_guards.py`.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

WORKFLOW = REPO_ROOT / ".github" / "workflows" / "e2e-smoke.yml"

# The exact selection expression the smoke lane runs. Pinned as one string
# because both halves matter: `smoke` alone would also pick up offline tests
# tagged for the lane, and `compose` alone is the full (slow) lane.
SELECTION = '-m "smoke and compose"'

SERVICE_PYPROJECTS = (
    REPO_ROOT / "services" / "inh-public-api-svc" / "pyproject.toml",
    REPO_ROOT / "services" / "inh-ingestion-svc" / "pyproject.toml",
)


def _text() -> str:
    assert WORKFLOW.exists(), f"expected workflow at {WORKFLOW}"
    return WORKFLOW.read_text()


def _job_block(job: str, text: str) -> str:
    start = text.index(f"  {job}:")
    nxt = re.search(r"\n  [a-z][a-z0-9-]*:\n", text[start + 1 :])
    return text[start : start + 1 + nxt.start()] if nxt else text[start:]


def test_workflow_file_exists() -> None:
    assert WORKFLOW.exists(), f"expected workflow at {WORKFLOW}"


def test_triggers_on_pull_request() -> None:
    """A merge gate that does not run on PRs never reports a check context."""
    text = _text()
    trigger = re.search(r"^on:\n(?:.*\n)*?  pull_request:", text, re.MULTILINE)
    assert trigger is not None, (
        "expected a `pull_request:` trigger — without it this workflow never "
        "reports a status on a PR and the required check hangs pending"
    )


def test_job_name_is_exactly_e2e_smoke() -> None:
    """The job's display `name:` is the required-check context downstream.

    Branch protection registers the context by display name, so a drift
    between the job id and its `name:` leaves the registered context
    permanently pending.
    """
    text = _text()
    job = _job_block("e2e-smoke", text)
    name = re.search(r"^    name: (.+)$", job, re.MULTILINE)
    assert name is not None, "job `e2e-smoke` has no `name:`"
    assert (
        name.group(1).strip() == "E2E smoke"
    ), f"job name must be exactly `E2E smoke`, found: {name.group(1)!r}"


def test_runs_the_smoke_and_compose_selection() -> None:
    """Both marker halves must be in the selection expression."""
    text = _text()
    assert SELECTION in text, (
        f"expected the smoke selection `{SELECTION}` in the workflow; the "
        "lane is defined by that exact expression (Tasks 4-6 tag tests with "
        "`@pytest.mark.smoke` on the strength of it)"
    )


def test_selection_does_not_pull_in_the_eval_gate_marker() -> None:
    """The ranking-regression gate belongs to the nightly lane, not this one.

    `eval_gate` compares per-mode retrieval metrics against a committed
    baseline that only `integration.yml` ratchets. Selecting it here would
    block merge on a metric drift unrelated to the PR under test, and would
    do it on a CPU-embedding runner whose numbers are noisier than the
    baseline's tolerance assumes.
    """
    text = _text()
    assert "eval_gate" not in text, (
        "the smoke lane must not select the `eval_gate` marker — that gate's "
        "baseline is ratcheted by integration.yml on `main` and is not a "
        "per-PR signal"
    )


def test_timeout_minutes_is_forty() -> None:
    """A wedged gate must not hold a required check pending for 6 hours."""
    text = _text()
    job = _job_block("e2e-smoke", text)
    timeout = re.search(r"^    timeout-minutes: (\d+)$", job, re.MULTILINE)
    assert timeout is not None, "job `e2e-smoke` has no `timeout-minutes:`"
    assert (
        timeout.group(1) == "40"
    ), f"expected `timeout-minutes: 40`, found {timeout.group(1)}"


def test_concurrency_cancels_superseded_runs() -> None:
    """Only the newest commit on a PR gates merge, so cancel the rest.

    Without `cancel-in-progress: true`, every push to a PR leaves a
    40-minute compose job running for a commit no one will merge.
    """
    text = _text()
    concurrency = re.search(
        r"^concurrency:\n  group: (.+)\n  cancel-in-progress: (.+)$",
        text,
        re.MULTILINE,
    )
    assert concurrency is not None, (
        "expected a top-level `concurrency:` block with `group:` and "
        "`cancel-in-progress:`"
    )
    assert concurrency.group(2).strip() == "true", (
        "smoke runs must be cancellable: only the newest commit's result "
        f"gates merge. Found cancel-in-progress: {concurrency.group(2)!r}"
    )


def test_boots_the_stack_and_waits_for_health() -> None:
    """The lane is only meaningful against a fully booted stack.

    Both halves of the wait are pinned, because they cover different gaps
    and the copied-from-`integration.yml` shape keeps both:

    - `--wait` makes `docker compose up` block until every service's own
      healthcheck passes;
    - the curl poll then waits on the public API's `/health` specifically,
      which is the endpoint the tests actually talk to.

    Without them the tests race the boot and fail as connection errors
    rather than as real regressions -- the worst failure mode for a merge
    gate, since it reads as a flaky block rather than a bug.
    """
    text = _text()
    boot = re.search(r"^\s*run: docker compose up (.+)$", text, re.MULTILINE)
    assert boot is not None, "expected a `docker compose up` step that boots the stack"
    assert "--wait" in boot.group(1), (
        "`docker compose up` must pass `--wait` so the tests do not race the "
        f"stack boot. Found: docker compose up {boot.group(1)}"
    )

    assert re.search(r"curl -fsS http://localhost:18000/health", text), (
        "expected the health-wait step to poll the public API's /health "
        "endpoint with `curl -fsS http://localhost:18000/health` (copied "
        "from integration.yml's compose-integration job)"
    )


def test_tears_the_stack_down_unconditionally() -> None:
    """A failed run must not leave volumes behind for the next job.

    The `if:` is the whole point and is checked as part of the same match,
    not separately: `docker compose down -v` under `if: success()` would
    tear down only on the happy path, which is precisely backwards -- the
    runs that leave dirty volumes behind are the failing ones. Asserting
    only that the command appears somewhere would stay green through that
    edit, so the condition is anchored to the line immediately above the
    `run:` it guards.
    """
    text = _text()
    teardown = re.search(
        r"^\s*if: (.+)\n\s*run: docker compose down -v$", text, re.MULTILINE
    )
    assert teardown is not None, (
        "expected a `docker compose down -v` teardown step whose `if:` is on "
        "the line directly above its `run:` -- either the step is missing, or "
        "it carries no `if:` at all (which would skip teardown on failure)"
    )
    assert teardown.group(1).strip() == "always()", (
        "the teardown step must be `if: always()`; a failed smoke run is "
        "exactly the case that must not leave volumes behind. Found: "
        f"if: {teardown.group(1)!r}"
    )


@pytest.mark.parametrize("pyproject", SERVICE_PYPROJECTS, ids=lambda p: p.parent.name)
def test_service_declares_the_smoke_marker(pyproject: Path) -> None:
    """Both services must declare `smoke`, even before both use it.

    The workflow runs the same selection in each service directory, and
    `--strict-markers` (or a `filterwarnings = error` config) turns an
    undeclared marker into a collection error rather than an empty
    selection — so the declaration has to land in both, not just in the
    service that happens to carry the first tagged test.
    """
    text = pyproject.read_text()
    assert re.search(r'^\s*"smoke: .+",$', text, re.MULTILINE), (
        f"{pyproject} must declare a `smoke: ...` marker in its pytest "
        "`markers = [...]` list"
    )
