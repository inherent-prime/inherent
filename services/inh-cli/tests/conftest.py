from __future__ import annotations

import os

# Typer forces a Rich terminal when GITHUB_ACTIONS is set, which splits option
# names in --help with ANSI escapes ("-\x1b[0m\x1b[1;36m-json"). The flag is
# read at typer import time, so it must be set before any test imports the app.
os.environ["_TYPER_FORCE_DISABLE_TERMINAL"] = "1"

import pytest  # noqa: E402


@pytest.fixture
def inherent_home(tmp_path, monkeypatch):
    home = tmp_path / "inherent-home"
    home.mkdir()
    monkeypatch.setenv("INHERENT_HOME", str(home))
    monkeypatch.delenv("INHERENT_URL", raising=False)
    monkeypatch.delenv("INHERENT_API_KEY", raising=False)
    return home


@pytest.fixture
def api_env(inherent_home, monkeypatch):
    monkeypatch.setenv("INHERENT_URL", "http://inherent.test")
    monkeypatch.setenv("INHERENT_API_KEY", "ink_test")
    return inherent_home
