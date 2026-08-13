"""Repo-level guard: the retrieval-eval gate step must run BEFORE the other
compose tests inside `integration.yml`'s `compose-integration` job.

Why this ordering is load-bearing, not cosmetic: the retrieval-eval hard gate
(`tests/evals/test_compose_retrieval_regression.py`) scores a golden corpus
by rank against the live stack's search index. It shares that index with
every other compose test in the same job -- documents uploaded by
lifecycle/tenancy/MCP/benchmark tests crowd the golden corpus's expected
documents out of top-5, which degrades recall/ndcg/mrr and can fail the gate
on pollution that has nothing to do with an actual ranking regression (see
the tolerance work in #236, and the fresh-stack verification in
`.superpowers/sdd/2026-08-12-e2e-merge-gates/task-12-fresh-verification.md`,
which reproduced exactly this failure mode by running the lanes in the
reverse order).

`integration.yml` currently protects against this only by convention: the
"Run retrieval-eval hard gate" step (`-m 'eval_gate and compose'`) is placed
textually before "Run remaining public-api compose tests"
(`-m 'compose and not eval_gate'`), so the gate always sees the golden corpus
before any polluting document lands in it. Nothing pins that ordering --  a
future edit could silently swap the two steps and reintroduce this
flakiness with no other test catching it. This test is that pin.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

WORKFLOW = REPO_ROOT / ".github" / "workflows" / "integration.yml"

# Exact marker-expression strings as they appear in integration.yml's
# `compose-integration` job (single-quoted `-m` args). Matched literally
# rather than re-derived from a marker-expression parser, so this test fails
# loudly on either a marker-text edit or a step-order edit -- either is worth
# a human look.
EVAL_GATE_STEP_MARKER = "-m 'eval_gate and compose'"
REMAINING_COMPOSE_STEP_MARKER = "-m 'compose and not eval_gate'"


def test_eval_gate_step_runs_before_remaining_compose_tests() -> None:
    """Pin eval-gate-first step ordering inside `compose-integration`.

    The eval gate must observe the golden corpus before the other compose
    tests upload anything alongside it. If this test fails, the fix is to
    restore the "Run retrieval-eval hard gate" step to before "Run remaining
    public-api compose tests" in `.github/workflows/integration.yml` --
    do NOT edit this test to match a reordered workflow; the ordering itself
    is the thing under test.
    """
    text = WORKFLOW.read_text()

    eval_gate_offset = text.find(EVAL_GATE_STEP_MARKER)
    remaining_compose_offset = text.find(REMAINING_COMPOSE_STEP_MARKER)

    assert eval_gate_offset != -1, (
        f"could not find the eval-gate step's marker expression ({EVAL_GATE_STEP_MARKER!r}) "
        "in integration.yml -- has the pytest -m expression changed? Update this test's "
        "EVAL_GATE_STEP_MARKER to match, and re-verify the ordering assertion below still "
        "reflects the real steps."
    )
    assert remaining_compose_offset != -1, (
        "could not find the remaining-compose-tests step's marker expression "
        f"({REMAINING_COMPOSE_STEP_MARKER!r}) in integration.yml -- has the pytest -m "
        "expression changed? Update this test's REMAINING_COMPOSE_STEP_MARKER to match, "
        "and re-verify the ordering assertion below still reflects the real steps."
    )

    assert eval_gate_offset < remaining_compose_offset, (
        "the 'Run retrieval-eval hard gate' step (-m 'eval_gate and compose') must appear "
        "BEFORE the 'Run remaining public-api compose tests' step "
        "(-m 'compose and not eval_gate') in .github/workflows/integration.yml. Running the "
        "other compose tests first pollutes the shared workspace with documents that crowd "
        "the golden corpus out of top-5, degrading recall/ndcg/mrr and producing a false "
        "retrieval-eval regression (see #236, and the fresh-stack verification in "
        ".superpowers/sdd/2026-08-12-e2e-merge-gates/task-12-fresh-verification.md). "
        "Fix this by restoring eval-gate-first step ordering in the workflow -- do not "
        "change this test."
    )
