"""Repo-level guard: the s3rver container must start deterministically (#353).

Why this suite exists
---------------------
`s3rver` used to run on a bare ``node:20-alpine`` with
``command: npx s3rver ...``, which downloads the package from npm on EVERY
container start. Health probes begin the moment the container starts, so that
install raced a ``interval: 10s x retries: 5`` = 50s budget. ``up --wait``
tore the whole stack down mid-download and failed the required `E2E smoke`
gate on four unrelated branches in one morning.

The two compose files fix it differently, because they have different
constraints:

- ``docker-compose.yml`` (dev + CI) **builds** ``docker/s3rver``, so the npm
  install happens at build time. ``up --build --wait`` finishes the build
  before starting a container, so no probe can fire while npm is working: a
  slow registry costs build time instead of turning the lane red.
- ``docker-compose.release.yml`` **cannot build** -- a pip-installed
  ``inherent up`` runs it with no build context -- so it keeps the ``npx``
  form and instead gets a wide start-up budget.

Both must pin an exact version either way: a floating ``s3rver`` meant the S3
implementation under every integration and E2E run could change with no
commit, the same class of drift as the image lockfile pinning in #225.

No runtime test can catch any of this: a passing stack proves only that npm
was fast enough that day and that day's version worked. The properties live
in the build and compose definitions, so they are pinned there.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DEV_COMPOSE = REPO_ROOT / "docker-compose.yml"
RELEASE_COMPOSE = REPO_ROOT / "docker-compose.release.yml"
S3RVER_DOCKERFILE = REPO_ROOT / "docker" / "s3rver" / "Dockerfile"

# The release compose still installs at container start, so its probe must not
# be able to tear the stack down before that can finish. `start_period` plus
# `retries x interval` is the real budget; text-embeddings-inference uses the
# same 90s + 12 x 10s for its own slow first start.
MINIMUM_RELEASE_BUDGET_SECONDS = 180

VERSION_PIN = re.compile(r"\bs3rver@\d+\.\d+\.\d+\b")


def _s3rver_block(compose: Path) -> str:
    text = compose.read_text(encoding="utf-8")
    match = re.search(r"^  s3rver:\n(?P<body>(?:(?:    .*)?\n)+)", text, re.MULTILINE)
    assert match, f"{compose.name}: no s3rver service found"
    return match.group("body")


def _field(block: str, name: str) -> str | None:
    match = re.search(rf"^    {name}: (?P<value>.+)$", block, re.MULTILINE)
    return match.group("value") if match else None


def _seconds(block: str, field: str) -> int:
    match = re.search(rf"^      {field}: (?P<value>\d+)s$", block, re.MULTILINE)
    assert match, f"s3rver healthcheck has no {field}; see #353"
    return int(match.group("value"))


# --- dev / CI: the install must happen at build time -------------------------


def test_dev_compose_builds_s3rver_instead_of_downloading_at_startup() -> None:
    """`up --build` finishes the build before a probe can fire."""
    block = _s3rver_block(DEV_COMPOSE)
    assert "build:" in block, (
        "docker-compose.yml must build s3rver; an `npx` at container start "
        "races the healthcheck and tears the stack down (#353)"
    )
    # The command itself, not the block: the comments above it legitimately
    # mention npx while explaining why this service no longer uses it.
    command = _field(block, "command")
    assert command is None or "npx" not in command, (
        "the dev stack must not install s3rver at container start"
    )


def test_s3rver_image_pins_an_exact_version() -> None:
    """A floating version changes the S3 implementation with no commit."""
    assert S3RVER_DOCKERFILE.is_file(), f"{S3RVER_DOCKERFILE} is missing"
    content = S3RVER_DOCKERFILE.read_text(encoding="utf-8")
    assert re.search(r"^ARG S3RVER_VERSION=\d+\.\d+\.\d+$", content, re.MULTILINE), (
        "docker/s3rver/Dockerfile must pin S3RVER_VERSION to an exact version"
    )
    assert "npm install -g" in content


# --- release: no build context, so the budget has to absorb the install ------


def test_release_compose_pins_the_s3rver_version() -> None:
    """The release compose has no build context, but must still pin."""
    command = _field(_s3rver_block(RELEASE_COMPOSE), "command")
    assert command, "release s3rver has no command"
    assert VERSION_PIN.search(command), (
        f"s3rver must be pinned to an exact version, got: {command}"
    )


def test_release_compose_npx_never_prompts() -> None:
    """Without --yes, npx can block on an install confirmation and time out."""
    command = _field(_s3rver_block(RELEASE_COMPOSE), "command")
    assert command and "--yes" in command


def test_release_healthcheck_budgets_the_install() -> None:
    """Probes must not tear the stack down while npm is still downloading."""
    block = _s3rver_block(RELEASE_COMPOSE)
    retries = re.search(r"^      retries: (?P<value>\d+)$", block, re.MULTILINE)
    assert retries, "release s3rver healthcheck has no retries"

    total = _seconds(block, "start_period") + _seconds(block, "interval") * int(
        retries.group("value")
    )
    assert total >= MINIMUM_RELEASE_BUDGET_SECONDS, (
        f"s3rver has only {total}s before it is declared unhealthy; the npm "
        f"install it still does at container start measured 423s on a cold "
        f"cache (#353)"
    )


@pytest.mark.parametrize("compose", [DEV_COMPOSE, RELEASE_COMPOSE], ids=lambda p: p.name)
def test_s3rver_keeps_its_healthcheck(compose: Path) -> None:
    """Both stacks still gate dependents on s3rver actually serving."""
    assert "healthcheck:" in _s3rver_block(compose)
