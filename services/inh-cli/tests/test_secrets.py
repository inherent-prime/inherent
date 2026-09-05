from __future__ import annotations

import re
import stat

from inh_cli.secrets import load_or_create_compose_env, parse_env_file


def test_generates_ink_key_and_0600(inherent_home) -> None:
    path, values = load_or_create_compose_env()
    assert path == inherent_home / "compose.env"
    assert re.match(r"^ink_", values["INHERENT_API_KEY"])
    assert values["INHERENT_WORKSPACE_ID"].startswith("ws_")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    for key in (
        "POSTGRES_PASSWORD",
        "WEAVIATE_API_KEY",
        "INGESTION_API_KEY",
        "INHERENT_API_KEY",
        "INHERENT_WORKSPACE_ID",
        "INHERENT_USER_ID",
    ):
        assert values[key]


def test_second_run_does_not_regenerate(inherent_home) -> None:
    path, _ = load_or_create_compose_env()
    original = path.read_bytes()
    load_or_create_compose_env()
    assert path.read_bytes() == original
    assert parse_env_file(path)["INHERENT_API_KEY"].startswith("ink_")
