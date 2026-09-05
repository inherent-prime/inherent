"""Stack lifecycle commands: up, down, status, logs, doctor."""

from __future__ import annotations

import json
import sys
from typing import Annotated, Any
from urllib.parse import urlparse

import httpx
import typer
from rich.console import Console
from rich.table import Table

from inh_cli import __version__
from inh_cli.client import ClientError, make_client, request
from inh_cli.compose import (
    compose_env_path,
    compose_ps,
    preflight_docker,
    require_running_stack,
    run_compose,
    stack_is_running,
)
from inh_cli.config import Config, Resolved, load_config, save_config
from inh_cli.output import TableSpec, render
from inh_cli.secrets import load_or_create_compose_env

LOCAL_API_URL = "http://localhost:18000"
INGESTION_HEALTH_URL = "http://localhost:18002/health"
DOCKER_INSTALL_URL = "https://docs.docker.com/get-docker/"


def _json_mode(ctx: typer.Context, json_flag: bool) -> bool:
    return json_flag or bool(ctx.obj and ctx.obj.get("json"))


def _published_ports(row: dict[str, Any]) -> str:
    publishers = row.get("Publishers") or []
    parts = []
    for publisher in publishers:
        published = publisher.get("PublishedPort") or publisher.get("URL")
        target = publisher.get("TargetPort")
        if published and target:
            parts.append(f"{published}->{target}")
        elif published:
            parts.append(str(published))
    return ",".join(parts)


def _service_counts(rows: list[dict[str, Any]]) -> tuple[int, int]:
    total = len(rows)
    ready = 0
    for row in rows:
        state = str(row.get("State", "")).lower()
        if state == "running":
            ready += 1
        elif state == "exited" and str(row.get("ExitCode", "0")) in {"0", "0.0"}:
            ready += 1
    return ready, total


def _print_banner(
    *,
    workspace_id: str,
    api_key: str,
    ready: int,
    total: int,
    first_run: bool,
) -> None:
    # Only the prefix, always. `inherent up` runs in CI (T10's E2E) and its
    # stdout lands in a build log; the full key is in config.toml at 0600.
    masked = api_key[:12] + "…" if len(api_key) > 12 else api_key
    sys.stdout.write(
        "\n"
        f"  ✓ Stack healthy — {ready}/{total} services\n"
        f"  ✓ Workspace ready: {workspace_id}\n"
        f"  ✓ API key {'minted and saved' if first_run else 'loaded'} → ~/.inherent/config.toml\n"
        f"    {masked}\n"
        "\n"
        "  Connect your agent:\n"
        "    inherent connect claude\n"
        "    inherent connect cursor\n"
        "\n"
        "  Try it now:\n"
        "    inherent docs upload ./README.md\n"
        '    inherent search "what is inherent?"\n'
        "\n"
        f"  REST {LOCAL_API_URL} · MCP {LOCAL_API_URL}/mcp\n"
        "\n"
    )


# Compose surfaces a missing tag as a registry manifest error naming the digest,
# not the tag we asked for. `up` defaults INHERENT_VERSION to the CLI's own
# version, so a CLI published ahead of its engine images fails here first.
_MISSING_IMAGE_MARKERS = (
    "manifest unknown",
    "not found: manifest",
    "manifest for",
    "pull access denied",
    "denied",
)


def _up_failure(error: ClientError, version: str, *, explicit: bool) -> ClientError:
    """Explain a failed `up`, naming the image tag when that is the cause."""

    message = str(error)
    if not any(marker in message.lower() for marker in _MISSING_IMAGE_MARKERS):
        return error
    chosen = "--engine-version" if explicit else "this CLI's version"
    return ClientError(
        f"No engine images published for version {version} (taken from {chosen}). "
        f"Pick a published release with `inherent up --engine-version <version>`.\n\n"
        f"{message}",
        exit_code=1,
    )


