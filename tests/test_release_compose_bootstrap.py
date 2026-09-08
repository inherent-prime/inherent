"""Pins for the checkout-free bootstrap service in release Compose."""

import re
from pathlib import Path

COMPOSE = Path(__file__).resolve().parents[1] / "docker-compose.release.yml"


def _service(name: str, compose: str) -> str:
    """Return one top-level service block without requiring a YAML dependency."""
    match = re.search(rf"(?ms)^  {re.escape(name)}:\n(.*?)(?=^  \S[^\n]*:\n|\Z)", compose)
    assert match, f"missing service: {name}"
    return match.group(1)


def test_release_compose_bootstrap_contract() -> None:
    compose = COMPOSE.read_text()
    bootstrap = _service("bootstrap", compose)
    public_api = _service("inh-public-api-svc", compose)

    assert 'restart: "no"' in bootstrap
    assert "/ingestion-svc:" in bootstrap
    assert "SERVICE_MODE: bootstrap" in bootstrap
    assert re.search(r"BOOTSTRAP_API_KEY:.*\$\{INHERENT_API_KEY:\?", bootstrap)
    assert re.search(
        r"postgres-init:\n\s+condition: service_completed_successfully", bootstrap
    )
    assert re.search(r"bootstrap:\n\s+condition: service_completed_successfully", public_api)


def test_release_compose_enables_local_admin_api() -> None:
    public_api = _service("inh-public-api-svc", COMPOSE.read_text())

    assert 'ADMIN_API_ENABLED: "true"' in public_api
