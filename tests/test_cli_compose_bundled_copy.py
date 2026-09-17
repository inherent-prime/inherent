"""The compose file inside inh-cli must stay byte-identical to the repo copy."""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
REPO_COMPOSE = REPO_ROOT / "docker-compose.release.yml"
PACKAGED_COMPOSE = (
    REPO_ROOT
    / "services"
    / "inh-cli"
    / "src"
    / "inh_cli"
    / "data"
    / "docker-compose.release.yml"
)


def test_packaged_compose_matches_repo_copy() -> None:
    assert REPO_COMPOSE.is_file()
    assert PACKAGED_COMPOSE.is_file()
    assert PACKAGED_COMPOSE.read_bytes() == REPO_COMPOSE.read_bytes()
