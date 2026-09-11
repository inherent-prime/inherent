from __future__ import annotations

import json
import subprocess
from unittest.mock import patch

import pytest

from inh_cli.client import ClientError
from inh_cli.compose import (
    bundled_compose_path,
    compose_argv,
    parse_compose_ps,
    preflight_docker,
)


def test_compose_argv_is_exact(inherent_home) -> None:
    env_file = inherent_home / "compose.env"
    argv = compose_argv("up", "-d", "--wait", env_file=env_file)
    assert argv == [
        "docker",
        "compose",
        "-p",
        "inherent",
        "-f",
        str(bundled_compose_path()),
        "--env-file",
        str(env_file),
        "up",
        "-d",
        "--wait",
    ]


def test_bundled_compose_exists() -> None:
    path = bundled_compose_path()
    assert path.is_file()
    assert "services:" in path.read_text(encoding="utf-8")


def test_parse_compose_ps_jsonl() -> None:
    payload = (
        json.dumps({"Service": "postgres", "State": "running"})
        + "\n"
        + json.dumps({"Service": "weaviate", "State": "running"})
        + "\n"
    )
    rows = parse_compose_ps(payload)
    assert [row["Service"] for row in rows] == ["postgres", "weaviate"]


def test_parse_compose_ps_array() -> None:
    payload = json.dumps([{"Service": "postgres", "State": "running"}])
    assert parse_compose_ps(payload)[0]["Service"] == "postgres"


def test_missing_docker_is_actionable() -> None:
    with patch("inh_cli.compose.subprocess.run", side_effect=FileNotFoundError):
        with pytest.raises(ClientError, match="Docker is not installed") as error:
            preflight_docker()
    assert error.value.exit_code == 1
    assert "https://docs.docker.com/get-docker/" in str(error.value)


def test_daemon_down_is_actionable() -> None:
    with patch(
        "inh_cli.compose.subprocess.run",
        return_value=subprocess.CompletedProcess(["docker"], 1, stdout="", stderr="Cannot connect"),
    ):
        with pytest.raises(ClientError, match="Docker daemon is not running") as error:
            preflight_docker()
    assert error.value.exit_code == 1


def test_run_compose_missing_docker() -> None:
    from inh_cli.compose import run_compose

    with patch("inh_cli.compose.subprocess.run", side_effect=FileNotFoundError):
        with pytest.raises(ClientError, match="Docker is not installed"):
            run_compose(["ps"])


def test_run_compose_nonzero_raises() -> None:
    from inh_cli.compose import run_compose

    with patch(
        "inh_cli.compose.subprocess.run",
        return_value=subprocess.CompletedProcess(["docker"], 1, stdout="", stderr="boom"),
    ):
        with pytest.raises(ClientError, match="boom"):
            run_compose(["down"])


def test_compose_ps_empty_on_failure(inherent_home) -> None:
    from inh_cli.compose import compose_ps

    with patch(
        "inh_cli.compose.run_compose",
        return_value=subprocess.CompletedProcess(["docker"], 1, stdout="", stderr="x"),
    ):
        assert compose_ps() == []


def test_require_running_stack_exit_2(inherent_home) -> None:
    from inh_cli.compose import require_running_stack

    with pytest.raises(ClientError) as error:
        require_running_stack()
    assert error.value.exit_code == 2


def test_stack_is_running_from_rows() -> None:
    from inh_cli.compose import stack_is_running

    assert stack_is_running([{"State": "running"}])
    assert not stack_is_running([{"State": "exited"}])


def test_parse_compose_ps_empty() -> None:
    assert parse_compose_ps("") == []


def test_compose_v1_is_rejected() -> None:
    def fake_run(argv, **kwargs):
        if argv[:3] == ["docker", "version", "--format"]:
            return subprocess.CompletedProcess(argv, 0, stdout="28.0.0\n", stderr="")
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="unknown command")

    with patch("inh_cli.compose.subprocess.run", side_effect=fake_run):
        with pytest.raises(ClientError, match="Compose v2") as error:
            preflight_docker()
    assert error.value.exit_code == 1
