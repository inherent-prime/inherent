"""Compose-marked smoke: real up → whoami → down. Opt-in, skipped in default pytest."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from inh_cli.main import app

pytestmark = pytest.mark.compose


def test_up_whoami_down_cycle() -> None:
    runner = CliRunner()
    up = runner.invoke(app, ["up"])
    assert up.exit_code == 0, up.output
    whoami = runner.invoke(app, ["--json", "whoami"])
    assert whoami.exit_code == 0, whoami.output
    down = runner.invoke(app, ["down", "--volumes", "--yes"])
    assert down.exit_code == 0, down.output