def up(
    engine_version: Annotated[
        str | None,
        typer.Option(
            "--engine-version",
            help="Pin the engine image tag (default: this CLI's version).",
        ),
    ] = None,
    detach: Annotated[
        bool,
        typer.Option("--detach/--no-detach", help="Wait in the background (default)."),
    ] = True,
    registry: Annotated[
        str | None,
        typer.Option("--registry", help="Override INHERENT_REGISTRY."),
    ] = None,
) -> None:
    """Pull images, start the stack, seed a workspace, and save config."""

    preflight_docker()
    env_path, values = load_or_create_compose_env()
    version = (engine_version or __version__).lstrip("v")
    child_env = {"INHERENT_VERSION": version}
    if registry:
        child_env["INHERENT_REGISTRY"] = registry

    try:
        if detach:
            compose_args = ["up", "-d", "--wait"]
            run_compose(compose_args, env_file=env_path, env=child_env)
        else:
            run_compose(["up"], env_file=env_path, env=child_env, capture=False)
    except ClientError as error:
        raise _up_failure(error, version, explicit=engine_version is not None) from error

    api_key = values["INHERENT_API_KEY"]
    workspace_id = values["INHERENT_WORKSPACE_ID"]
    resolved = Resolved(url=LOCAL_API_URL, api_key=api_key, workspace_id=workspace_id)
    with make_client(resolved) as client:
        request(client, "GET", "/v1/whoami")

    first_run = load_config() is None
    save_config(
        Config(
            url=LOCAL_API_URL,
            api_key=api_key,
            workspace_id=workspace_id,
            engine_version=version,
        )
    )
    rows = compose_ps(env_file=env_path)
    ready, total = _service_counts(rows)
    _print_banner(
        workspace_id=workspace_id,
        api_key=api_key,
        ready=ready,
        total=total,
        first_run=first_run,
    )


def down(
    volumes: Annotated[
        bool,
        typer.Option("--volumes", help="Also destroy volumes (all indexed data)."),
    ] = False,
    yes: Annotated[
        bool,
        typer.Option("--yes", help="Skip the destructive confirmation prompt."),
    ] = False,
) -> None:
    """Stop the local stack."""

    preflight_docker()
    env_file = compose_env_path()
    if not env_file.exists():
        raise ClientError("Stack is not running. Run `inherent up` first.", exit_code=2)
    if volumes:
        if not yes:
            if not sys.stdin.isatty():
                raise ClientError(
                    "Refusing to destroy volumes without --yes (no TTY). "
                    "This deletes all indexed documents, vectors, and database data."
                )
            if not typer.confirm(
                "This deletes all indexed data (documents, vectors, databases). Continue?"
            ):
                raise typer.Exit(1)
        run_compose(["down", "-v"], env_file=env_file)
        sys.stdout.write("Stack stopped and volumes removed.\n")
        return
    run_compose(["down"], env_file=env_file)
    sys.stdout.write("Stack stopped.\n")


def _health_payload(path: str = "/health") -> tuple[int, dict[str, Any] | None]:
    try:
        response = httpx.get(f"{LOCAL_API_URL}{path}", timeout=2.0)
    except httpx.HTTPError:
        return 0, None
    try:
        payload = response.json()
    except ValueError:
        return response.status_code, None
    if isinstance(payload, dict):
        return response.status_code, payload
    return response.status_code, None


def status(
    ctx: typer.Context,
    json_flag: Annotated[
        bool, typer.Option("--json", help="Write machine-readable JSON to stdout.")
    ] = False,
) -> None:
    """Show compose state merged with API health."""

    preflight_docker()
    env_file = compose_env_path()
    if not env_file.exists():
        raise ClientError("Stack is not running. Run `inherent up` first.", exit_code=2)
    rows = compose_ps(env_file=env_file)
    if not stack_is_running(rows):
        raise ClientError("Stack is not running. Run `inherent up` first.", exit_code=2)

    # /health is a liveness probe: {"status", "service"} and nothing else.
    # /health/ready is the one carrying `version` and per-component `checks`,
    # so reading `version` off /health left status --json permanently null.
    _, health = _health_payload("/health/ready")
    merged = []
    for row in rows:
        service = row.get("Service") or row.get("Name") or ""
        entry = {
            "service": service,
            "state": row.get("State"),
            "health": row.get("Health") or "",
            "ports": _published_ports(row),
            "image": row.get("Image") or "",
        }
        if "public-api" in str(service) and health:
            entry["api_health"] = health.get("status")
            entry["engine_version"] = health.get("version")
        merged.append(entry)

    if _json_mode(ctx, json_flag):
        print(json.dumps({"services": merged, "health": health}, separators=(",", ":")))
        return
    render(
        merged,
        json_mode=False,
        table=TableSpec(("service", "state", "health", "ports", "image")),
    )


