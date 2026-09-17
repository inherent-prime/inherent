from __future__ import annotations

import json
import stat

import httpx
import pytest
from typer.testing import CliRunner

import inh_cli.client as client_mod
from inh_cli.config import Config, save_config
from inh_cli.main import app


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def test_whoami_renders_and_json_passthrough(api_env, inherent_home, runner, monkeypatch) -> None:
    payload = {
        "key_id": "k1",
        "key_name": "Local CLI Key",
        "user_id": "usr",
        "workspace_id": "ws_1",
        "workspace_ids": ["ws_1"],
        "permissions": ["read", "write", "search"],
        "engine_version": "0.7.0",
        "endpoint": "http://localhost:18000",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/whoami"
        return httpx.Response(200, json=payload, request=request)

    monkeypatch.setattr(client_mod, "_transport", httpx.MockTransport(handler))
    human = runner.invoke(app, ["whoami"])
    assert human.exit_code == 0
    assert "Local CLI Key" in human.stdout
    encoded = runner.invoke(app, ["--json", "whoami"])
    assert json.loads(encoded.stdout) == payload


def test_workspaces_uses_admin_when_available(api_env, inherent_home, runner, monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/admin/workspaces"
        return httpx.Response(
            200,
            json=[{"workspace_id": "ws_a", "name": "A", "document_count": 2}],
            request=request,
        )

    monkeypatch.setattr(client_mod, "_transport", httpx.MockTransport(handler))
    result = runner.invoke(app, ["workspaces", "list"])
    assert result.exit_code == 0
    assert "ws_a" in result.stdout


def test_workspaces_fallback_only_on_404(api_env, inherent_home, runner, monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/admin/workspaces":
            return httpx.Response(404, json={"detail": "Not Found"}, request=request)
        assert request.url.path == "/v1/whoami"
        return httpx.Response(
            200,
            json={"workspace_ids": ["ws_own"], "workspace_id": "ws_own"},
            request=request,
        )

    monkeypatch.setattr(client_mod, "_transport", httpx.MockTransport(handler))
    result = runner.invoke(app, ["workspaces", "list"])
    assert result.exit_code == 0
    assert "ws_own" in result.stdout
    assert "admin-gated" in result.stderr or "own workspaces" in result.output


@pytest.mark.parametrize("status", [401, 403, 500])
def test_workspaces_does_not_fallback_on_real_errors(
    api_env, inherent_home, runner, monkeypatch, status: int
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"detail": "nope"}, request=request)

    monkeypatch.setattr(client_mod, "_transport", httpx.MockTransport(handler))
    result = runner.invoke(app, ["workspaces", "list"])
    assert result.exit_code == 1


def test_keys_list_404_is_local_only_message(api_env, inherent_home, runner, monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "Not Found"}, request=request)

    monkeypatch.setattr(client_mod, "_transport", httpx.MockTransport(handler))
    result = runner.invoke(app, ["keys", "list"])
    assert result.exit_code == 1
    assert "ADMIN_API_ENABLED" in result.output


def test_keys_list_never_prints_full_key(api_env, inherent_home, runner, monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {
                    "key_id": "kid",
                    "key_name": "dev",
                    "key_prefix": "ink_abc12345",
                    "workspace_id": "ws",
                    "status": "active",
                }
            ],
            request=request,
        )

    monkeypatch.setattr(client_mod, "_transport", httpx.MockTransport(handler))
    result = runner.invoke(app, ["keys", "list"])
    assert result.exit_code == 0
    assert "ink_" in result.stdout
    assert "ink_abc12345" in result.stdout
    # Full keys are 4 + 43 urlsafe chars; prefixes are 12.
    assert "ink_abc12345xxxx" not in result.stdout


