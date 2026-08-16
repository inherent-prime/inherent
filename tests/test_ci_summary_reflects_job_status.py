"""Repo-level guard: a job summary step must never claim success unconditionally.

Bug (#205): `ci.yml`'s `service-checks` job ends with

    - name: Report check summary
      if: always()
      run: echo "### ${{ matrix.service }} checks passed :white_check_mark:" >> "$GITHUB_STEP_SUMMARY"

`if: always()` makes this step run even when an earlier step (Lint, Test, ...)
already failed the job -- but the `run:` body unconditionally echoes a
"checks passed :white_check_mark:" line with no branch on the job's actual
outcome. A red `service-checks` job therefore still prints a green summary,
which is actively misleading to anyone reading the Actions summary instead of
digging into which step failed. The identical pattern was copied verbatim
into `root-tests` (added for #183) -- exactly the kind of copy-paste this
suite is written to catch generically, in every workflow, not just the two
known offenders.

These tests read the raw YAML text rather than parsing it, matching the house
style in `tests/test_ci_schema_fidelity.py` / `tests/test_integration_workflow_guards.py`
/ `tests/test_conventions_workflow_guards.py`: the root suite has no project
of its own to declare a YAML-parsing dependency for these lookups.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]

WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"

# A step's `run:` body is a "success claim" if it says (a form of) "checks
# passed", or plants the green-checkmark emoji/shortcode that reads as one at
# a glance in the rendered summary.
SUCCESS_CLAIM_RE = re.compile(r"checks?\s+passed|:white_check_mark:|✅", re.IGNORECASE)

# A step's `run:` body counts as "branching on failure" (i.e. it is capable of
# emitting a truthful failure-path message) if it contains any of these. This
# is intentionally broad: it must recognize both a single self-branching step
# (an `if/else` inside one `run:` block, e.g. keyed off `${{ job.status }}`)
# and a dedicated sibling step gated on `failure()`.
FAILURE_BRANCH_RE = re.compile(
    r"exit 1|::error::|\bfailure\b|\bfailed\b|:x:|❌", re.IGNORECASE
)

JOB_HEADER_RE = re.compile(r"^  ([a-zA-Z0-9_-]+):\s*$", re.MULTILINE)
STEP_HEADER_RE = re.compile(r"^      - name: (.+)$", re.MULTILINE)
IF_RE = re.compile(r"^\s*if: (.+)$", re.MULTILINE)


def _all_workflows() -> list[Path]:
    paths = sorted(WORKFLOWS_DIR.glob("*.yml"))
    assert paths, f"expected at least one workflow under {WORKFLOWS_DIR}"
    return paths


class Step:
    """One `- name: ...` step block, sliced from the raw workflow text."""

    def __init__(self, name: str, block: str) -> None:
        self.name = name
        self.block = block
        # The `if:` condition lives in the step's header, before its `run:`
        # (or `uses:`) key -- slicing there avoids matching an `if:` that
        # happens to appear inside a *later* step's multi-line `run:` body.
        header_end = block.index("\n        run:") if "\n        run:" in block else len(block)
        header = block[:header_end]
        cond_match = IF_RE.search(header)
        self.condition = cond_match.group(1).strip() if cond_match else ""
        self.run_body = block[header_end:]

    def claims_success(self) -> bool:
        return bool(SUCCESS_CLAIM_RE.search(self.run_body))

    def branches_on_failure(self) -> bool:
        return bool(FAILURE_BRANCH_RE.search(self.run_body))


def _job_blocks(text: str) -> dict[str, str]:
    """Split a workflow's raw text into ``{job_id: job_block}``.

    Job ids are the two-space-indented keys directly under `jobs:` (e.g.
    `  service-checks:`). Each block runs from that header to the next job
    header (same indentation) or end of file -- matching the `_job_block`
    helper already used in `tests/test_conventions_workflow_guards.py`.
    """
    jobs_idx = text.index("\njobs:")
    body = text[jobs_idx + len("\njobs:") :]

    headers = list(JOB_HEADER_RE.finditer(body))
    blocks: dict[str, str] = {}
    for i, m in enumerate(headers):
        start = m.start()
        end = headers[i + 1].start() if i + 1 < len(headers) else len(body)
        blocks[m.group(1)] = body[start:end]
    return blocks


def _steps(job_block: str) -> list[Step]:
    headers = list(STEP_HEADER_RE.finditer(job_block))
    steps: list[Step] = []
    for i, m in enumerate(headers):
        start = m.start()
        end = headers[i + 1].start() if i + 1 < len(headers) else len(job_block)
        steps.append(Step(name=m.group(1).strip(), block=job_block[start:end]))
    return steps


def _iter_jobs() -> list[tuple[Path, str, list[Step]]]:
    """Yield ``(workflow_path, job_id, steps)`` for every job in every workflow."""
    out: list[tuple[Path, str, list[Step]]] = []
    for path in _all_workflows():
        text = path.read_text()
        if "\njobs:" not in text:
            continue
        for job_id, block in _job_blocks(text).items():
            out.append((path, job_id, _steps(block)))
    return out


ALL_JOBS = _iter_jobs()
JOB_IDS = [f"{path.name}::{job_id}" for path, job_id, _ in ALL_JOBS]


@pytest.mark.parametrize("workflow_path,job_id,steps", ALL_JOBS, ids=JOB_IDS)
def test_success_claiming_steps_cannot_fire_after_a_failed_step(
    workflow_path: Path, job_id: str, steps: list[Step]
) -> None:
    """A step that claims success must not be reachable once the job is red.

    Two shapes are acceptable:

    1. The step's own `run:` body branches on the job's outcome (e.g. an
       `if`/`else` keyed off `${{ job.status }}` or `${{ steps.*.outcome }}`)
       and therefore emits a *different*, truthful message once something
       upstream failed -- `if: always()` is fine here because the body
       itself is honest either way.
    2. The step is gated on `success()` (default `if:`-less steps are
       implicitly `success()` too) so it simply never runs once a prior step
       has failed, AND some other step in the same job is gated on
       `failure()`/`always()` and emits a truthful failure-path message --
       so the job as a whole still tells the truth when it's red.

    `if: always()` with a body that unconditionally prints a success claim
    and no failure branch satisfies neither shape: it is the #205 bug, and
    this test is written to catch it under any job name / any workflow, not
    just the two `service-checks` / `root-tests` occurrences found in the
    initial sweep.
    """
    success_steps = [s for s in steps if s.claims_success()]
    if not success_steps:
        pytest.skip(f"{job_id} in {workflow_path.name} has no success-claiming step")

    for step in success_steps:
        if step.branches_on_failure():
            # Shape 1: the step is honest in both branches -- always() is safe.
            continue

        assert "always()" not in step.condition, (
            f"{workflow_path.name}:{job_id} step {step.name!r} runs under "
            f"`if: {step.condition}` and unconditionally claims success "
            "(matches a 'checks passed' / :white_check_mark: pattern) with "
            "no branch on the job's actual outcome -- this is the #205 bug "
            "shape: a failed earlier step still leaves this step printing a "
            "green summary. Either branch the message on job status (e.g. "
            "`${{ job.status }}`) or drop `always()` and add a `failure()` "
            "counterpart step."
        )

        # Shape 2: this step only runs on success (no always()); some other
        # step in the job must cover the failure path so the job is honest
        # when red.
        has_failure_path = any(
            other is not step
            and ("failure()" in other.condition or "always()" in other.condition)
            and other.branches_on_failure()
            for other in steps
        )
        assert has_failure_path, (
            f"{workflow_path.name}:{job_id} step {step.name!r} claims success "
            "but the job has no failure()/always()-gated step that emits a "
            "truthful failure-path message -- a red job silently produces no "
            "summary at all instead of saying it failed."
        )


def test_ci_service_checks_summary_names_the_failed_service() -> None:
    """Pin the concrete #205 fix in `ci.yml`'s `service-checks` job.

    The generic sweep above proves *some* branch exists; this pins that the
    failure branch is actually truthful -- it must name the matrix service
    and say a check failed, not just avoid the literal success string.
    """
    text = (WORKFLOWS_DIR / "ci.yml").read_text()
    job = _job_blocks(text)["service-checks"]
    steps = {s.name: s for s in _steps(job)}

    step = steps["Report check summary"]
    assert step.branches_on_failure(), (
        "service-checks 'Report check summary' must branch on job status so "
        "a failed job doesn't still print a success line"
    )
    assert "matrix.service" in step.run_body, (
        "the failure branch must still name the matrix service, matching the "
        "existing success line's convention"
    )


def test_ci_root_tests_summary_names_the_failure() -> None:
    """Pin the concrete #205 fix in `ci.yml`'s `root-tests` job (the #183 copy)."""
    text = (WORKFLOWS_DIR / "ci.yml").read_text()
    job = _job_blocks(text)["root-tests"]
    steps = {s.name: s for s in _steps(job)}

    step = steps["Report check summary"]
    assert step.branches_on_failure(), (
        "root-tests 'Report check summary' must branch on job status so a "
        "failed job doesn't still print a success line"
    )
