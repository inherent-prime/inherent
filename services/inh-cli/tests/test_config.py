import stat

import pytest

from inh_cli.config import Config, StackNotConfigured, load_config, resolve, save_config


def test_config_round_trip_respects_inherent_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    home = tmp_path / "custom-home"
    monkeypatch.setenv("INHERENT_HOME", str(home))
    config = Config(
        url="http://localhost:18000",
        api_key="ink_local",
        workspace_id="ws_local",
        engine_version="0.7.0",
    )

    save_config(config)

    assert load_config() == config
    assert stat.S_IMODE((home / "config.toml").stat().st_mode) == 0o600


def test_environment_overrides_file(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("INHERENT_HOME", str(tmp_path))
    save_config(Config(url="http://file", api_key="ink_file", workspace_id="ws_file"))
    monkeypatch.setenv("INHERENT_URL", "http://env")
    monkeypatch.setenv("INHERENT_API_KEY", "ink_env")

    resolved = resolve()

    assert resolved.url == "http://env"
    assert resolved.api_key == "ink_env"
    assert resolved.workspace_id == "ws_file"


def test_file_only_config_works(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("INHERENT_HOME", str(tmp_path))
    save_config(Config(url="http://file", api_key="ink_file"))

    assert resolve().url == "http://file"


def test_explicit_connection_works_without_a_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("INHERENT_HOME", str(tmp_path))

    resolved = resolve("http://explicit/", "ink_explicit")

    assert resolved.url == "http://explicit"
    assert resolved.api_key == "ink_explicit"


def test_missing_config_has_stack_not_running_exit_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("INHERENT_HOME", str(tmp_path))
    monkeypatch.delenv("INHERENT_URL", raising=False)
    monkeypatch.delenv("INHERENT_API_KEY", raising=False)

    with pytest.raises(StackNotConfigured) as error:
        resolve()

    assert error.value.exit_code == 2
