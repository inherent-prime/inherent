"""Repo-level guard: every compose-dependent Makefile target pre-flights the
stack before running tests (#209).

THE BUG: both Python services default `addopts` to `-m 'not compose'` as a
laptop-safety default. A command-line `-m` REPLACES that expression rather
than intersecting with it, so `make test-integration` (`pytest -m compose`)
selects exactly the tests the default was excluding -- and each one then
skips itself individually, at fixture setup, the moment it finds no
reachable stack. An all-skipped pytest run still exits 0, so
`make test-integration` on a laptop with no Docker running printed "N
skipped" and reported success having verified nothing.
`docs/maintainers/release_acceptance_matrix.md` treats it as a release
acceptance step -- the same defect class that let v0.6.0 ship believing it
had e2e coverage it never had (see CHANGELOG's Hetzner-e2e "Removed" entry).

THE FIX: `scripts/dev/require-stack.sh` probes the stack (reusing
`doctor.sh`'s own health checks) and exits non-zero with an actionable
message before pytest ever starts; `scripts/dev/run-compose-suite.sh` wraps
the pytest invocation itself and additionally asserts, from pytest's own
JUnit report, that at least one test actually EXECUTED (not just
collected-then-skipped) before treating the run as a pass.

This test pins the wiring so a future compose-dependent target added
without `require-stack` as a prerequisite fails CI immediately, instead of
silently reintroducing #209. It reads the raw Makefile text rather than a
real Make parser -- matching the house convention in
`test_ci_schema_fidelity.py` / `test_integration_workflow_guards.py`: the
root suite deliberately carries no project dependencies to pull one in for.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
MAKEFILE = REPO_ROOT / "Makefile"

# Targets that look like Makefile targets syntactically (`name:` at column 0)
# but aren't real build targets -- special directives / variable assignments
# that happen to be followed by a recipe-shaped block, or nothing at all.
NOT_A_TARGET = {".PHONY", ".DEFAULT_GOAL"}

_TARGET_HEADER_RE = re.compile(r"^([A-Za-z0-9_.-]+):\s*(.*)$")


def _iter_targets(text: str):
    """Yield ``(name, prereqs, recipe_body)`` for each real Makefile target.

    A target header is a line at column 0 shaped ``name: prereqs``; its
    recipe is every following tab-indented line, exactly how `make` itself
    delimits a recipe. Variable assignments (``FOO ?= bar``) don't match the
    header regex (the ``:`` isn't immediately after the name), so they don't
    need special-casing here.
    """
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        match = _TARGET_HEADER_RE.match(line)
        if match and not line.startswith("\t"):
            name = match.group(1)
            prereqs = match.group(2).split("#", 1)[0].split()
            i += 1
            body_lines = []
            while i < len(lines) and lines[i].startswith("\t"):
                body_lines.append(lines[i])
                i += 1
            yield name, prereqs, "\n".join(body_lines)
        else:
            i += 1


def _is_compose_dependent(body: str) -> bool:
    """A target is compose-dependent if its recipe runs compose-marked tests.

    Two shapes count:
    1. It delegates to ``scripts/dev/run-compose-suite.sh`` (the guarded
       wrapper every fixed target in this Makefile uses).
    2. It invokes ``pytest -m <expr>`` directly with an expression that
       selects ``compose`` as a *positive* term -- i.e. not part of a
       ``not compose`` exclusion (that's the safe, default-respecting
       direction and must NOT be flagged; see `test-fast` below).
    """
    if "run-compose-suite.sh" in body:
        return True

    for pytest_call in re.finditer(r"pytest\s+(.*)", body):
        rest = pytest_call.group(1)
        # `-m` takes either a quoted multi-token expression ("not compose
        # and not slow") or a single bare token (`-m compose`, no quotes
        # needed for one word) -- match both shapes.
        marker_match = re.search(r"-m\s+(?:['\"]([^'\"]+)['\"]|(\S+))", rest)
        if not marker_match:
            continue
        expr = marker_match.group(1) or marker_match.group(2)
        tokens = expr.split()
        for idx, token in enumerate(tokens):
            if token == "compose" and not (idx > 0 and tokens[idx - 1] == "not"):
                return True
    return False


def _targets() -> dict[str, tuple[list[str], str]]:
    text = MAKEFILE.read_text()
    return {
        name: (prereqs, body)
        for name, prereqs, body in _iter_targets(text)
        if name not in NOT_A_TARGET
    }


def test_makefile_has_at_least_one_compose_dependent_target() -> None:
    """Sanity check on the detection heuristic itself: if this ever finds
    zero compose-dependent targets, the heuristic broke silently (or
    test-integration/test-benchmark/test-retrieval-eval were removed) --
    either way the guard below would vacuously pass on nothing.
    """
    targets = _targets()
    compose_dependent = [name for name, (_, body) in targets.items() if _is_compose_dependent(body)]
    assert compose_dependent, "expected at least one compose-dependent Makefile target (found none)"


@pytest.mark.parametrize(
    "name",
    ["test-integration", "test-benchmark", "test-retrieval-eval"],
)
def test_known_compose_targets_are_detected_as_compose_dependent(name: str) -> None:
    """Canary: the three targets #209 is about must themselves be recognized
    as compose-dependent, or the guard test below would silently exempt them.
    """
    targets = _targets()
    assert name in targets, f"expected Makefile target `{name}` to exist"
    _, body = targets[name]
    assert _is_compose_dependent(body), (
        f"`{name}` was expected to be detected as compose-dependent (it runs "
        f"compose-marked tests) but the heuristic did not flag it: {body!r}"
    )


def test_test_fast_is_not_flagged_compose_dependent() -> None:
    """Canary for the other direction: `test-fast` deliberately EXCLUDES
    compose tests (`-m 'not compose and ...'`) and must never be required to
    depend on `require-stack` -- it is the fast offline loop specifically
    because it does NOT touch the stack. If this test starts failing, the
    detection heuristic in `_is_compose_dependent` has become too broad.
    """
    targets = _targets()
    assert "test-fast" in targets
    _, body = targets["test-fast"]
    assert not _is_compose_dependent(body), (
        "test-fast runs 'not compose' (exclusion) and must not be flagged "
        f"compose-dependent: {body!r}"
    )


def test_every_compose_dependent_target_requires_stack() -> None:
    """The actual regression guard: every compose-dependent target's
    prerequisite list must include `require-stack`, or a stack-down run
    silently degrades into an all-skipped pytest exit 0 again (#209).
    """
    targets = _targets()
    offenders = [
        name
        for name, (prereqs, body) in targets.items()
        if _is_compose_dependent(body) and "require-stack" not in prereqs
    ]
    assert not offenders, (
        "the following Makefile target(s) run compose-marked tests but do "
        f"not depend on `require-stack`: {offenders}. Add `require-stack` "
        "to the target's prerequisite list (`target-name: require-stack`) "
        "so a missing stack fails loudly instead of all-skipping to a "
        "silent pytest exit 0 (#209). See scripts/dev/require-stack.sh."
    )


def test_require_stack_target_invokes_the_script() -> None:
    """`require-stack` itself must call the actual pre-flight script, not
    just exist as a documentation-only no-op target.
    """
    targets = _targets()
    assert "require-stack" in targets, "Makefile must define a `require-stack` target"
    _, body = targets["require-stack"]
    assert "scripts/dev/require-stack.sh" in body, (
        f"`require-stack` target must invoke scripts/dev/require-stack.sh, got: {body!r}"
    )


def test_require_stack_script_exists_and_is_executable() -> None:
    script = REPO_ROOT / "scripts" / "dev" / "require-stack.sh"
    assert script.is_file(), f"{script} must exist"
    assert script.stat().st_mode & 0o111, f"{script} must be executable"


def test_run_compose_suite_script_exists_and_is_executable() -> None:
    script = REPO_ROOT / "scripts" / "dev" / "run-compose-suite.sh"
    assert script.is_file(), f"{script} must exist"
    assert script.stat().st_mode & 0o111, f"{script} must be executable"
