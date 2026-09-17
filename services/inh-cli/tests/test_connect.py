from __future__ import annotations

import json
import stat
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from inh_cli.commands.connect import inherent_server_block
from inh_cli.main import app


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def test_print_emits_fixture_and_touches_nothing(api_env, inherent_home, runner, tmp_path) -> None:
    decoy = tmp_path / "readonly"
    decoy.mkdir()
    config = decoy / "mcp.json"
    result = runner.invoke(app, ["connect", "claude", "--print", "--config-path", str(config)])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    expected = {
        "mcpServers": {"inherent": inherent_server_block("http://inherent.test", "ink_test")}
    }
    assert payload == expected
    assert not config.exists()
    assert list(decoy.iterdir()) == []


def test_print_uses_inherent_url(monkeypatch, inherent_home, runner, tmp_path) -> None:
    monkeypatch.setenv("INHERENT_URL", "https://api.example.com")
    monkeypatch.setenv("INHERENT_API_KEY", "ink_remote")
    result = runner.invoke(app, ["connect", "cursor", "--print"])
    payload = json.loads(result.stdout)
    assert payload["mcpServers"]["inherent"]["url"] == "https://api.example.com/mcp"


def test_preserves_unrelated_keys(api_env, inherent_home, runner, tmp_path, monkeypatch) -> None:
    config = tmp_path / "mcp.json"
    original = {
        "theme": "dark",
        "mcpServers": {"other": {"command": "npx", "args": ["x"]}},
    }
    config.write_text(json.dumps(original, indent=2) + "\n")
    monkeypatch.setattr("inh_cli.commands.connect._verify_mcp", lambda *a, **k: True)
    result = runner.invoke(app, ["connect", "cursor", "--config-path", str(config), "--force"])
    assert result.exit_code == 0, result.output
    written = json.loads(config.read_text())
    assert written["theme"] == "dark"
    assert written["mcpServers"]["other"] == {"command": "npx", "args": ["x"]}
    assert "inherent" in written["mcpServers"]


def test_second_write_is_idempotent(api_env, inherent_home, runner, tmp_path, monkeypatch) -> None:
    config = tmp_path / "mcp.json"
    monkeypatch.setattr("inh_cli.commands.connect._verify_mcp", lambda *a, **k: True)
    first = runner.invoke(app, ["connect", "claude", "--config-path", str(config)])
    assert first.exit_code == 0, first.output
    bytes_after_first = config.read_bytes()
    second = runner.invoke(app, ["connect", "claude", "--config-path", str(config)])
    assert second.exit_code == 0, second.output
    assert config.read_bytes() == bytes_after_first


def test_existing_entry_without_force_leaves_file(api_env, inherent_home, runner, tmp_path) -> None:
    config = tmp_path / "mcp.json"
    original = {
        "mcpServers": {
            "inherent": {"type": "http", "url": "http://old/mcp", "headers": {"X-API-Key": "x"}}
        }
    }
    config.write_text(json.dumps(original))
    result = runner.invoke(app, ["connect", "claude", "--config-path", str(config)], input="n\n")
    assert result.exit_code == 1
    assert json.loads(config.read_text()) == original


def test_malformed_config_exits_without_write(api_env, inherent_home, runner, tmp_path) -> None:
    config = tmp_path / "mcp.json"
    config.write_text("{not json")
    original = config.read_bytes()
    result = runner.invoke(app, ["connect", "claude", "--config-path", str(config)])
    assert result.exit_code == 1
    assert config.read_bytes() == original


def test_backup_matches_original(api_env, inherent_home, runner, tmp_path, monkeypatch) -> None:
    config = tmp_path / "mcp.json"
    original = json.dumps({"mcpServers": {"other": {"url": "x"}}})
    config.write_text(original)
    monkeypatch.setattr("inh_cli.commands.connect._verify_mcp", lambda *a, **k: True)
    result = runner.invoke(app, ["connect", "claude", "--config-path", str(config)])
    assert result.exit_code == 0, result.output
    backups = list(tmp_path.glob("mcp.json.bak-*"))
    assert len(backups) == 1
    assert backups[0].read_text() == original


def test_written_file_is_0600(api_env, inherent_home, runner, tmp_path, monkeypatch) -> None:
    config = tmp_path / "mcp.json"
    monkeypatch.setattr("inh_cli.commands.connect._verify_mcp", lambda *a, **k: True)
    runner.invoke(app, ["connect", "claude", "--config-path", str(config)])
    assert stat.S_IMODE(config.stat().st_mode) == 0o600


def test_verify_mcp_success_and_failure(monkeypatch) -> None:
    from inh_cli.commands.connect import _verify_mcp

    monkeypatch.setattr(
        "inh_cli.commands.connect.httpx.post",
        lambda *a, **k: httpx.Response(200),
    )
    assert _verify_mcp("http://localhost:18000", "ink_x") is True
    monkeypatch.setattr(
        "inh_cli.commands.connect.httpx.post",
        lambda *a, **k: (_ for _ in ()).throw(httpx.ConnectError("nope")),
    )
    assert _verify_mcp("http://localhost:18000", "ink_x") is False


def test_claude_and_cursor_default_paths(monkeypatch, tmp_path) -> None:
    from inh_cli.commands.connect import TARGETS, agent_present

    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-dir"))
    assert TARGETS["claude"].path_for() == tmp_path / "claude-dir" / ".claude.json"
    monkeypatch.setattr("inh_cli.commands.connect.shutil.which", lambda _: None)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert TARGETS["cursor"].path_for() == tmp_path / ".cursor" / "mcp.json"
    assert agent_present(TARGETS["cursor"]) is False
    (tmp_path / ".cursor").mkdir()
    assert agent_present(TARGETS["cursor"]) is True


def test_unknown_agent(api_env, inherent_home, runner) -> None:
    result = runner.invoke(app, ["connect", "windsurf", "--print"])
    assert result.exit_code == 1
    assert "Unknown agent" in result.output


def test_missing_directory_message(api_env, inherent_home, runner, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("inh_cli.commands.connect.agent_present", lambda target: False)
    missing = tmp_path / "not-installed" / "mcp.json"
    result = runner.invoke(app, ["connect", "claude", "--config-path", str(missing)])
    assert result.exit_code == 1
    assert "not found" in result.output
    assert "--print" in result.output
    assert not missing.exists()


def test_rerun_against_the_same_stack_writes_no_new_backup(
    api_env, inherent_home, runner, tmp_path, monkeypatch
) -> None:
    """Every run used to leave another backup, each holding a plaintext key."""
    monkeypatch.setattr("inh_cli.commands.connect._verify_mcp", lambda *_: True)
    config = tmp_path / ".claude.json"
    config.write_text("{}")

    first = runner.invoke(app, ["connect", "claude", "--config-path", str(config)])
    assert first.exit_code == 0, first.output
    after_first = sorted(p.name for p in tmp_path.glob(".claude.json.bak-*"))

    second = runner.invoke(app, ["connect", "claude", "--config-path", str(config)])

    assert second.exit_code == 0, second.output
    assert sorted(p.name for p in tmp_path.glob(".claude.json.bak-*")) == after_first
    assert "already points at this stack" in second.output
    # The entry is still there and still correct.
    entry = json.loads(config.read_text())["mcpServers"]["inherent"]
    assert entry["url"].endswith("/mcp")
