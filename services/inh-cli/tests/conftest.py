from __future__ import annotations

import pytest


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
