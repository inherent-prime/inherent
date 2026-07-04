"""Release-compose hardening guard (#3).

The published release stack (``docker-compose.release.yml`` — the artifact users
``curl | docker compose up``) must not ship exploitable defaults:

* no default Postgres password and no default ingestion API key — the stack
  must refuse to start (``:?``) rather than boot with a known credential;
* backing datastores must publish their ports only on loopback, so the
  anonymous-access Weaviate (and Postgres/Mongo/Valkey/S3) are not reachable
  from other hosts on the network.

This is a text-based guard (like ``tests/test_local_postgres_init.py``) so it
needs no YAML dependency and runs in the standard CI test job.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.security

# services/inh-public-api-svc/tests/security/<this> -> repo root is 4 parents up.
REPO_ROOT = Path(__file__).resolve().parents[4]
COMPOSE = (REPO_ROOT / "docker-compose.release.yml").read_text()

# Every backing datastore and its published *host* port. These must never be
# exposed beyond the host in the shipped defaults.
_DATASTORE_HOST_PORTS = {
    "postgres": "15432",
    "mongodb": "27018",
    "weaviate-http": "18080",
    "weaviate-grpc": "15051",
    "valkey": "16379",
    "s3rver": "19000",
}


def test_no_default_postgres_password() -> None:
    """The DB password must not default to 'postgres'; unset must fail-fast."""
    assert (
        "POSTGRES_PASSWORD:-postgres" not in COMPOSE
    ), "release compose still ships a default Postgres password"
    assert (
        "POSTGRES_PASSWORD:?" in COMPOSE
    ), "POSTGRES_PASSWORD must use ':?' so an unset value refuses to start"


def test_no_default_ingestion_api_key() -> None:
    """The ingestion API key must not default to a shipped literal."""
    assert (
        "dev-ingestion-key" not in COMPOSE
    ), "release compose still ships the known 'dev-ingestion-key' default"
    assert (
        "INGESTION_API_KEY:?" in COMPOSE
    ), "INGESTION_API_KEY must use ':?' so an unset value refuses to start"


@pytest.mark.parametrize("name,host_port", _DATASTORE_HOST_PORTS.items())
def test_datastore_ports_bound_to_loopback(name: str, host_port: str) -> None:
    """Each datastore's published port must bind to 127.0.0.1 only."""
    assert f'"127.0.0.1:{host_port}:' in COMPOSE, (
        f"datastore '{name}' host port {host_port} is not loopback-bound "
        f"(reachable from other hosts)"
    )


def test_weaviate_requires_api_key_not_anonymous() -> None:
    """Weaviate must not run anonymous in the release stack (#3 follow-up)."""
    assert 'AUTHENTICATION_ANONYMOUS_ACCESS_ENABLED: "false"' in COMPOSE
    assert 'AUTHENTICATION_APIKEY_ENABLED: "true"' in COMPOSE
    # The key is required (fail-fast) both at the DB and passed to the services.
    assert "WEAVIATE_API_KEY:?" in COMPOSE
