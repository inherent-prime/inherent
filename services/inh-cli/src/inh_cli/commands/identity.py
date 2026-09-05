"""Identity and local control-plane commands."""

from __future__ import annotations

import json
import secrets
import sys
from pathlib import Path
from typing import Annotated

import typer

from inh_cli.client import ClientError, call
from inh_cli.compose import require_running_stack, run_compose
from inh_cli.config import Config, load_config, resolve, save_config
from inh_cli.output import TableSpec, render
from inh_cli.secrets import parse_env_file
from inh_cli.stack import is_local_url

workspaces_app = typer.Typer(help="List workspaces visible to this key.")
keys_app = typer.Typer(help="List, create, or revoke API keys (writes are local-only).")


def _workspace(ctx: typer.Context) -> str | None:
    return ctx.obj.get("workspace") if ctx.obj else None


def _json_mode(ctx: typer.Context, json_flag: bool) -> bool:
    return json_flag or bool(ctx.obj and ctx.obj.get("json"))


def _require_local_writes() -> None:
    resolved = resolve(workspace_id=None)
    if not is_local_url(resolved.url):
        raise ClientError(
            f"Key writes only work against the local stack. {resolved.url} is remote. "
            "Unset INHERENT_URL to use the stack `inherent up` manages.",
            exit_code=2,
        )
    require_running_stack()


def _run_bootstrap(env_file: Path, extra: dict[str, str]) -> None:
    """Run a one-shot bootstrap container against the already-running stores.

    Names are passed to ``-e`` without values: compose then reads each from
    this process's environment. A ``-e KEY=value`` form would put the minted
    key in argv, where every local user can read it out of ``ps``.
    """

    args = ["run", "--rm"]
    for key in extra:
        args.extend(["-e", key])
    args.append("bootstrap")
    run_compose(args, env_file=env_file, env=extra)


def whoami(
    ctx: typer.Context,
    json_flag: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Show the authenticated key's identity."""

    response = call("GET", "/v1/whoami", workspace_id=_workspace(ctx))
    payload = response.json()
    if _json_mode(ctx, json_flag):
        print(json.dumps(payload, separators=(",", ":")))
        return
    render(
        payload,
        json_mode=False,
        table=TableSpec(
            (
                "key_name",
                "workspace_id",
                "permissions",
                "engine_version",
                "endpoint",
            )
        ),
    )


@workspaces_app.command("list")
def workspaces_list(
    ctx: typer.Context,
    json_flag: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """List workspaces. Falls back to /v1/whoami only on 404."""

    response = call(
        "GET",
        "/v1/admin/workspaces",
        workspace_id=_workspace(ctx),
        allow_statuses=(404,),
    )
    note = None
    if response.status_code == 404:
        whoami_response = call("GET", "/v1/whoami", workspace_id=_workspace(ctx))
        identity = whoami_response.json()
        payload = [
            {"workspace_id": workspace_id, "name": None, "document_count": None}
            for workspace_id in identity.get("workspace_ids") or []
        ]
        note = "Admin listings are gated. Showing this key's own workspaces from /v1/whoami."
    else:
        payload = response.json()
    if _json_mode(ctx, json_flag):
        print(json.dumps(payload, separators=(",", ":")))
        return
    render(
        payload,
        json_mode=False,
        table=TableSpec(("workspace_id", "name", "document_count")),
    )
    if note:
        sys.stderr.write(note + "\n")


@keys_app.command("list")
def keys_list(
    ctx: typer.Context,
    json_flag: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """List API key metadata. Local stack only."""

    response = call(
        "GET",
        "/v1/admin/keys",
        workspace_id=_workspace(ctx),
        allow_statuses=(404,),
    )
    if response.status_code == 404:
        raise ClientError(
            "Key listing requires a local stack (ADMIN_API_ENABLED). "
            "Against a remote deployment, use `inherent whoami` to see the key you are using."
        )
    payload = response.json()
    # Never print a full key value — the admin payload is prefixes only.
    if _json_mode(ctx, json_flag):
        print(json.dumps(payload, separators=(",", ":")))
        return
    render(
        payload,
        json_mode=False,
        table=TableSpec(("key_id", "key_name", "key_prefix", "workspace_id", "status")),
    )


@keys_app.command("create")
def keys_create(
    name: Annotated[str, typer.Option("--name")] = "Local CLI Key",
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
    save: Annotated[
        bool, typer.Option("--save", help="Write the new key into config.toml.")
    ] = False,
) -> None:
    """Mint a key via the bootstrap container. Prints the full value once."""

    _require_local_writes()
    env_file = require_running_stack()
    values = parse_env_file(env_file)
    new_key = "ink_" + secrets.token_urlsafe(32)
    extra = {
        "SERVICE_MODE": "bootstrap",
        "BOOTSTRAP_API_KEY": new_key,
        "BOOTSTRAP_WORKSPACE_ID": workspace or values.get("INHERENT_WORKSPACE_ID", ""),
        "BOOTSTRAP_USER_ID": values.get("INHERENT_USER_ID", ""),
        "BOOTSTRAP_KEY_NAME": name,
        "BOOTSTRAP_ACTION": "create",
    }
    _run_bootstrap(env_file, extra)
    sys.stdout.write(f"{new_key}\n")
    sys.stderr.write("Save this key now; it will not be shown again.\n")
    if save:
        current = load_config()
        save_config(
            Config(
                url=current.url if current else "http://localhost:18000",
                api_key=new_key,
                workspace_id=workspace
                or (current.workspace_id if current else values.get("INHERENT_WORKSPACE_ID")),
                engine_version=current.engine_version if current else None,
            )
        )


@keys_app.command("revoke")
def keys_revoke(
    prefix: str,
    yes: Annotated[bool, typer.Option("--yes")] = False,
    force: Annotated[
        bool,
        typer.Option("--force", help="Allow revoking the key currently in config.toml."),
    ] = False,
) -> None:
    """Revoke a key by prefix via the bootstrap container."""

    _require_local_writes()
    env_file = require_running_stack()
    current = load_config()
    if current and current.api_key.startswith(prefix) and not force:
        raise ClientError(
            "Refusing to revoke the key currently in config.toml. Pass --force if you mean it."
        )
    if not yes:
        if not sys.stdin.isatty():
            raise ClientError("Refusing to revoke without --yes (no TTY).")
        if not typer.confirm(f"Revoke key prefix {prefix}?"):
            raise typer.Exit(1)
    extra = {
        "SERVICE_MODE": "bootstrap",
        "BOOTSTRAP_ACTION": "revoke",
        "BOOTSTRAP_KEY_PREFIX": prefix,
    }
    _run_bootstrap(env_file, extra)
    sys.stdout.write(f"Revoked {prefix}\n")
