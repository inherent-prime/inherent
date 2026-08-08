"""Weaviate naming contract test (#12) — anti-drift.

The public API and the ingestion service both derive Weaviate collection and
tenant names by calling the single shared implementation in
``inh_contracts.naming`` (base32-encoded, injective — see ADR 0002's
2026-07-04 amendment and #1). They MUST still agree byte-for-byte with each
other and with the ingestion side's own pin
(``services/inh-ingestion-svc/tests/test_naming_contract.py``) — these GOLDEN
assertions are pinned independently on both services (belt-and-suspenders
against the shared package itself regressing); if either drifts from these
values, CI fails.
"""

from __future__ import annotations

import pytest

from src.services.search import _get_user_tenant_name, _get_workspace_collection_name

# Part of the selectable contract-regression surface (M6 #30).
pytestmark = [pytest.mark.contract]


@pytest.mark.parametrize(
    ("workspace_id", "expected"),
    [
        ("ws_local_001", "Workspace_O5ZV63DPMNQWYXZQGAYQ"),
        ("ws-123", "Workspace_O5ZS2MJSGM"),
    ],
)
def test_workspace_collection_name_golden(workspace_id: str, expected: str) -> None:
    assert _get_workspace_collection_name(workspace_id) == expected


@pytest.mark.parametrize(
    ("user_id", "expected"),
    [
        ("local-dev-user", "User_NRXWGYLMFVSGK5RNOVZWK4Q"),
        ("user_001", "User_OVZWK4S7GAYDC"),
    ],
)
def test_user_tenant_name_golden(user_id: str, expected: str) -> None:
    assert _get_user_tenant_name(user_id) == expected
