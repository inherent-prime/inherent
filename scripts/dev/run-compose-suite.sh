#!/usr/bin/env bash
# run-compose-suite.sh -- run a Compose-marked pytest suite and refuse to
# report success if it executed zero tests (#209, second half of the fix).
#
# scripts/dev/require-stack.sh (wired as a Makefile prerequisite ahead of
# this script) closes the common case: no stack up at all. This script is
# the defense-in-depth layer for every OTHER way a compose suite can end up
# executing nothing while still exiting 0 -- a stack that is up but missing
# one dependency doctor.sh doesn't probe, a marker expression that
# (accidentally) selects no tests for this service, a fixture that skips for
# a reason unrelated to reachability -- pytest's own exit code cannot tell
# "24 skipped" apart from "24 passed" (both are 0), so this script asserts
# it directly from pytest's own JUnit report instead of trusting $?.
#
# Every caller MUST pass the FULL marker expression, including `compose`
# itself: a bare `pytest -m benchmark` (no `compose`) does not add to a
# service's `addopts` default (`-m 'not compose'`) -- a command-line `-m`
# REPLACES it -- so `-m benchmark` alone selects exactly the compose-marked
# benchmark tests the default was excluding, defeating the safety default
# instead of respecting it. Pass "compose and benchmark", not "benchmark".
#
# Usage: run-compose-suite.sh <service-dir> <marker-expression> <label>
#   service-dir        e.g. services/inh-public-api-svc
#   marker-expression  passed verbatim to `pytest -m`, e.g. "compose",
#                       "compose and benchmark", "compose and retrieval_eval"
#   label               short identifier used in the failure message and the
#                       scratch report filename (e.g. "test-integration")
set -uo pipefail

if [ "$#" -ne 3 ]; then
  echo "usage: $0 <service-dir> <marker-expression> <label>" >&2
  exit 2
fi

SERVICE_DIR=$1
MARKER_EXPR=$2
LABEL=$3

RED='\033[0;31m'
BOLD='\033[1m'
RESET='\033[0m'

REPORT="$(mktemp -t "${LABEL}-junit-XXXXXX.xml")"
trap 'rm -f "$REPORT"' EXIT

( cd "$SERVICE_DIR" && uv run pytest -m "$MARKER_EXPR" --junitxml="$REPORT" )
pytest_exit=$?

# Pull `tests="N"` / `skipped="N"` off EVERY <testsuite> element and SUM
# them -- a JUnit report can carry more than one <testsuite> (e.g. a suite
# split by xdist worker, or a future multi-service run), and reading only
# the first one under-counts `tests` and `executed` for every suite after
# it. Regex over the raw JUnit XML rather than an XML parser: this is a
# two-attribute read on a single well-known tag, and the root suite (this
# script's caller, `tests/`) deliberately carries no project/dependencies of
# its own to import one for (see docs/testing.md and
# test_ci_schema_fidelity.py's docstring for the same house convention).
# `<testsuite ` (trailing space) deliberately does not match the outer
# `<testsuites>` wrapper element.
tests=$(grep -o '<testsuite [^>]*' "$REPORT" | awk -F'"' '{for (i = 1; i <= NF; i++) if ($i ~ /tests=$/) sum += $(i + 1)} END {print sum + 0}')
skipped=$(grep -o '<testsuite [^>]*' "$REPORT" | awk -F'"' '{for (i = 1; i <= NF; i++) if ($i ~ /skipped=$/) sum += $(i + 1)} END {print sum + 0}')
executed=$((tests - skipped))

if [ "$executed" -le 0 ]; then
  printf "\n${RED}${BOLD}[%s] collected %s test(s), %s skipped, 0 EXECUTED.${RESET}\n" "$LABEL" "$tests" "$skipped"
  printf "A compose-marked suite that executes nothing is not a pass -- pytest's own\n"
  printf "exit code here was %s, which alone is NOT evidence anything ran (#209).\n" "$pytest_exit"
  printf "Confirm the stack is actually healthy (${BOLD}make doctor${RESET}) and that '%s'\n" "$MARKER_EXPR"
  printf "selects real tests for %s.\n\n" "$SERVICE_DIR"
  exit 1
fi

exit "$pytest_exit"
