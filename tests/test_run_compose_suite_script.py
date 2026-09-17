"""Pin scripts/dev/run-compose-suite.sh's own JUnit-report decision (R2).

`test_makefile_compose_preflight_guard.py` pins that every compose-dependent
Makefile target WIRES this script in as a prerequisite -- but nothing
exercised what the script actually DOES with pytest's JUnit report once it
runs: sum `tests="N"` / `skipped="N"` off every `<testsuite>` element and
refuse to call the run a pass when nothing executed (`executed <= 0`), even
if pytest's own exit code was 0 (#209's second half). That decision had zero
test coverage, so a future edit (e.g. `head -1` silently reverted to only
reading the first `<testsuite>`, or the `tests - skipped` arithmetic
flipped) could regress #209 with an all-green suite.

TEST STRATEGY -- stub `uv` instead of running a real service:
`run-compose-suite.sh`'s only external dependency is `uv run pytest -m
"$MARKER_EXPR" --junitxml="$REPORT"`, invoked via a bare `uv` looked up on
`$PATH`. Prepending a tiny stub `uv` executable to `$PATH` lets each test
control exactly what JUnit XML the script sees and what exit code the
"pytest" run returns, without needing a real Postgres/Weaviate/RabbitMQ
stack or real pytest markers/fixtures -- and without changing one line of
the script's own production contract (no seam added; `SERVICE_DIR` is just
a plain directory the stub `cd`s into, matching the real `uv run pytest`
invocation shape exactly). This directly pins the counting + exit-code
logic the review flagged as untested, deterministically.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "dev" / "run-compose-suite.sh"

# A stub `uv` that intercepts `uv run pytest -m <expr> --junitxml=<path>`.
# Controlled entirely through env vars so each test can shape the scenario:
#   STUB_JUNIT_CONTENT  -- text to write to the requested --junitxml path.
#                          Unset entirely (not even empty-string) means:
#                          never create the file at all, simulating pytest
#                          crashing before it ever wrote a report.
#   STUB_EXIT_CODE      -- exit code the stub "pytest" run returns (default 0).
_STUB_UV = """#!/usr/bin/env bash
set -uo pipefail
report=""
for arg in "$@"; do
  case "$arg" in
    --junitxml=*) report="${arg#--junitxml=}" ;;
  esac
done
if [ -n "${STUB_JUNIT_CONTENT+set}" ] && [ -n "$report" ]; then
  printf '%s' "$STUB_JUNIT_CONTENT" > "$report"
fi
exit "${STUB_EXIT_CODE:-0}"
"""

# -- Synthetic JUnit XML fixtures ------------------------------------------

NORMAL_PASS_XML = (
    '<?xml version="1.0" encoding="utf-8"?>\n'
    '<testsuites><testsuite name="pytest" errors="0" failures="0" '
    'skipped="2" tests="10" time="1.0"><testcase/></testsuite></testsuites>\n'
)

ALL_SKIPPED_XML = (
    '<?xml version="1.0" encoding="utf-8"?>\n'
    '<testsuites><testsuite name="pytest" errors="0" failures="0" '
    'skipped="24" tests="24" time="1.0"></testsuite></testsuites>\n'
)

REAL_FAILURE_XML = (
    '<?xml version="1.0" encoding="utf-8"?>\n'
    '<testsuites><testsuite name="pytest" errors="0" failures="1" '
    'skipped="0" tests="10" time="1.0"><testcase/></testsuite></testsuites>\n'
)

# The R1 regression case: more than one <testsuite> element. Reading only
# the first (the pre-fix `head -1` bug) would see tests=3/skipped=3 ->
# executed=0 -> a FALSE FAILURE, even though the second suite ran 9 real
# tests. Summing across both gives tests=12/skipped=3/executed=9.
MULTI_SUITE_XML = (
    "<testsuites>"
    '<testsuite name="a" skipped="3" tests="3"/>'
    '<testsuite name="b" skipped="0" tests="9"/>'
    "</testsuites>\n"
)

MALFORMED_XML = "this is not xml at all\n"


def _run(
    tmp_path: Path,
    *,
    junit_content: str | None,
    stub_exit_code: int,
    marker_expr: str = "compose",
    label: str = "test-label",
) -> subprocess.CompletedProcess[str]:
    """Invoke the real script as a subprocess against a stubbed `uv`.

    ``junit_content=None`` means the stub never creates the --junitxml file
    at all (missing-report case); any string (including "") writes it
    verbatim.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    stub = bin_dir / "uv"
    stub.write_text(_STUB_UV)
    stub.chmod(0o755)

    service_dir = tmp_path / "svc"
    service_dir.mkdir(exist_ok=True)

    import os

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["STUB_EXIT_CODE"] = str(stub_exit_code)
    if junit_content is not None:
        env["STUB_JUNIT_CONTENT"] = junit_content
    else:
        env.pop("STUB_JUNIT_CONTENT", None)

    return subprocess.run(
        ["bash", str(SCRIPT), str(service_dir), marker_expr, label],
        capture_output=True,
        text=True,
        env=env,
    )


def test_script_exists_and_is_executable() -> None:
    assert SCRIPT.is_file()
    assert SCRIPT.stat().st_mode & 0o111


def test_all_skipped_fails_even_though_inner_pytest_exited_zero(tmp_path: Path) -> None:
    """#209's core assertion: pytest's own 0 exit code is not evidence
    anything ran -- an all-skipped run must fail the script."""
    proc = _run(tmp_path, junit_content=ALL_SKIPPED_XML, stub_exit_code=0)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "0 EXECUTED" in proc.stdout
    assert "24" in proc.stdout  # both the collected count and skipped count


def test_normal_pass_propagates_inner_exit_code_zero(tmp_path: Path) -> None:
    proc = _run(tmp_path, junit_content=NORMAL_PASS_XML, stub_exit_code=0)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "EXECUTED" not in proc.stdout


def test_real_failures_propagate_inner_nonzero_exit_code(tmp_path: Path) -> None:
    """executed > 0 here (10 tests, 0 skipped) -- the guard must not fire,
    and the script must not mask pytest's own failing exit code as a pass."""
    proc = _run(tmp_path, junit_content=REAL_FAILURE_XML, stub_exit_code=1)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "EXECUTED" not in proc.stdout  # the 0-executed guard did not fire


def test_multi_suite_junit_counts_the_sum_across_suites(tmp_path: Path) -> None:
    """R1: more than one <testsuite> element must be summed, not truncated
    to the first one. tests=3+9=12, skipped=3+0=3, executed=9 -- a pass."""
    proc = _run(tmp_path, junit_content=MULTI_SUITE_XML, stub_exit_code=0)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "EXECUTED" not in proc.stdout


@pytest.mark.parametrize(
    "junit_content",
    [
        pytest.param(None, id="missing-report-file"),
        pytest.param("", id="empty-report-file"),
        pytest.param(MALFORMED_XML, id="malformed-non-xml-report"),
    ],
)
def test_missing_empty_or_malformed_report_fails_loudly_never_silently_passes(
    tmp_path: Path, junit_content: str | None
) -> None:
    """A report the script cannot read attributes from must never be
    treated as a pass, regardless of what the inner pytest run returned --
    the whole point of this script is to be the thing you can trust."""
    proc = _run(tmp_path, junit_content=junit_content, stub_exit_code=0)
    assert proc.returncode != 0, proc.stdout + proc.stderr
    assert "0 EXECUTED" in proc.stdout
