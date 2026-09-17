"""Live local-stack identity/admin smoke contract (#278, #279)."""

import os

import httpx
import pytest

pytestmark = [pytest.mark.compose, pytest.mark.integration]

API_URL = os.environ.get("PUBLIC_API_URL", "http://localhost:18000").rstrip("/")
API_KEY = os.environ.get("INTEGRATION_API_KEY", "ink_dev_local_key_001")


def test_live_identity_and_admin_surfaces() -> None:
    """Prove compose enables admin and seeded identity flows through real storage."""
    try:
        health = httpx.get(f"{API_URL}/health", timeout=5)
    except httpx.HTTPError as exc:
        pytest.skip(f"public API not reachable at {API_URL}: {exc}")
    if health.status_code != 200:
        pytest.skip(f"public API unhealthy at {API_URL}: HTTP {health.status_code}")

    headers = {"X-API-Key": API_KEY}
    whoami = httpx.get(f"{API_URL}/v1/whoami", headers=headers, timeout=10)
    workspaces = httpx.get(f"{API_URL}/v1/admin/workspaces", headers=headers, timeout=10)
    keys = httpx.get(f"{API_URL}/v1/admin/keys", headers=headers, timeout=10)

    assert whoami.status_code == workspaces.status_code == keys.status_code == 200
    assert whoami.json()["workspace_id"] in whoami.json()["workspace_ids"]
    assert whoami.json()["workspace_id"] in {row["workspace_id"] for row in workspaces.json()}
    assert whoami.json()["key_id"] in {row["key_id"] for row in keys.json()}
    assert "key_hash" not in keys.text
