#!/usr/bin/env bash
# require-stack.sh -- pre-flight guard for compose-dependent Makefile targets (#209).
#
# THE BUG THIS CLOSES: both Python services default to `-m 'not compose'` in
# `addopts` as a laptop-safety default. A command-line `-m` REPLACES that
# expression rather than intersecting with it, so `make test-integration`
# (`pytest -m compose`) selects precisely the tests the default was excluding
# -- and each of those tests then skips itself individually, at fixture
# setup, the moment it finds no reachable stack. An all-skipped pytest run
# still exits 0, so `make test-integration` on a laptop with no Docker
# running prints "N skipped" and reports success having verified nothing.
# `docs/maintainers/release_acceptance_matrix.md` treats that command as an
# acceptance step, and this is the exact defect class that let v0.6.0 ship
# believing it had e2e coverage it never had (see CHANGELOG's Hetzner-e2e
# "Removed" entry).
#
# THE FIX: probe the stack BEFORE pytest ever starts, so "stack not up"
# fails loudly and immediately instead of quietly degrading into an
# all-skipped pass. This script does not invent a second health-probe
# mechanism -- it reuses doctor.sh's own checks (same endpoints, same
# `docker compose exec` probes) verbatim and adds only the actionable
# next-step message a CI/test context needs that an interactive `make
# doctor` call does not.
#
# Usage: bash scripts/dev/require-stack.sh
# Exit 0: every service doctor.sh checks is healthy -- safe to run
#         compose-marked tests.
# Exit 1: at least one service is not reachable -- prints doctor.sh's full
#         per-service report, then the actionable next step.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

RED='\033[0;31m'
BOLD='\033[1m'
RESET='\033[0m'

if bash "$SCRIPT_DIR/doctor.sh"; then
  exit 0
fi

# doctor.sh already printed the per-service failure detail above (including
# `docker compose logs <svc>` hints); add the one thing it doesn't say,
# because doctor.sh is also used standalone outside a test context: what a
# compose-dependent test target needs the caller to do next.
printf "${RED}${BOLD}Stack not running${RESET} -- compose-dependent tests cannot run against it.\n"
printf "Every 'compose'-marked test skips itself at fixture setup when the stack is\n"
printf "unreachable, and pytest exits 0 on an all-skipped run -- so running the\n"
printf "tests anyway would silently report success having verified nothing (#209).\n\n"
printf "Run ${BOLD}make dev${RESET} first (starts the stack in the background and bootstraps the\n"
printf "dev workspace/key), then re-run this target. Use ${BOLD}make doctor${RESET} to re-check\n"
printf "readiness without running tests.\n\n"
exit 1
