"""Weaviate naming contract tests (milestone #12, anti-drift).

These assert GOLDEN values for the workspace-collection and user-tenant name
derivations. The public-api side duplicates this logic and asserts the SAME
golden values in its own test, so any future drift on either side fails CI.
"""

import pytest

from src.services.weaviate import get_user_tenant_name, get_workspace_collection_name


@pytest.fixture(autouse=True)
def cleanup_test_data():
    """No-op override of the package-level DB-dependent autouse fixture.

    The package ``tests/conftest.py`` defines an autouse ``cleanup_test_data``
    that depends on ``db_service`` and skips when PostgreSQL is unavailable.
    These tests are pure/offline, so we override it with a no-op (same pattern
    as tests/failure_injection/conftest.py).
    """
    yield


@pytest.mark.parametrize(
    ("workspace_id", "expected"),
    [
        ("ws_local_001", "Workspace_O5ZV63DPMNQWYXZQGAYQ"),
        ("ws-123", "Workspace_O5ZS2MJSGM"),
    ],
)
def test_workspace_collection_name_golden(workspace_id: str, expected: str):
    assert get_workspace_collection_name(workspace_id) == expected


@pytest.mark.parametrize(
    ("user_id", "expected"),
    [
        ("local-dev-user", "User_NRXWGYLMFVSGK5RNOVZWK4Q"),
        ("user_001", "User_OVZWK4S7GAYDC"),
    ],
)
def test_user_tenant_name_golden(user_id: str, expected: str):
    assert get_user_tenant_name(user_id) == expected