def logs(
    service: Annotated[str | None, typer.Argument(help="Compose service name.")] = None,
    follow: Annotated[bool, typer.Option("-f", "--follow")] = False,
    tail: Annotated[int | None, typer.Option("--tail", help="Number of lines.")] = None,
) -> None:
    """Stream compose logs. Unknown service names suggest a close match."""

    preflight_docker()
    env_file = require_running_stack()
    rows = compose_ps(env_file=env_file)
    names = [str(row.get("Service") or "") for row in rows if row.get("Service")]
    if service and service not in names:
        import difflib

        matches = difflib.get_close_matches(service, names, n=1, cutoff=0.4)
        hint = f" Did you mean `{matches[0]}`?" if matches else ""
        raise ClientError(f"Unknown service `{service}`.{hint}")
    args = ["logs"]
    if follow:
        args.append("-f")
    if tail is not None:
        args.extend(["--tail", str(tail)])
    if service:
        args.append(service)
    run_compose(args, env_file=env_file, capture=False)


def doctor(
    ctx: typer.Context,
    json_flag: Annotated[
        bool, typer.Option("--json", help="Write machine-readable JSON to stdout.")
    ] = False,
) -> None:
    """Probe /health/ready and published ports, then print triage hints."""

    preflight_docker()
    env_file = compose_env_path()
    if not env_file.exists() or not stack_is_running():
        raise ClientError("Stack is not running. Run `inherent up` first.", exit_code=2)

    report: dict[str, Any] = {"status": "healthy", "checks": {}, "hints": []}
    status_code, payload = _health_payload("/health/ready")
    if not payload:
        report["status"] = "unhealthy"
        report["hints"].append("GET /health/ready failed. Run: inherent logs inh-public-api-svc")
    else:
        report["status"] = payload.get("status", "unhealthy")
        report["checks"] = payload.get("checks") or {}
        report["engine_version"] = payload.get("version")
        if status_code >= 500 and report["status"] == "healthy":
            report["status"] = "unhealthy"

    # Port reachability for the two published Inherent APIs.
    for label, url in (
        ("inh-public-api-svc", f"{LOCAL_API_URL}/health"),
        ("inh-ingestion-svc", INGESTION_HEALTH_URL),
    ):
        try:
            httpx.get(url, timeout=2.0)
            report.setdefault("ports", {})[label] = "reachable"
        except httpx.HTTPError:
            report.setdefault("ports", {})[label] = "unreachable"
            report["hints"].append(f"Cannot reach {url}. Run: inherent logs {label}")
            if report["status"] == "healthy":
                report["status"] = "degraded"

    checks = report.get("checks") or {}
    if isinstance(checks, dict):
        for name, component in checks.items():
            status = component.get("status") if isinstance(component, dict) else None
            if status and status != "healthy":
                report["hints"].append(
                    f"{name} is {status}. Inspect with: inherent logs inh-public-api-svc"
                )
                if report["status"] == "healthy":
                    report["status"] = "degraded"

    json_mode = _json_mode(ctx, json_flag)
    if json_mode:
        print(json.dumps(report, separators=(",", ":")))
    else:
        console = Console()
        table = Table(title="doctor")
        table.add_column("check")
        table.add_column("status")
        table.add_row("stack", str(report["status"]))
        for name, component in (checks or {}).items():
            if isinstance(component, dict):
                table.add_row(str(name), str(component.get("status", "")))
        console.print(table)
        for hint in report["hints"]:
            console.print(hint)

    status_name = str(report["status"])
    if status_name == "healthy":
        raise typer.Exit(0)
    if status_name == "degraded":
        raise typer.Exit(1)
    raise typer.Exit(1)


def register(app: typer.Typer) -> None:
    app.command("up")(up)
    app.command("down")(down)
    app.command("status")(status)
    app.command("logs")(logs)
    app.command("doctor")(doctor)


def is_local_url(url: str) -> bool:
    host = urlparse(url).hostname
    return host in {"localhost", "127.0.0.1", "::1"}
