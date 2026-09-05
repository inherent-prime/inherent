"""Generate and persist the values the release compose refuses to start without."""

from __future__ import annotations

import os
import secrets
import tempfile
from pathlib import Path

from inh_cli.compose import compose_env_path


def parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key] = value
    return values


def render_env_file(values: dict[str, str]) -> str:
    lines = [f"{key}={values[key]}" for key in sorted(values)]
    return "\n".join(lines) + "\n"


def _write_env(path: Path, values: dict[str, str]) -> None:
    directory = path.parent
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(dir=directory, text=True)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(render_env_file(values))
        temporary.chmod(0o600)
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)


def generate_missing(existing: dict[str, str]) -> dict[str, str]:
    """Fill only the keys that are absent. Present values stay byte-identical."""

    values = dict(existing)
    if not values.get("POSTGRES_PASSWORD"):
        values["POSTGRES_PASSWORD"] = secrets.token_urlsafe(32)
    if not values.get("WEAVIATE_API_KEY"):
        values["WEAVIATE_API_KEY"] = secrets.token_urlsafe(32)
    if not values.get("INGESTION_API_KEY"):
        values["INGESTION_API_KEY"] = secrets.token_urlsafe(32)
    if not values.get("INHERENT_API_KEY"):
        # Public API keys are rejected unless they start with ink_.
        values["INHERENT_API_KEY"] = "ink_" + secrets.token_urlsafe(32)
    if not values.get("INHERENT_WORKSPACE_ID"):
        values["INHERENT_WORKSPACE_ID"] = "ws_" + secrets.token_hex(6)
    if not values.get("INHERENT_USER_ID"):
        values["INHERENT_USER_ID"] = "usr_" + secrets.token_hex(8)
    # Local admin listings are safe on a single-operator stack.
    values.setdefault("ADMIN_API_ENABLED", "true")
    values.setdefault("INHERENT_KEY_NAME", "Local CLI Key")
    values.setdefault("INHERENT_WORKSPACE_NAME", "Default Workspace")
    return values


def load_or_create_compose_env() -> tuple[Path, dict[str, str]]:
    """Read ``compose.env``, generate whatever is missing, persist 0600."""

    path = compose_env_path()
    values = generate_missing(parse_env_file(path))
    _write_env(path, values)
    return path, values
