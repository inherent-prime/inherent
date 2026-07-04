"""Golden naming tests — the package is the source of truth (#12).

Names are derived by an *injective* (collision-free) encoding of the raw id, so
two ids that differ only in punctuation can never map to the same Weaviate
collection/tenant. A lossy strip (the old behaviour) let ``ws-123`` / ``ws_123``
/ ``ws123`` all collapse onto one tenant — a cross-tenant leak (#1).
"""

import re

import pytest

from inh_contracts import CONTRACT_VERSION
from inh_contracts.naming import get_user_tenant_name, get_workspace_collection_name


@pytest.mark.parametrize(
    ("workspace_id", "expected"),
    [
        ("ws_local_001", "Workspace_O5ZV63DPMNQWYXZQGAYQ"),
        ("ws-123", "Workspace_O5ZS2MJSGM"),
    ],
)
def test_workspace_collection_name_golden(workspace_id: str, expected: str) -> None:
    assert get_workspace_collection_name(workspace_id) == expected


@pytest.mark.parametrize(
    ("user_id", "expected"),
    [
        ("local-dev-user", "User_NRXWGYLMFVSGK5RNOVZWK4Q"),
        ("user_001", "User_OVZWK4S7GAYDC"),
    ],
)
def test_user_tenant_name_golden(user_id: str, expected: str) -> None:
    assert get_user_tenant_name(user_id) == expected


# --- Injectivity: the actual security property (#1) -----------------------

# IDs that the OLD strip-based derivation collapsed onto ONE name.
_COLLIDING_WORKSPACE_IDS = ["ws-123", "ws_123", "ws123", "w-s123", "ws.123"]
_COLLIDING_USER_IDS = ["a-b", "a_b", "ab", "a.b", "a b"]


def test_distinct_workspace_ids_never_collide() -> None:
    names = {get_workspace_collection_name(i) for i in _COLLIDING_WORKSPACE_IDS}
    assert len(names) == len(_COLLIDING_WORKSPACE_IDS), (
        f"collision: {_COLLIDING_WORKSPACE_IDS} -> {names}"
    )


def test_distinct_user_ids_never_collide() -> None:
    names = {get_user_tenant_name(i) for i in _COLLIDING_USER_IDS}
    assert len(names) == len(_COLLIDING_USER_IDS), (
        f"collision: {_COLLIDING_USER_IDS} -> {names}"
    )


# --- Validity: names must satisfy Weaviate's charset rules ----------------

_COLLECTION_RE = re.compile(r"^[A-Z][_0-9A-Za-z]*$")  # Weaviate class name
_TENANT_RE = re.compile(r"^[A-Za-z0-9_-]+$")  # Weaviate tenant name


@pytest.mark.parametrize(
    "raw_id",
    ["ws-123", "ws_local_001", "WS/With\\Odd:chars", "工作区", "a.b c", "x" * 60],
)
def test_workspace_name_is_weaviate_valid(raw_id: str) -> None:
    assert _COLLECTION_RE.match(get_workspace_collection_name(raw_id))


@pytest.mark.parametrize(
    "raw_id",
    ["local-dev-user", "user_001", "user/with:odd", "用户", "a.b c", "y" * 60],
)
def test_tenant_name_is_weaviate_valid(raw_id: str) -> None:
    assert _TENANT_RE.match(get_user_tenant_name(raw_id))


def test_contract_version_is_pinned() -> None:
    assert CONTRACT_VERSION == "1.0.0"
