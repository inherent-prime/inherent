"""Repo-level guard: eval governance jobs run on `main` only.

`integration.yml` has three jobs, and only the first is a test in the usual
sense. The other two mutate state outside the run:

- `eval-baseline-ratchet` pushes a baseline commit and opens/auto-merges a PR
  against `main`;
- `eval-regression-alert` files or updates a GitHub issue.

Both are governance for `main`'s recorded eval scores. Neither is meaningful
on a feature branch: the workflow is `workflow_dispatch`-able on any ref, so
dispatching it to validate a branch would have the alert job file or update a
regression issue describing that branch's scores as if they were `main`'s.

These tests pin the ref guard on both jobs so a dispatch on a feature branch
runs `compose-integration` and nothing else.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]

WORKFLOW = REPO_ROOT / ".github" / "workflows" / "integration.yml"

# The jobs that write somewhere the run itself does not own -- git history and
# the PR list for the ratchet, the issue tracker for the alert.
GOVERNANCE_JOBS = ("eval-baseline-ratchet", "eval-regression-alert")


def _job_condition(job: str) -> str:
    """Return the `if:` expression guarding ``job``.

    Reads the raw YAML rather than parsing it: the `if:` line is a single
    top-level key inside the job block, and a regex keeps this suite free of a
    YAML dependency it would otherwise need for one lookup (the root suite has
    no project of its own to declare one in).
    """
    text = WORKFLOW.read_text()

    start = text.index(f"  {job}:")
    # The next line at job indentation (two spaces, non-space after) ends this
    # job's block; the last job runs to end-of-file.
    nxt = re.search(r"\n  [a-z][a-z0-9-]*:\n", text[start + 1 :])
    block = text[start : start + 1 + nxt.start()] if nxt else text[start:]

    condition = re.search(r"^    if: (.+)$", block, re.MULTILINE)
    assert condition is not None, f"job `{job}` has no `if:` guard at all"
    return condition.group(1)


@pytest.mark.parametrize("job", GOVERNANCE_JOBS)
def test_governance_job_is_guarded_to_main(job: str) -> None:
    """A job that mutates repo state must not run on a feature branch.

    `integration.yml` is `workflow_dispatch`-able on any ref (that is how a
    branch gets a real compose run before merge). Without this guard, such a
    dispatch would let the job act on a feature branch's results as though
    they were `main`'s.
    """
    condition = _job_condition(job)

    assert "github.ref == 'refs/heads/main'" in condition, (
        f"job `{job}` mutates state outside the run and must be guarded to "
        f"`github.ref == 'refs/heads/main'` so a workflow_dispatch on a feature "
        f"branch cannot trigger it. Found: {condition}"
    )


def test_compose_integration_is_not_ref_guarded() -> None:
    """The test job itself must stay dispatchable on any branch.

    This is the other half of the contract: guarding the governance jobs is
    only useful because `compose-integration` still runs everywhere, which is
    what makes a pre-merge `workflow_dispatch` on a feature branch worth doing.
    """
    text = WORKFLOW.read_text()
    start = text.index("  compose-integration:")
    nxt = re.search(r"\n  [a-z][a-z0-9-]*:\n", text[start + 1 :])
    block = text[start : start + 1 + nxt.start()] if nxt else text[start:]

    top_level_if = re.search(r"^    if: (.+)$", block, re.MULTILINE)
    assert top_level_if is None, (
        "`compose-integration` must remain runnable on any ref so a feature "
        f"branch can be validated before merge. Found guard: {top_level_if.group(1) if top_level_if else ''}"
    )


# Files the ratchet job commits. If any of these is missing from
# `paths-ignore`, merging a ratchet PR re-triggers integration.yml and
# recreates the unbounded main<->ratchet loop (#146/#158/#153).
RATCHET_OUTPUTS = (
    "services/inh-public-api-svc/tests/evals/corpus/retrieval_baseline.json",
    "services/inh-public-api-svc/tests/evals/corpus/retrieval_history.jsonl",
    "README.md",
    "docs/_generated/retrieval-baseline.md",
)


def test_ratchet_outputs_are_in_paths_ignore() -> None:
    """Every file the ratchet job commits must be in `on.push.paths-ignore`.

    `paths-ignore` skips the workflow only when *every* changed file matches,
    so omitting one ratchet output is enough to restart the loop.
    """
    text = WORKFLOW.read_text()
    for path in RATCHET_OUTPUTS:
        assert f'- "{path}"' in text, (
            f"ratchet output `{path}` is missing from integration.yml "
            "paths-ignore; merging a ratchet PR would re-trigger the "
            "unbounded main<->ratchet loop"
        )


def test_ratchet_job_regenerates_docs_snippet() -> None:
    """The ratchet must publish the docs snippet in the same step as README.md.

    Splitting the two outputs across jobs (or forgetting --docs-snippet) would
    let the MkDocs table freeze while the README table moved, which is the
    #153 failure mode this wiring exists to close.
    """
    text = WORKFLOW.read_text()
    assert "--docs-snippet" in text
    assert (
        'git add "$baseline" "$history" README.md docs/_generated/retrieval-baseline.md'
        in text
    )
