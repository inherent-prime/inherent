from __future__ import annotations

import json
import subprocess

import httpx
import pytest
from typer.testing import CliRunner

import inh_cli.client as client_mod
from inh_cli.main import app
from inh_cli.secrets import load_or_create_compose_env, parse_env_file

PS_PAYLOAD = [
    {
        "Service": "inh-public-api-svc",
        "State": "running",
        "Health": "healthy",
        "Image": "public-api-svc:0.7.0",
        "Publishers": [{"PublishedPort": 18000, "TargetPort": 8080}],
    },
    {
        "Service": "inh-ingestion-svc",
        "State": "running",
        "Health": "healthy",
        "Image": "ingestion-svc:0.7.0",
        "Publishers": [{"PublishedPort": 18002, "TargetPort": 8000}],
    },
]


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def test_up_compose_argv_and_whoami(inherent_home, runner, monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run(args, **kwargs):
        calls.append(list(args))
        return subprocess.CompletedProcess(["docker"], 0, stdout="", stderr="")

    monkeypatch.setattr("inh_cli.stack.preflight_docker", lambda: None)
    monkeypatch.setattr("inh_cli.stack.run_compose", fake_run)
    monkeypatch.setattr("inh_cli.stack.compose_ps", lambda **_: PS_PAYLOAD)

    def whoami(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/whoami"
        return httpx.Response(200, json={"key_id": "k", "workspace_id": "ws"}, request=request)

    monkeypatch.setattr(client_mod, "_transport", httpx.MockTransport(whoami))

    result = runner.invoke(app, ["up"])
    assert result.exit_code == 0, result.output
    assert calls[0][:3] == ["up", "-d", "--wait"]
    assert "Workspace ready" in result.stdout
    assert (inherent_home / "config.toml").exists()


def test_up_never_prints_the_full_api_key(inherent_home, runner, monkeypatch) -> None:
    """`inherent up` runs in CI; its stdout must never carry a usable key."""

    monkeypatch.setattr("inh_cli.stack.preflight_docker", lambda: None)
    monkeypatch.setattr(
        "inh_cli.stack.run_compose",
        lambda *a, **k: subprocess.CompletedProcess(["docker"], 0, stdout="", stderr=""),
    )
    monkeypatch.setattr("inh_cli.stack.compose_ps", lambda **_: PS_PAYLOAD)
    monkeypatch.setattr(
        client_mod,
        "_transport",
        httpx.MockTransport(lambda request: httpx.Response(200, json={}, request=request)),
    )

    result = runner.invoke(app, ["up"])
    assert result.exit_code == 0, result.output

    api_key = parse_env_file(inherent_home / "compose.env")["INHERENT_API_KEY"]
    assert len(api_key) > 12
    assert api_key not in result.stdout
    assert api_key[:12] in result.stdout


def test_status_table_and_json(inherent_home, runner, monkeypatch) -> None:
    load_or_create_compose_env()
    monkeypatch.setattr("inh_cli.stack.preflight_docker", lambda: None)
    monkeypatch.setattr("inh_cli.stack.compose_ps", lambda **_: PS_PAYLOAD)
    monkeypatch.setattr("inh_cli.stack.stack_is_running", lambda *_: True)

    health = {
        "status": "healthy",
        "version": "0.7.0",
        "service": "inh-public-api-svc",
        "checks": {},
    }
    monkeypatch.setattr("inh_cli.stack._health_payload", lambda path="/health": (200, health))

    table = runner.invoke(app, ["status"])
    assert table.exit_code == 0, table.output
    assert "inh-public-api-svc" in table.stdout

    encoded = runner.invoke(app, ["--json", "status"])
    assert encoded.exit_code == 0
    payload = json.loads(encoded.stdout)
    assert payload["services"][0]["service"] == "inh-public-api-svc"
    assert encoded.stdout.strip() == json.dumps(payload, separators=(",", ":"))


def test_status_without_stack_exits_2(inherent_home, runner, monkeypatch) -> None:
    monkeypatch.setattr("inh_cli.stack.preflight_docker", lambda: None)
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 2


def test_doctor_degraded_exits_1(inherent_home, runner, monkeypatch) -> None:
    load_or_create_compose_env()
    monkeypatch.setattr("inh_cli.stack.preflight_docker", lambda: None)
    monkeypatch.setattr("inh_cli.stack.stack_is_running", lambda *_: True)
    monkeypatch.setattr(
        "inh_cli.stack._health_payload",
        lambda path="/health/ready": (
            200,
            {
                "status": "degraded",
                "checks": {"weaviate": {"status": "degraded", "message": "slow"}},
            },
        ),
    )
    monkeypatch.setattr(
        "inh_cli.stack.httpx.get",
        lambda *a, **k: httpx.Response(200),
    )
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 1


def test_logs_unknown_service_suggests_match(inherent_home, runner, monkeypatch) -> None:
    load_or_create_compose_env()
    monkeypatch.setattr("inh_cli.stack.preflight_docker", lambda: None)
    monkeypatch.setattr(
        "inh_cli.stack.require_running_stack", lambda: inherent_home / "compose.env"
    )
    monkeypatch.setattr("inh_cli.stack.compose_ps", lambda **_: PS_PAYLOAD)
    result = runner.invoke(app, ["logs", "inh-public-api"])
    assert result.exit_code == 1
    assert "inh-public-api-svc" in result.output


def test_up_no_detach_and_registry(inherent_home, runner, monkeypatch) -> None:
    seen = {}

    def fake_run(args, **kwargs):
        seen["args"] = list(args)
        seen["env"] = kwargs.get("env")
        seen["capture"] = kwargs.get("capture")
        return subprocess.CompletedProcess(["docker"], 0, stdout="", stderr="")

    monkeypatch.setattr("inh_cli.stack.preflight_docker", lambda: None)
    monkeypatch.setattr("inh_cli.stack.run_compose", fake_run)
    monkeypatch.setattr("inh_cli.stack.compose_ps", lambda **_: PS_PAYLOAD)
    monkeypatch.setattr(
        client_mod,
        "_transport",
        httpx.MockTransport(lambda req: httpx.Response(200, json={}, request=req)),
    )
    result = runner.invoke(
        app, ["up", "--no-detach", "--registry", "ghcr.io/example", "--engine-version", "1.2.3"]
    )
    assert result.exit_code == 0, result.output
    assert seen["args"] == ["up"]
    assert seen["capture"] is False
    assert seen["env"]["INHERENT_REGISTRY"] == "ghcr.io/example"
    assert seen["env"]["INHERENT_VERSION"] == "1.2.3"


def test_down_and_logs_passthrough(inherent_home, runner, monkeypatch) -> None:
    load_or_create_compose_env()
    calls = []
    monkeypatch.setattr("inh_cli.stack.preflight_docker", lambda: None)
    monkeypatch.setattr(
        "inh_cli.stack.run_compose",
        lambda args, **kwargs: calls.append(list(args))
        or subprocess.CompletedProcess(["docker"], 0),
    )
    monkeypatch.setattr(
        "inh_cli.stack.require_running_stack", lambda: inherent_home / "compose.env"
    )
    monkeypatch.setattr("inh_cli.stack.compose_ps", lambda **_: PS_PAYLOAD)
    assert runner.invoke(app, ["down"]).exit_code == 0
    assert calls[-1] == ["down"]
    assert runner.invoke(app, ["down", "--volumes", "--yes"]).exit_code == 0
    assert calls[-1] == ["down", "-v"]
    assert runner.invoke(app, ["logs", "inh-public-api-svc", "-f", "--tail", "20"]).exit_code == 0
    assert calls[-1] == ["logs", "-f", "--tail", "20", "inh-public-api-svc"]


def test_doctor_json_healthy(inherent_home, runner, monkeypatch) -> None:
    load_or_create_compose_env()
    monkeypatch.setattr("inh_cli.stack.preflight_docker", lambda: None)
    monkeypatch.setattr("inh_cli.stack.stack_is_running", lambda *_: True)
    monkeypatch.setattr(
        "inh_cli.stack._health_payload",
        lambda path="/health/ready": (
            200,
            {"status": "healthy", "checks": {"db": {"status": "healthy"}}},
        ),
    )
    monkeypatch.setattr("inh_cli.stack.httpx.get", lambda *a, **k: httpx.Response(200))
    result = runner.invoke(app, ["--json", "doctor"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["status"] == "healthy"


def test_down_volumes_without_tty_requires_yes(inherent_home, runner, monkeypatch) -> None:
    load_or_create_compose_env()
    monkeypatch.setattr("inh_cli.stack.preflight_docker", lambda: None)
    result = runner.invoke(app, ["down", "--volumes"])
    assert result.exit_code == 1
    assert "--yes" in result.output


def test_status_reports_the_engine_version_from_health_ready(
    inherent_home, runner, monkeypatch
) -> None:
    """`/health` is liveness-only: reading `version` off it left the field null."""
    load_or_create_compose_env()
    monkeypatch.setattr("inh_cli.stack.preflight_docker", lambda: None)
    monkeypatch.setattr("inh_cli.stack.compose_ps", lambda **_: PS_PAYLOAD)
    monkeypatch.setattr("inh_cli.stack.stack_is_running", lambda *_: True)

    # Shapes the real service returns for each path.
    payloads = {
        "/health": {"status": "healthy", "service": "inh-public-api-svc"},
        "/health/ready": {
            "status": "healthy",
            "version": "0.7.0",
            "service": "inh-public-api-svc",
            "checks": {},
        },
    }
    monkeypatch.setattr(
        "inh_cli.stack._health_payload", lambda path="/health": (200, payloads[path])
    )

    result = runner.invoke(app, ["--json", "status"])

    assert result.exit_code == 0, result.output
    api = json.loads(result.stdout)["services"][0]
    assert api["engine_version"] == "0.7.0"
    assert api["api_health"] == "healthy"


def test_up_names_the_missing_engine_version(inherent_home, runner, monkeypatch) -> None:
    """`up` defaults the image tag to the CLI version, which may not be published."""
    from inh_cli.client import ClientError

    def fake_run(args, **kwargs):
        raise ClientError(
            "public-api-svc Error manifest unknown: manifest unknown",
            exit_code=1,
        )

    monkeypatch.setattr("inh_cli.stack.preflight_docker", lambda: None)
    monkeypatch.setattr("inh_cli.stack.run_compose", fake_run)

    result = runner.invoke(app, ["up"])

    assert result.exit_code == 1
    assert "No engine images published for version" in result.output
    assert "--engine-version" in result.output


def test_up_does_not_rewrite_unrelated_compose_failures(inherent_home, runner, monkeypatch) -> None:
    from inh_cli.client import ClientError

    def fake_run(args, **kwargs):
        raise ClientError("port is already allocated", exit_code=1)

    monkeypatch.setattr("inh_cli.stack.preflight_docker", lambda: None)
    monkeypatch.setattr("inh_cli.stack.run_compose", fake_run)

    result = runner.invoke(app, ["up"])

    assert result.exit_code == 1
    assert "port is already allocated" in result.output
    assert "No engine images published" not in result.output
