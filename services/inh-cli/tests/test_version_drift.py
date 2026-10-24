from __future__ import annotations

import json

from packaging.version import Version
from typer.testing import CliRunner

from inh_cli import __version__
from inh_cli.main import app
from inh_cli.secrets import load_or_create_compose_env
from inh_cli.stack import version_drift_message

# An engine one minor ahead of whatever the CLI currently is, so the drift note
# fires at every release. A hard-coded engine version stopped drifting the moment
# the CLI reached it (v0.8.0 prep).
_CLI = Version(__version__)
ENGINE_AHEAD = f"{_CLI.major}.{_CLI.minor + 1}.0"


def test_version_drift_message_matrix() -> None:
    assert version_drift_message("0.7.0", "1.0.0") == (
        "Warning: CLI 0.7.0 and engine 1.0.0 have different major versions. "
        "Use `inherent up --engine-version <version>` to select an engine image."
    )
    assert version_drift_message("0.7.0", "0.8.0") == "Note: CLI 0.7.0 and engine 0.8.0 differ."
    assert version_drift_message("0.7.0", "0.7.1") is None
    assert version_drift_message("0.7.0", "development") is None
    # PEP 440 prerelease form (no separator before "rc") must still be detected.
    assert version_drift_message("0.7.0rc1", "1.0.0") == (
        "Warning: CLI 0.7.0rc1 and engine 1.0.0 have different major versions. "
        "Use `inherent up --engine-version <version>` to select an engine image."
    )


def test_status_warning_uses_stderr_and_keeps_json_parseable(inherent_home, monkeypatch) -> None:
    load_or_create_compose_env()
    monkeypatch.setattr("inh_cli.stack.preflight_docker", lambda: None)
    monkeypatch.setattr(
        "inh_cli.stack.compose_ps",
        lambda **_: [{"Service": "inh-public-api-svc", "State": "running"}],
    )
    monkeypatch.setattr("inh_cli.stack.stack_is_running", lambda *_: True)
    monkeypatch.setattr(
        "inh_cli.stack._health_payload",
        lambda _: (200, {"status": "healthy", "version": ENGINE_AHEAD, "checks": {}}),
    )

    result = CliRunner().invoke(app, ["--json", "status"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["health"]["version"] == ENGINE_AHEAD
    assert f"CLI {__version__} and engine {ENGINE_AHEAD} differ" in result.stderr
