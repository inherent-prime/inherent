"""Repo-level guard: the `Conventions` merge-gate workflow shape.

`.github/workflows/conventions.yml` is a required-status-check workflow (its
single job is named exactly `Conventions`, which becomes a required check
context on `main`). Two behaviors must hold for it to do its job as a merge
gate without becoming a source of false-positive blocks:

- it must trigger on label changes too (`labeled`/`unlabeled`), not just
  code pushes, so applying a skip label re-evaluates the gate without a new
  commit;
- each gate must be skippable via its own label (`no-changelog`,
  `no-docs-needed`) so an intentionally-exempt PR is not stuck.

Most of these tests pin the YAML text rather than executing the workflow
(this repo has no local GitHub Actions runner), matching the pattern in
`tests/test_integration_workflow_guards.py`. The gate *scripts* themselves
are pure bash over a `changed.txt` file list, though, so those are extracted
and actually executed against synthetic file lists -- pinning the text of a
`grep` pattern proves the pattern is spelled a certain way, not that it
classifies a real PR's diff correctly.
"""

from __future__ import annotations

import re
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

WORKFLOW = REPO_ROOT / ".github" / "workflows" / "conventions.yml"

# The exact file set the automated retrieval-eval ratchet job commits
# (`eval-baseline-ratchet` in `.github/workflows/integration.yml`).
RATCHET_FILES = [
    "services/inh-public-api-svc/tests/evals/corpus/retrieval_baseline.json",
    "services/inh-public-api-svc/tests/evals/corpus/retrieval_history.jsonl",
    "README.md",
    "docs/_generated/retrieval-baseline.md",
]


def _text() -> str:
    assert WORKFLOW.exists(), f"expected workflow at {WORKFLOW}"
    return WORKFLOW.read_text()


def _step_script(step_name: str) -> str:
    """Extract the `run: |` block of the named step as runnable bash.

    The block runs verbatim under `bash -e` in `_run_gate` below, so the
    workflow's real gate logic is what gets exercised -- not a paraphrase of
    it maintained separately in this test file.
    """
    text = _text()
    idx = text.index(f"- name: {step_name}")
    run_idx = text.index("run: |", idx)
    body = text[run_idx + len("run: |") :].lstrip("\n")

    lines: list[str] = []
    for line in body.splitlines():
        # The block ends at the first non-blank line that is not indented
        # deeper than the step's own `- name:` key.
        if line.strip() and not line.startswith("          "):
            break
        lines.append(line)
    script = textwrap.dedent("\n".join(lines))
    assert script.strip(), f"step {step_name!r} has an empty `run:` block"
    return script


