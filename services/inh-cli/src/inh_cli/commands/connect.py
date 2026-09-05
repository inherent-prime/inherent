"""Write MCP client config for Claude Code and Cursor."""

from __future__ import annotations

import json
import os
import shutil
import stat
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Callable

import httpx
import typer

from inh_cli import __version__
from inh_cli.client import ClientError
from inh_cli.config import resolve

# Claude Code JSON MCP config: ``type`` is required for HTTP. A ``url``-only
# entry is treated as stdio and skipped.
# Source: https://code.claude.com/docs/en/mcp (fetched 2026-08-30)
# Cursor remote servers: ``url`` + ``headers``; ``type: "http"`` is accepted
# by both the IDE (ignores type, routes on url) and cursor-agent (requires
# http/sse/stdio — ``streamable-http`` drops the whole file).
# Source: https://docs.cursor.com/en/context/mcp (fetched 2026-08-30)


@dataclass(frozen=True)
class Target:
    root_key: str
    label: str
    binary: str
    default_dir_name: str
    path_for: Callable[[], Path]


def _claude_path() -> Path:
    override = os.environ.get("CLAUDE_CONFIG_DIR")
    if override:
        return Path(override) / ".claude.json"
    return Path.home() / ".claude.json"


def _cursor_path() -> Path:
    return Path.home() / ".cursor" / "mcp.json"


TARGETS: dict[str, Target] = {
    "claude": Target(
        root_key="mcpServers",
        label="Claude Code",
        binary="claude",
        default_dir_name=".claude",
        path_for=_claude_path,
    ),
    "cursor": Target(
        root_key="mcpServers",
        label="Cursor",
        binary="cursor",
        default_dir_name=".cursor",
        path_for=_cursor_path,
    ),
}


def inherent_server_block(url: str, api_key: str) -> dict[str, Any]:
    return {
        "type": "http",
        "url": f"{url.rstrip('/')}/mcp",
        "headers": {"X-API-Key": api_key},
    }


def agent_present(target: Target) -> bool:
    if shutil.which(target.binary):
        return True
    return (Path.home() / target.default_dir_name).exists()


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    directory = path.parent
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(dir=directory, suffix=".tmp", text=True)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
        temporary.chmod(0o600)
        os.replace(temporary, path)
        path.chmod(stat.S_IMODE(0o600))
    finally:
        temporary.unlink(missing_ok=True)


def _verify_mcp(url: str, api_key: str) -> bool:
    mcp_url = f"{url.rstrip('/')}/mcp"
    try:
        response = httpx.post(
            mcp_url,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "X-API-Key": api_key,
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "inherent-cli", "version": __version__},
                },
            },
            timeout=5.0,
        )
    except httpx.HTTPError:
        return False
    return response.status_code == 200


def connect(
    agent: str,
    print_only: Annotated[
        bool,
        typer.Option("--print", help="Write the JSON block to stdout and touch no files."),
    ] = False,
    force: Annotated[
        bool, typer.Option("--force", help="Replace an existing inherent entry.")
    ] = False,
    config_path: Annotated[
        Path | None,
        typer.Option("--config-path", help="Override the agent's config file path."),
    ] = None,
) -> None:
    """Register the running stack as an MCP server in a coding agent."""

    if agent not in TARGETS:
        names = ", ".join(sorted(TARGETS))
        raise ClientError(f"Unknown agent `{agent}`. Choose one of: {names}.")
    target = TARGETS[agent]
    resolved = resolve()
    block = inherent_server_block(resolved.url, resolved.api_key)
    wrapper = {target.root_key: {"inherent": block}}
    if print_only:
        sys.stderr.write("The emitted JSON includes your API key.\n")
        print(json.dumps(wrapper, indent=2))
        return

    path = config_path or target.path_for()
    if not path.parent.exists() and not agent_present(target):
        raise ClientError(
            f"{target.label} config not found at {path}. "
            "Use `--print` and add it manually, or pass `--config-path`."
        )

    existing: dict[str, Any] = {}
    original_bytes = b""
    if path.exists():
        original_bytes = path.read_bytes()
        try:
            parsed = json.loads(original_bytes.decode("utf-8") or "{}")
        except json.JSONDecodeError as error:
            raise ClientError(
                f"Refusing to write {path}: existing file is not valid JSON."
            ) from error
        if not isinstance(parsed, dict):
            raise ClientError(f"Refusing to write {path}: existing file is not a JSON object.")
        existing = parsed

    servers = existing.setdefault(target.root_key, {})
    if not isinstance(servers, dict):
        raise ClientError(f"Refusing to write {path}: {target.root_key} is not an object.")
    if "inherent" in servers and servers["inherent"] != block and not force:
        if not sys.stdin.isatty():
            raise ClientError(
                "An inherent MCP entry already exists. Re-run with --force to replace it."
            )
        sys.stderr.write("Existing inherent entry:\n")
        sys.stderr.write(json.dumps(servers["inherent"], indent=2) + "\n")
        sys.stderr.write("Replacement:\n")
        sys.stderr.write(json.dumps(block, indent=2) + "\n")
        if not typer.confirm("Replace the existing inherent MCP entry?"):
            raise typer.Exit(1)

    # A re-run with the same stack is a no-op. Rewriting anyway left a new
    # timestamped backup on every invocation, each holding a plaintext API key.
    if servers.get("inherent") == block:
        sys.stderr.write(f"{path} already points at this stack; left unchanged.\n")
    else:
        if path.exists():
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            backup = path.with_name(f"{path.name}.bak-{stamp}")
            backup.write_bytes(original_bytes)
            backup.chmod(0o600)
            sys.stderr.write(f"Backup: {backup}\n")

        servers["inherent"] = block
        _atomic_write(path, existing)
    if _verify_mcp(resolved.url, resolved.api_key):
        sys.stdout.write("connected — try asking your agent: 'search my inherent docs for …'\n")
    else:
        sys.stdout.write(
            f"Wrote {path}, but POST {resolved.url}/mcp initialize did not succeed. "
            "Is the stack running?\n"
        )
