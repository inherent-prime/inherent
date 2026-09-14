"""Repo-level guard: `make type-check` covers every package CI type-checks (#360).

THE BUG: `.github/workflows/ci.yml`'s `service-checks` matrix runs
`uv run mypy src` for all four packages (inh-ingestion-svc, inh-public-api-svc,
inh-contracts, inh-cli) -- each `include:` entry sets a `typecheck:` command
that the "Type check" step runs unconditionally. The Makefile's `type-check`
target, however, only `cd`ed into two of the four (`PUBLIC_API_DIR`,
`CLI_DIR`), with a stale help comment ("services that currently enable it")
papering over the gap. `make lint` and `make format-check` already cover all
four -- `type-check` was the one target silently narrower than CI. Real
consequence: PR #357 shipped a genuine mypy error in inh-ingestion-svc that
`make type-check` reported green on; CI's type-check step then failed and,
because it gates the Test step in the same job, skipped Test/coverage too --
a whole CI cycle with no test signal, discoverable only by reading the CI
logs directly.

THE FIX: `type-check` now `cd`s into all four package dirs, matching the
`lint`/`format-check` targets' idiom and ordering
(`INGESTION_DIR`, `PUBLIC_API_DIR`, `CONTRACTS_DIR`, `CLI_DIR`).

This test pins that parity going forward: any package CI type-checks (an
`include:` entry in the `service-checks` matrix with a non-empty
`typecheck:` command) must have a corresponding `cd $(<...>_DIR) && uv run
mypy src` line in the Makefile's `type-check` target, resolved through the
Makefile's own `<...>_DIR ?= services/<pkg>` variable definitions -- so a
future package added to CI's matrix without also being added to
`make type-check` fails this test immediately, instead of silently
regressing #360. Reads the raw Makefile/workflow text rather than a real
Make or GitHub Actions parser, matching the house convention in
`test_makefile_compose_preflight_guard.py` / `test_ci_schema_fidelity.py`:
the root suite deliberately carries no project dependencies to pull a real
parser in for.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MAKEFILE = REPO_ROOT / "Makefile"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"


def _makefile_text() -> str:
    return MAKEFILE.read_text()


def _ci_text() -> str:
    return CI_WORKFLOW.read_text()


def _makefile_target_body(name: str, text: str) -> str:
    """Return the tab-indented recipe body of Makefile target `name`.

    Mirrors `test_makefile_compose_preflight_guard.py`'s target-header
    convention: a target header is a line at column 0 shaped `name:
    prereqs`, followed by every following tab-indented line.
    """
    lines = text.splitlines()
    header_re = re.compile(rf"^{re.escape(name)}:")
    for i, line in enumerate(lines):
        if header_re.match(line):
            body_lines = []
            for later in lines[i + 1 :]:
                if not later.startswith("\t"):
                    break
                body_lines.append(later)
            return "\n".join(body_lines)
    raise AssertionError(f"expected Makefile target `{name}:` to exist")


def _makefile_dir_vars(text: str) -> dict[str, str]:
    """Map each `<NAME>_DIR ?= <path>` Makefile variable to its path value."""
    return dict(re.findall(r"^(\w+_DIR)\s*\?=\s*(\S+)\s*$", text, re.MULTILINE))


def _type_checked_dirs_in_makefile() -> set[str]:
    """Resolve every `cd $(<...>_DIR)` in `type-check`'s body to its path."""
    text = _makefile_text()
    body = _makefile_target_body("type-check", text)
    dir_vars = _makefile_dir_vars(text)
    dirs: set[str] = set()
    for var in re.findall(r"cd \$\((\w+_DIR)\)", body):
        assert var in dir_vars, (
            f"`type-check` references undefined Makefile variable `{var}`"
        )
        dirs.add(dir_vars[var])
    return dirs


def _ci_matrix_include_block() -> str:
    """Return the `service-checks` job's `include:` list as raw YAML text."""
    text = _ci_text()
    start = text.index("service-checks:")
    include_idx = text.index("include:", start)
    # The include list ends at `services:` (the Postgres service-container
    # key, back at the job's own indentation) which follows the matrix.
    end_idx = text.index("\n    services:", include_idx)
    return text[include_idx:end_idx]


def _ci_type_checked_dirs() -> set[str]:
    """Every `path:` in the CI matrix whose entry sets a real `typecheck:`.

    A blank/missing `typecheck:` key makes `matrix.typecheck` falsy, so the
    "Type check" step's `if: ${{ matrix.typecheck }}` skips it for that
    service -- that service does not count as CI-type-checked.
    """
    include_block = _ci_matrix_include_block()
    # Split into one chunk per matrix entry, keyed on `- service:`.
    entries = re.split(r"\n\s*- service:", include_block)[1:]
    dirs: set[str] = set()
    for entry in entries:
        path_match = re.search(r"path:\s*(\S+)", entry)
        typecheck_match = re.search(r"typecheck:\s*(\S.*)$", entry, re.MULTILINE)
        assert path_match is not None, (
            f"CI matrix entry has no `path:`: {entry[:200]!r}"
        )
        if typecheck_match and typecheck_match.group(1).strip():
            dirs.add(path_match.group(1))
    return dirs


def test_ci_matrix_has_at_least_one_typechecked_package() -> None:
    """Sanity check on the extraction heuristic: if this finds zero
    typecheck-enabled packages, the heuristic broke silently and the parity
    guard below would vacuously pass on nothing.
    """
    assert _ci_type_checked_dirs(), (
        "expected at least one package with a `typecheck:` command in the "
        "CI `service-checks` matrix (found none) -- the extraction regex "
        "may have broken"
    )


def test_type_check_target_covers_every_ci_typechecked_package() -> None:
    """The actual regression guard for #360: every package CI type-checks
    must also be covered by `make type-check`, or a real mypy error can pass
    the local gate green while CI reports it red.
    """
    ci_dirs = _ci_type_checked_dirs()
    make_dirs = _type_checked_dirs_in_makefile()
    missing = ci_dirs - make_dirs
    assert not missing, (
        f"CI type-checks {sorted(missing)} (via `service-checks`'s matrix "
        "`typecheck:` command) but Makefile's `type-check` target does not "
        "`cd` into it -- add `@cd $(<...>_DIR) && uv run mypy src` for it "
        "to `type-check` in the Makefile, matching `lint`/`format-check` "
        "(#360)."
    )
