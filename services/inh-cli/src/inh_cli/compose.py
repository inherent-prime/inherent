"""Locate the bundled release compose file and run ``docker compose``."""

from __future__ import annotations

import json
import os
import subprocess
from importlib import resources
from pathlib import Path
from typing import Any, Mapping, Sequence

from inh_cli.client import ClientError
from inh_cli.config import home

COMPOSE_PROJECT = "inherent"
DOCKER_INSTALL_URL = "https://docs.docker.com/get-docker/"


def bundled_compose_path() -> Path:
    """Path to the compose file shipped inside the wheel.

    ``importlib.resources`` is required: a ``__file__``-relative path breaks
    the moment the package is installed as a zip or namespace.
    """

    return Path(str(resources.files("inh_cli") / "data" / "docker-compose.release.yml"))


def compose_env_path() -> Path:
    return home() / "compose.env"


def compose_argv(*args: str, env_file: Path | None = None) -> list[str]:
    """Build the exact argv later tasks must reuse, including key writes."""

    argv = [
        "docker",
        "compose",
        "-p",
        COMPOSE_PROJECT,
        "-f",
        str(bundled_compose_path()),
    ]
    if env_file is not None:
        argv.extend(["--env-file", str(env_file)])
    argv.extend(args)
    return argv


def _missing_docker() -> ClientError:
    return ClientError(
        "Docker is not installed or not on PATH. "
        f"Install Docker Engine and the Compose v2 plugin from {DOCKER_INSTALL_URL}.",
        exit_code=1,
    )


def preflight_docker() -> None:
    """Refuse to proceed without a running daemon and Compose v2."""

    try:
        version = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as error:
        raise _missing_docker() from error
    if version.returncode != 0 or not version.stdout.strip():
        raise ClientError(
            "Docker daemon is not running. Start Docker and retry. "
            f"Install help: {DOCKER_INSTALL_URL}.",
            exit_code=1,
        )
    try:
        compose = subprocess.run(
            ["docker", "compose", "version"],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as error:
        raise ClientError(
            "Docker Compose v2 is required (`docker compose`). "
            "The standalone `docker-compose` v1 binary is not supported.",
            exit_code=1,
        ) from error
    if compose.returncode != 0:
        raise ClientError(
            "Docker Compose v2 is required (`docker compose`). "
            "The standalone `docker-compose` v1 binary is not supported.",
            exit_code=1,
        )


def run_compose(
    args: Sequence[str],
    *,
    env_file: Path | None = None,
    env: Mapping[str, str] | None = None,
    capture: bool = True,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run one compose command against the bundled file."""

    argv = compose_argv(*args, env_file=env_file)
    child_env = os.environ.copy()
    if env:
        child_env.update(env)
    try:
        result = subprocess.run(
            argv,
            env=child_env,
            text=True,
            capture_output=capture,
            check=False,
        )
    except FileNotFoundError as error:
        raise _missing_docker() from error
    if check and result.returncode != 0:
        stderr = (result.stderr or result.stdout or "").strip()
        raise ClientError(stderr or f"docker compose {' '.join(args)} failed")
    return result


def parse_compose_ps(payload: str) -> list[dict[str, Any]]:
    """Parse ``docker compose ps --format json`` (array or JSONL)."""

    text = payload.strip()
    if not text:
        return []
    if text.startswith("["):
        data = json.loads(text)
        return list(data) if isinstance(data, list) else []
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def compose_ps(*, env_file: Path | None = None) -> list[dict[str, Any]]:
    result = run_compose(
        ["ps", "-a", "--format", "json"],
        env_file=env_file,
        check=False,
    )
    if result.returncode != 0:
        return []
    return parse_compose_ps(result.stdout or "")


def stack_is_running(rows: list[dict[str, Any]] | None = None) -> bool:
    """True when at least one long-running service is up."""

    if rows is None:
        env_file = compose_env_path()
        if not env_file.exists():
            return False
        rows = compose_ps(env_file=env_file)
    return any(str(row.get("State", "")).lower() == "running" for row in rows)


def require_running_stack() -> Path:
    """Return the compose env path or exit 2 when the stack is down."""

    env_file = compose_env_path()
    if not env_file.exists() or not stack_is_running():
        raise ClientError(
            "Stack is not running. Run `inherent up` first.",
            exit_code=2,
        )
    return env_file
