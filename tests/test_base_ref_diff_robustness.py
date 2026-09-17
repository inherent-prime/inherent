"""Repo-level guard: how the PR gates compute their changed-file list.

Three workflows open with a step that writes `changed.txt` and then gate on
it -- `ci.yml` (skips `service-checks`/`root-tests` on a docs-only PR),
`e2e-smoke.yml` (same filter, kept in sync) and `conventions.yml` (the
CHANGELOG/docs gates). Every one of those decisions is only as trustworthy as
that file list, and a wrong list fails *silently*: the gate goes green having
looked at the wrong diff.

The original form was `git fetch origin "$BASE_REF"` followed by
`git diff --name-only "origin/$BASE_REF...HEAD"`, which has two problems:

- `git fetch origin <branch>` is only guaranteed to move `FETCH_HEAD`. It
  does not guarantee `refs/remotes/origin/<branch>` exists or is current, so
  the diff either aborts on a missing ref or silently compares against a
  stale one;
- it resolves the base branch through whatever the checkout action left in
  the `origin` remote, rather than naming the repository it means.

Both are fixed by fetching `${GITHUB_SERVER_URL}/${GITHUB_REPOSITORY}` --
which on a `pull_request` event is always the BASE repo, for fork and
non-fork PRs alike -- and diffing `FETCH_HEAD...HEAD`.

These tests pin the YAML text rather than executing the workflow (this repo
has no local GitHub Actions runner), matching the pattern in
`tests/test_conventions_workflow_guards.py` and
`tests/test_e2e_smoke_workflow_guards.py`.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# (workflow file, name of the step that writes `changed.txt`).
GATE_STEPS = [
    ("ci.yml", "Detect changed files"),
    ("e2e-smoke.yml", "Detect changed files"),
    ("conventions.yml", "Collect changed files"),
]


def _run_block(workflow: str, step_name: str) -> str:
    """Return the `run: |` body of the named step, comments stripped.

    Comments are dropped so an assertion cannot be satisfied by prose that
    merely *describes* the right command in the block above it.
    """
    path = REPO_ROOT / ".github" / "workflows" / workflow
    assert path.exists(), f"expected workflow at {path}"
    text = path.read_text()

    idx = text.index(f"- name: {step_name}")
    run_idx = text.index("run: |", idx)
    body = text[run_idx + len("run: |") :]

    lines: list[str] = []
    for line in body.splitlines()[1:]:
        # The block ends at the first line that is neither blank nor indented
        # deeper than the `run:` key itself.
        if line.strip() and not line.startswith(" " * 10):
            break
        if line.strip().startswith("#"):
            continue
        lines.append(line)
    return "\n".join(lines)


@pytest.mark.parametrize(("workflow", "step_name"), GATE_STEPS)
def test_diff_does_not_depend_on_a_remote_tracking_ref(workflow: str, step_name: str) -> None:
    """No gate may diff against `origin/<base>`.

    `git fetch origin <branch>` does not promise to create or update
    `refs/remotes/origin/<branch>`, so a diff naming it can abort or compare
    against a stale ref -- and a stale base silently produces the wrong
    changed-file list.
    """
    block = _run_block(workflow, step_name)
    assert "origin/" not in block, (
        f"{workflow}: the `{step_name}` step must not resolve the base branch "
        "through a remote-tracking ref (`origin/$BASE_REF`); diff against "
        "`FETCH_HEAD` instead"
    )


@pytest.mark.parametrize(("workflow", "step_name"), GATE_STEPS)
def test_base_ref_is_fetched_from_the_canonical_upstream(workflow: str, step_name: str) -> None:
    """The base branch is fetched from `github.repository`, not `origin`.

    On a `pull_request` event `GITHUB_REPOSITORY` is always the base
    repository -- never the contributor's fork -- so naming it directly makes
    the fetch correct for fork and non-fork PRs alike, independent of how the
    checkout action configured the `origin` remote.
    """
    block = _run_block(workflow, step_name)
    assert re.search(r"\$\{GITHUB_SERVER_URL\}/\$\{GITHUB_REPOSITORY\}\.git", block), (
        f"{workflow}: the `{step_name}` step must fetch the base branch from "
        "`${GITHUB_SERVER_URL}/${GITHUB_REPOSITORY}.git`"
    )
    assert re.search(r'git fetch "\$\{?\w+\}?" "\$BASE_REF"', block), (
        f'{workflow}: expected `git fetch "$UPSTREAM" "$BASE_REF"` in the ' f"`{step_name}` step"
    )


@pytest.mark.parametrize(("workflow", "step_name"), GATE_STEPS)
def test_diff_is_a_three_dot_range_from_fetch_head(workflow: str, step_name: str) -> None:
    """The list is the PR's own changes: `FETCH_HEAD...HEAD`, three dots.

    Two dots would also report commits that landed on the base branch since
    this PR forked, attributing other people's files to this PR and running
    (or blocking) gates on them.
    """
    block = _run_block(workflow, step_name)
    assert 'git diff --name-only "FETCH_HEAD...HEAD" > changed.txt' in block, (
        f"{workflow}: the `{step_name}` step must build `changed.txt` from "
        '`git diff --name-only "FETCH_HEAD...HEAD"`'
    )


@pytest.mark.parametrize(("workflow", "step_name"), GATE_STEPS)
def test_base_ref_reaches_the_shell_through_env(workflow: str, step_name: str) -> None:
    """`github.base_ref` is untrusted input: it must not be interpolated.

    A branch name is attacker-controlled on a fork PR. Passing it through
    `env:` makes it a shell variable rather than text spliced into the script
    body. `tests/test_conventions_workflow_guards.py` pins this for
    `conventions.yml`; the same reasoning binds every gate that reads it.
    """
    block = _run_block(workflow, step_name)
    assert "${{ github.base_ref }}" not in block, (
        f"{workflow}: `github.base_ref` must not be template-interpolated "
        f"into the `{step_name}` script; pass it via `env: BASE_REF:`"
    )
    assert '"$BASE_REF"' in block, (
        f"{workflow}: expected the `{step_name}` script to reference the base "
        'branch as `"$BASE_REF"`'
    )