def _run_gate(step_name: str, changed: list[str], tmp_path: Path) -> int:
    """Run a gate step against a synthetic `changed.txt`, return its exit code.

    Exit 0 means the gate passed (PR allowed); non-zero means it blocked.
    `bash -e` matches the default shell GitHub Actions runs `run:` blocks
    with (`/usr/bin/bash -e {0}`).
    """
    (tmp_path / "changed.txt").write_text("".join(f"{p}\n" for p in changed))
    script = tmp_path / "gate.sh"
    script.write_text(_step_script(step_name))
    proc = subprocess.run(
        ["bash", "-e", str(script)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    return proc.returncode


def _job_block(job: str, text: str) -> str:
    start = text.index(f"  {job}:")
    nxt = re.search(r"\n  [a-z][a-z0-9-]*:\n", text[start + 1 :])
    return text[start : start + 1 + nxt.start()] if nxt else text[start:]


def test_workflow_file_exists() -> None:
    assert WORKFLOW.exists(), f"expected workflow at {WORKFLOW}"


def test_pull_request_trigger_includes_label_events() -> None:
    """The gate must re-evaluate when a skip label is applied or removed.

    Without `labeled`/`unlabeled` in the trigger types, applying
    `no-changelog` to an already-failing PR would not re-run the check, and
    it would stay red until an unrelated push.
    """
    text = _text()
    trigger = re.search(
        r"^on:\n  pull_request:\n    types: \[(.+)\]$", text, re.MULTILINE
    )
    assert trigger is not None, "expected a `pull_request: types: [...]` trigger"

    types = [t.strip() for t in trigger.group(1).split(",")]
    for expected in ("opened", "synchronize", "reopened", "labeled", "unlabeled"):
        assert expected in types, (
            f"pull_request trigger missing `{expected}`; found types={types}"
        )


def test_job_name_is_exactly_conventions() -> None:
    """The job's display `name:` is the required-check context downstream.

    Task 10 registers `Conventions` as a required status check on `main`; if
    the job's `name:` drifts from the job id, the registered context would
    never report, and the branch protection rule would hang pending forever.
    """
    text = _text()
    job = _job_block("conventions", text)
    name = re.search(r"^    name: (.+)$", job, re.MULTILINE)
    assert name is not None, "job `conventions` has no `name:`"
    assert name.group(1).strip() == "Conventions", (
        f"job name must be exactly `Conventions`, found: {name.group(1)!r}"
    )


def test_checkout_uses_full_history() -> None:
    """The diff step needs the base branch's history, not a shallow clone.

    `git diff origin/<base>...HEAD` fails on a `fetch-depth: 1` checkout
    because the merge-base commit is not present locally.
    """
    text = _text()
    assert re.search(r"fetch-depth:\s*0", text), (
        "checkout step must use `fetch-depth: 0` so the base-branch diff has "
        "a merge-base to compare against"
    )


def test_timeout_minutes_present() -> None:
    """A wedged gate step must not hold a required check pending forever."""
    text = _text()
    job = _job_block("conventions", text)
    assert re.search(r"^    timeout-minutes: \d+$", job, re.MULTILINE), (
        "job `conventions` has no `timeout-minutes:`"
    )


def test_changelog_gate_checks_no_changelog_label() -> None:
    """The CHANGELOG gate must be skippable via the `no-changelog` label."""
    text = _text()
    idx = text.index("CHANGELOG gate")
    step = text[idx : idx + 600]
    assert "no-changelog" in step, (
        "CHANGELOG gate step must reference the `no-changelog` skip label"
    )


def test_changelog_gate_blocks_service_change_without_changelog(
    tmp_path: Path,
) -> None:
    """The gate's whole point: a service source edit needs a CHANGELOG entry."""
    assert (
        _run_gate(
            "CHANGELOG gate",
            ["services/inh-public-api-svc/src/api/v1/search.py"],
            tmp_path,
        )
        != 0
    ), "expected the CHANGELOG gate to block a services/ change with no CHANGELOG.md"


def test_changelog_gate_allows_service_change_with_changelog(
    tmp_path: Path,
) -> None:
    """A service edit that ships a CHANGELOG entry passes."""
    assert (
        _run_gate(
            "CHANGELOG gate",
            ["services/inh-public-api-svc/src/api/v1/search.py", "CHANGELOG.md"],
            tmp_path,
        )
        == 0
    ), "expected the CHANGELOG gate to pass when CHANGELOG.md is in the diff"


def test_changelog_gate_allows_automated_eval_ratchet_diff(tmp_path: Path) -> None:
    """The automated baseline-ratchet PR must not need a CHANGELOG entry.

    `eval-baseline-ratchet` (in `.github/workflows/integration.yml`) opens a
    PR touching only the two machine-generated eval corpus files plus the
    README table and docs snippet rendered from them, then enables auto-merge on it. Those
    files live under `services/`, so an unqualified `^services/` gate blocks
    that PR forever: nothing merges, the committed baseline stays frozen, and
    the retrieval-eval floor silently stops rising -- the exact inert-gate
    failure that job exists to prevent. `github-actions[bot]` cannot be
    relied on to self-apply a `no-changelog` label (the fallback
    `GITHUB_TOKEN` path has no such guarantee), so the exemption has to live
    in the gate itself.
    """
    assert _run_gate("CHANGELOG gate", RATCHET_FILES, tmp_path) == 0, (
        "expected the CHANGELOG gate to pass for the automated ratchet diff "
        f"({RATCHET_FILES}); these are machine-generated eval artifacts, not "
        "user-facing changes"
    )


def test_changelog_gate_still_blocks_ratchet_files_bundled_with_source(
    tmp_path: Path,
) -> None:
    """The exemption must not become a loophole for real service changes.

    A PR that edits service source and happens to also touch the eval corpus
    is a normal change and still owes a CHANGELOG entry.
    """
    changed = [*RATCHET_FILES, "services/inh-public-api-svc/src/api/v1/search.py"]
    assert _run_gate("CHANGELOG gate", changed, tmp_path) != 0, (
        "exempting the eval corpus must not exempt service source changed "
        "alongside it"
    )


def test_docs_gate_checks_no_docs_needed_label() -> None:
    """The docs-sync gate must be skippable via the `no-docs-needed` label."""
    text = _text()
    idx = text.index("Docs-sync gate")
    step = text[idx : idx + 600]
    assert "no-docs-needed" in step, (
        "docs-sync gate step must reference the `no-docs-needed` skip label"
    )


def test_docs_gate_path_patterns_cover_api_mcp_and_contracts() -> None:
    """Pin the exact path fragments the docs-sync gate triggers on.

    A typo in any of these (e.g. `mcp_server/server.py` -> `server_py`, or a
    missing service directory) would silently narrow the gate to matching
    nothing, and no other test would catch it.
    """
    text = _text()
    idx = text.index("Docs-sync gate")
    step = text[idx : idx + 600]

    for fragment in (
        "services/inh-public-api-svc/src/",
        "api/v1/",
        r"mcp_server/server\.py",
        "services/inh-contracts/src/",
    ):
        assert fragment in step, (
            f"docs-sync gate must match changed paths under `{fragment}`, "
            "not found in the step's grep pattern"
        )


def test_base_ref_not_interpolated_directly_into_shell() -> None:
    """`github.base_ref` must reach the shell via env, not template expansion.

    Interpolating `${{ github.base_ref }}` directly into a `run:` block
    splices attacker-influenceable text into the shell command before the
    shell ever runs; GitHub's hardening guidance is to pass such values
    through `env:` and reference them as a shell variable (`"$BASE_REF"`)
    instead, so the value is data, never script text.
    """
    text = _text()
    idx = text.index("Collect changed files")
    step = text[idx : idx + 700]

    assert re.search(r"BASE_REF:\s*\$\{\{\s*github\.base_ref\s*\}\}", step), (
        "expected an `env: BASE_REF: ${{ github.base_ref }}` on the "
        "'Collect changed files' step"
    )

    run_idx = step.index("run: |")
    run_block = step[run_idx:]
    assert "${{ github.base_ref }}" not in run_block, (
        "`github.base_ref` must not be template-interpolated directly into "
        "the `run:` script itself; pass it via the step's `env:` and "
        'reference it as `"$BASE_REF"` instead'
    )
    assert '"$BASE_REF"' in run_block, (
        "expected the shell script to reference the base ref as "
        '`"$BASE_REF"`, not a re-interpolated template expression'
    )


@pytest.mark.parametrize(
    "gate_label",
    ["no-changelog", "no-docs-needed"],
)
def test_gate_labels_use_exact_array_membership(gate_label: str) -> None:
    """Each gate's `if:` condition must test exact label membership.

    `contains(join(labels.*.name, ','), 'no-changelog')` is SUBSTRING
    matching over the joined string, so a label like `no-changelog-needed`
    or `definitely-no-changelog` would also match and wrongly skip a
    required gate. `contains(array, item)` is exact element membership in
    GitHub Actions expression syntax, so the array form (no `join`) must be
    used instead.
    """
    text = _text()
    assert "github.event.pull_request.labels.*.name" in text, (
        "expected the workflow to inspect "
        "`github.event.pull_request.labels.*.name`"
    )
    assert "join(github.event.pull_request.labels" not in text, (
        "gate conditions must not join the label list into a string before "
        "calling `contains()` -- that degrades exact membership into "
        "substring matching, so `no-changelog-needed` would also match "
        "`no-changelog`"
    )
    assert (
        f"contains(github.event.pull_request.labels.*.name, '{gate_label}')"
        in text
    ), (
        f"expected an exact array-membership check for `{gate_label}`, e.g. "
        f"contains(github.event.pull_request.labels.*.name, '{gate_label}')"
    )