def test_keys_create_prints_ink_and_save_mode(inherent_home, runner, monkeypatch) -> None:
    monkeypatch.setenv("INHERENT_URL", "http://localhost:18000")
    monkeypatch.setenv("INHERENT_API_KEY", "ink_old")
    save_config(Config(url="http://localhost:18000", api_key="ink_old", workspace_id="ws_1"))
    monkeypatch.setattr(
        "inh_cli.commands.identity.require_running_stack", lambda: inherent_home / "compose.env"
    )
    monkeypatch.setattr("inh_cli.commands.identity._require_local_writes", lambda: None)
    (inherent_home / "compose.env").write_text(
        "INHERENT_WORKSPACE_ID=ws_1\nINHERENT_USER_ID=usr_1\n"
    )
    monkeypatch.setattr(
        "inh_cli.commands.identity.run_compose",
        lambda *a, **k: None,
    )
    result = runner.invoke(app, ["keys", "create", "--save"])
    assert result.exit_code == 0, result.output
    assert result.stdout.strip().startswith("ink_")
    saved = (inherent_home / "config.toml").read_text()
    assert "ink_old" not in saved
    assert stat.S_IMODE((inherent_home / "config.toml").stat().st_mode) == 0o600


def test_keys_create_keeps_the_minted_key_out_of_argv(inherent_home, runner, monkeypatch) -> None:
    """argv is world-readable via `ps`; the key may travel only in the env."""

    seen: dict[str, object] = {}

    def fake_run(args, **kwargs):
        seen["args"] = list(args)
        seen["env"] = dict(kwargs.get("env") or {})

    monkeypatch.setenv("INHERENT_URL", "http://localhost:18000")
    monkeypatch.setenv("INHERENT_API_KEY", "ink_old")
    monkeypatch.setattr("inh_cli.commands.identity._require_local_writes", lambda: None)
    monkeypatch.setattr(
        "inh_cli.commands.identity.require_running_stack", lambda: inherent_home / "compose.env"
    )
    (inherent_home / "compose.env").write_text(
        "INHERENT_WORKSPACE_ID=ws_1\nINHERENT_USER_ID=usr_1\n"
    )
    monkeypatch.setattr("inh_cli.commands.identity.run_compose", fake_run)

    result = runner.invoke(app, ["keys", "create"])
    assert result.exit_code == 0, result.output

    minted = result.stdout.strip().splitlines()[-1]
    assert minted.startswith("ink_")
    assert minted == seen["env"]["BOOTSTRAP_API_KEY"]
    assert not any(minted in argument for argument in seen["args"])
    assert "-e" in seen["args"] and "BOOTSTRAP_API_KEY" in seen["args"]


def test_keys_revoke_active_without_force_refuses(inherent_home, runner, monkeypatch) -> None:
    monkeypatch.setenv("INHERENT_URL", "http://localhost:18000")
    monkeypatch.setenv("INHERENT_API_KEY", "ink_live_key_value")
    save_config(
        Config(
            url="http://localhost:18000",
            api_key="ink_live_key_value",
            workspace_id="ws_1",
        )
    )
    monkeypatch.setattr("inh_cli.commands.identity._require_local_writes", lambda: None)
    monkeypatch.setattr(
        "inh_cli.commands.identity.require_running_stack",
        lambda: inherent_home / "compose.env",
    )
    result = runner.invoke(app, ["keys", "revoke", "ink_live_key", "--yes"])
    assert result.exit_code == 1
    assert "--force" in result.output


def test_keys_revoke_with_force(inherent_home, runner, monkeypatch) -> None:
    monkeypatch.setenv("INHERENT_URL", "http://localhost:18000")
    monkeypatch.setenv("INHERENT_API_KEY", "ink_live_key_value")
    save_config(
        Config(
            url="http://localhost:18000",
            api_key="ink_live_key_value",
            workspace_id="ws_1",
        )
    )
    monkeypatch.setattr("inh_cli.commands.identity._require_local_writes", lambda: None)
    monkeypatch.setattr(
        "inh_cli.commands.identity.require_running_stack",
        lambda: inherent_home / "compose.env",
    )
    monkeypatch.setattr("inh_cli.commands.identity.run_compose", lambda *a, **k: None)
    result = runner.invoke(app, ["keys", "revoke", "ink_live_key", "--yes", "--force"])
    assert result.exit_code == 0, result.output
    assert "Revoked" in result.stdout


def test_keys_create_against_remote_exits_2(api_env, inherent_home, runner) -> None:
    result = runner.invoke(app, ["keys", "create"])
    assert result.exit_code == 2
    assert "remote" in result.output
