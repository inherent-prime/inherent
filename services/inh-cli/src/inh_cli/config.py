"""Read and resolve the CLI's user configuration."""

from __future__ import annotations

import json
import os
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path

import click


@dataclass(frozen=True)
class Config:
    """Values persisted in ``config.toml``."""

    url: str
    api_key: str
    workspace_id: str | None = None
    engine_version: str | None = None
    compose_project: str = "inherent"


@dataclass(frozen=True)
class Resolved:
    """Endpoint and credentials ready for an API request."""

    url: str
    api_key: str
    workspace_id: str | None = None


class StackNotConfigured(click.ClickException):  # noqa: N818 - frozen public contract
    """Raised when neither environment nor disk provides connection details."""

    exit_code = 2


def home() -> Path:
    """Directory that holds ``config.toml`` and ``compose.env``.

    ``INHERENT_HOME`` relocates it for tests and non-default installs.
    """

    return Path(os.environ.get("INHERENT_HOME", Path.home() / ".inherent"))


def load_config() -> Config | None:
    """Load the user config, returning ``None`` when it does not exist."""

    path = home() / "config.toml"
    if not path.exists():
        return None
    with path.open("rb") as config_file:
        raw = tomllib.load(config_file)
    stack = raw.get("stack", {})
    api = raw.get("api", {})
    return Config(
        url=api.get("url", ""),
        api_key=api.get("key", ""),
        workspace_id=api.get("workspace_id"),
        engine_version=stack.get("version"),
        compose_project=stack.get("compose_project", "inherent"),
    )


def resolve(
    url: str | None = None,
    api_key: str | None = None,
    workspace_id: str | None = None,
) -> Resolved:
    """Resolve environment values before explicit and persisted fallbacks."""

    config = load_config()
    resolved_url = os.environ.get("INHERENT_URL") or url or (config.url if config else None)
    resolved_key = (
        os.environ.get("INHERENT_API_KEY") or api_key or (config.api_key if config else None)
    )
    if not resolved_url or not resolved_key:
        raise StackNotConfigured(
            "No local stack found. Run `inherent up`, or set INHERENT_URL / INHERENT_API_KEY."
        )
    return Resolved(
        url=resolved_url.rstrip("/"),
        api_key=resolved_key,
        workspace_id=workspace_id or (config.workspace_id if config else None),
    )


def save_config(config: Config) -> None:
    """Atomically persist config with owner-only permissions."""

    config_home = home()
    config_home.mkdir(mode=0o700, parents=True, exist_ok=True)
    lines = [
        "[stack]",
        f"compose_project = {json.dumps(config.compose_project)}",
    ]
    if config.engine_version is not None:
        lines.append(f"version = {json.dumps(config.engine_version)}")
    lines.extend(
        [
            "",
            "[api]",
            f"url = {json.dumps(config.url)}",
            f"key = {json.dumps(config.api_key)}",
        ]
    )
    if config.workspace_id is not None:
        lines.append(f"workspace_id = {json.dumps(config.workspace_id)}")

    path = config_home / "config.toml"
    descriptor, temporary_name = tempfile.mkstemp(dir=config_home, text=True)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as config_file:
            config_file.write("\n".join(lines) + "\n")
        temporary.chmod(0o600)
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)
