"""Repo-level guard: the s3rver container must start deterministically (#353).

Why this suite exists
---------------------
`s3rver` runs on a bare ``node:20-alpine`` and installs itself from npm on
every container start. Two properties of that arrangement broke the
PR-blocking `E2E smoke` lane:

- **No ``start_period``.** Docker began health probes immediately, so the npm
  download raced a ``interval: 10s x retries: 5`` budget. The stack was torn
  down ~40s in with ``container inherent-oss-s3rver is unhealthy``, on four
  unrelated branches in one morning.
- **No version pin.** ``npx s3rver`` resolved whatever npm served that day, so
  the S3 implementation under every integration and E2E run could change with
  no commit -- the same class of drift as the image lockfile pinning in #225.

No runtime test can catch either: a passing stack proves only that npm was
fast enough and that day's version worked. Both properties live in the compose
definition, so they are pinned there.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILES = ("docker-compose.yml", "docker-compose.release.yml")

# The install has to finish before a probe failure can tear the stack down.
# Measured >130s on a cold npm cache, so the total budget -- start_period plus
# retries x interval -- must clear that with room. text-embeddings-inference
# uses the same 90s + 12 x 10s for the same reason.
MINIMUM_TOTAL_BUDGET_SECONDS = 180


def _s3rver_block(compose: Path) -> str:
    text = compose.read_text(encoding="utf-8")
    match = re.search(r"^  s3rver:\n(?P<body>(?:(?:    .*)?\n)+)", text, re.MULTILINE)
    assert match, f"{compose.name}: no s3rver service found"
    return match.group("body")


@pytest.fixture(params=COMPOSE_FILES)
def s3rver(request) -> str:
    compose = REPO_ROOT / request.param
    assert compose.is_file(), f"{request.param} is missing"
    return _s3rver_block(compose)


def test_s3rver_version_is_pinned(s3rver: str) -> None:
    """A bare `npx s3rver` silently re-resolves on every start."""
    command = re.search(r"^    command: (?P<value>.+)$", s3rver, re.MULTILINE)
    assert command, "s3rver has no command"
    value = command.group("value")
    assert re.search(r"\bs3rver@\d+\.\d+\.\d+\b", value), (
        f"s3rver must be pinned to an exact version, got: {value}"
    )


def test_s3rver_npx_never_prompts(s3rver: str) -> None:
    """Without --yes, npx can block on an install confirmation and time out."""
    command = re.search(r"^    command: (?P<value>.+)$", s3rver, re.MULTILINE)
    assert command
    assert "--yes" in command.group("value")


def _seconds(s3rver: str, field: str) -> int:
    match = re.search(rf"^      {field}: (?P<value>\d+)s$", s3rver, re.MULTILINE)
    assert match, f"s3rver healthcheck has no {field}; see #353"
    return int(match.group("value"))


def test_s3rver_healthcheck_budgets_the_install(s3rver: str) -> None:
    """Probes must not tear the stack down while npm is still downloading."""
    start_period = _seconds(s3rver, "start_period")
    interval = _seconds(s3rver, "interval")
    retries = re.search(r"^      retries: (?P<value>\d+)$", s3rver, re.MULTILINE)
    assert retries, "s3rver healthcheck has no retries"

    total = start_period + interval * int(retries.group("value"))
    assert total >= MINIMUM_TOTAL_BUDGET_SECONDS, (
        f"s3rver has only {total}s before it is declared unhealthy; "
        f"the npm install alone measured >130s (#353)"
    )
