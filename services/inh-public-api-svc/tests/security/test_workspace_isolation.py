"""Workspace isolation regression tests (#32).

Guards ``_resolve_workspace`` (the shared core behind ``resolve_workspace_read``
/ ``resolve_workspace_search`` / ``resolve_workspace_write``): a user must never
be able to resolve a workspace that is not in their authorised set.

Two DIFFERENT DB calls back this now (#138 blocker-2 fix), and tests must
mock the right one for what they're exercising:

- A *user-scoped* key's authorised set comes from ``get_user_workspace_ids``
  (Mongo UNION Postgres upload-history fallback) — mock via
  ``_patch_user_workspaces``.
- A *workspace-scoped* key's binding is validated against
  ``user_owns_workspace_in_mongo`` — a MONGO-ONLY membership check,
  deliberately NOT the union above (see ``database.py`` for why: the union's
  Postgres fallback would keep re-granting a transferred workspace via its
  own stale upload-history rows) — mock via ``_patch_mongo_ownership``.
  Mocking ``get_user_workspace_ids`` for a scoped-key test is a no-op: that
  function is never called on the scoped-key path.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException, status

from src.models.api_key import APIKeyInfo
from src.services.auth import _resolve_workspace

pytestmark = pytest.mark.security


def _user_key(permissions: list[str] | None = None) -> APIKeyInfo:
    """A user-scoped key (workspace_id=None) — access is driven purely by the
    user's authorised workspace set, which is the riskiest path."""
    return APIKeyInfo(
        key_id="key-user",
        user_id="user-1",
        workspace_id=None,
        permissions=permissions or ["read", "search", "write"],
        rate_limit=100,
        expires_at=None,
        status="active",
    )


def _patch_user_workspaces(ws_ids: list[str], *, owns_binding: bool = True):
    """Patch the DB for a ``_resolve_workspace`` call.

    ``ws_ids`` backs ``get_user_workspace_ids`` (the user-scoped-key path).
    ``owns_binding`` backs ``user_owns_workspace_in_mongo`` (the
    workspace-scoped-key path) — defaults to True so tests that only care
    about a DIFFERENT branch (e.g. a header mismatch, which raises before the
    ownership check ever runs) don't need to think about it. Tests that
    specifically exercise a scoped key's ownership check should pass
    ``owns_binding`` explicitly rather than relying on this default.
    """
    mock_db = AsyncMock()
    mock_db.get_user_workspace_ids = AsyncMock(return_value=ws_ids)
    mock_db.user_owns_workspace_in_mongo = AsyncMock(return_value=owns_binding)
    return patch("src.services.auth.get_database", AsyncMock(return_value=mock_db))


def _patch_mongo_ownership(owns: bool):
    """Patch the DB so ``user_owns_workspace_in_mongo`` returns *owns* — the
    workspace-scoped-key path's ONLY authorization input (#138 blocker-2).
    Does not configure ``get_user_workspace_ids`` at all, since a
    correctly-implemented scoped-key check must never call it.
    """
    mock_db = AsyncMock()
    mock_db.user_owns_workspace_in_mongo = AsyncMock(return_value=owns)
    return patch("src.services.auth.get_database", AsyncMock(return_value=mock_db))


@pytest.mark.asyncio
async def test_request_for_unauthorised_workspace_is_forbidden() -> None:
    """A workspace NOT in the user's set, requested via header → 403."""
    key = _user_key()
    with _patch_user_workspaces(["ws-owned"]):
        with pytest.raises(HTTPException) as exc_info:
            await _resolve_workspace(key, "ws-someone-else", required=False)
    assert exc_info.value.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.asyncio
async def test_multi_workspace_user_can_access_each_owned_workspace() -> None:
    """A multi-workspace user can resolve any workspace in their own set."""
    key = _user_key()
    owned = ["ws-a", "ws-b", "ws-c"]
    with _patch_user_workspaces(owned):
        for ws in owned:
            resolved = await _resolve_workspace(key, ws, required=False)
            assert resolved.workspace_id == ws


@pytest.mark.asyncio
async def test_multi_workspace_user_cannot_access_foreign_workspace() -> None:
    """The same multi-workspace user is still blocked from a workspace outside
    their set — owning several workspaces must not grant access to all."""
    key = _user_key()
    with _patch_user_workspaces(["ws-a", "ws-b", "ws-c"]):
        with pytest.raises(HTTPException) as exc_info:
            await _resolve_workspace(key, "ws-foreign", required=False)
    assert exc_info.value.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.asyncio
async def test_workspace_scoped_key_cannot_cross_to_another_workspace() -> None:
    """A key scoped to one workspace cannot request a different workspace it
    does not own — rejected on the header-mismatch check, before the
    ownership check ever runs (owns_binding=True here isolates that: this
    test is NOT about the binding being stale)."""
    key = APIKeyInfo(
        key_id="key-ws",
        user_id="user-1",
        workspace_id="ws-scoped",
        permissions=["read", "search"],
        rate_limit=100,
        expires_at=None,
        status="active",
    )
    with _patch_mongo_ownership(owns=True):
        with pytest.raises(HTTPException) as exc_info:
            await _resolve_workspace(key, "ws-other", required=False)
    assert exc_info.value.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.asyncio
async def test_workspace_scoped_key_cannot_cross_even_to_an_owned_workspace() -> None:
    """A workspace-scoped key must stay bound to its workspace even when the
    owning *user* also owns the target workspace.

    Regression for the ``X-Workspace-Id`` scope-escape: a key deliberately
    scoped to ws-A (e.g. handed to a limited integration) must not be usable
    against ws-B just because the key's owner happens to own ws-B too —
    otherwise the key-level scope degrades to "any workspace the user owns".
    Rejected on the header-mismatch check (owns_binding=True isolates the
    ownership check, which is not what this test is about).
    """
    key = APIKeyInfo(
        key_id="key-ws",
        user_id="user-1",
        workspace_id="ws-a",  # key is scoped to ws-a only
        permissions=["read", "search"],
        rate_limit=100,
        expires_at=None,
        status="active",
    )
    with _patch_mongo_ownership(owns=True):
        with pytest.raises(HTTPException) as exc_info:
            await _resolve_workspace(key, "ws-b", required=False)
    assert exc_info.value.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.asyncio
async def test_workspace_scoped_key_with_matching_header_resolves() -> None:
    """A scoped key with a header naming its own workspace, and whose binding
    Mongo confirms is still owned, resolves fine."""
    key = APIKeyInfo(
        key_id="key-ws",
        user_id="user-1",
        workspace_id="ws-a",
        permissions=["read", "search"],
        rate_limit=100,
        expires_at=None,
        status="active",
    )
    with _patch_mongo_ownership(owns=True):
        resolved = await _resolve_workspace(key, "ws-a", required=False)
    assert resolved.workspace_id == "ws-a"


@pytest.mark.asyncio
async def test_workspace_scoped_key_ignores_absent_header() -> None:
    """A scoped key with no header, whose binding Mongo confirms is still
    owned, resolves to its bound workspace."""
    key = APIKeyInfo(
        key_id="key-ws",
        user_id="user-1",
        workspace_id="ws-a",
        permissions=["read", "search"],
        rate_limit=100,
        expires_at=None,
        status="active",
    )
    with _patch_mongo_ownership(owns=True):
        resolved = await _resolve_workspace(key, None, required=False)
    assert resolved.workspace_id == "ws-a"


@pytest.mark.asyncio
async def test_resolve_uses_only_authorised_set_for_default() -> None:
    """With no header and a single owned workspace, the resolved workspace is
    exactly that owned one (never a foreign id)."""
    key = _user_key()
    with _patch_user_workspaces(["ws-only"]):
        resolved = await _resolve_workspace(key, None, required=False)
    assert resolved.workspace_id == "ws-only"


@pytest.mark.asyncio
async def test_denied_workspace_access_is_logged_with_attempted_id() -> None:
    """A 403 for a user-scoped key must emit a diagnostic warning carrying the
    attempted workspace id and the user's authorised set, so support can tell a
    wrong-id paste (e.g. Clerk org_id) from a real ownership gap without DB access.
    """
    key = _user_key()
    with _patch_user_workspaces(["ws-owned"]):
        with patch("src.services.auth.logger") as mock_logger:
            with pytest.raises(HTTPException):
                await _resolve_workspace(key, "user_ClerkOrgIdPasted", required=False)
    mock_logger.warning.assert_called_once()
    _, kwargs = mock_logger.warning.call_args
    assert kwargs["requested_workspace_id"] == "user_ClerkOrgIdPasted"
    assert kwargs["authorised_workspace_ids"] == ["ws-owned"]
    assert kwargs["user_id"] == "user-1"


@pytest.mark.asyncio
async def test_scoped_key_mismatch_is_logged_with_attempted_id() -> None:
    """A 403 from a workspace-scoped key requesting a different workspace logs
    the key's binding and the requested id for the same diagnostic reason.

    DB is mocked so the binding IS still owned (owns_binding=True) — this
    isolates the header-mismatch branch from the separate stale-binding
    branch below it; the point of this test is the header mismatch, not the
    ownership check.
    """
    key = APIKeyInfo(
        key_id="key-ws",
        user_id="user-1",
        workspace_id="ws-scoped",
        permissions=["read", "search"],
        rate_limit=100,
        expires_at=None,
        status="active",
    )
    with _patch_mongo_ownership(owns=True):
        with patch("src.services.auth.logger") as mock_logger:
            with pytest.raises(HTTPException):
                await _resolve_workspace(key, "ws-other", required=False)
    mock_logger.warning.assert_called_once()
    _, kwargs = mock_logger.warning.call_args
    assert kwargs["requested_workspace_id"] == "ws-other"
    assert kwargs["key_workspace_id"] == "ws-scoped"


@pytest.mark.asyncio
async def test_scoped_key_with_deleted_binding_fails_closed() -> None:
    """#138 blocker-2: a scoped key whose bound workspace the owner no longer
    owns (deleted/transferred in Mongo, the CANONICAL ownership source) must
    be rejected, not silently served. Mongo says the binding is NOT owned
    (``user_owns_workspace_in_mongo`` returns False) — this is the ONLY input
    the scoped-key check consults; trusting ``key_info.workspace_id``
    unconditionally would have let a stale binding through even with no
    header requesting anything different."""
    key = APIKeyInfo(
        key_id="key-ws",
        user_id="user-1",
        workspace_id="ws-revoked",
        permissions=["read", "search"],
        rate_limit=100,
        expires_at=None,
        status="active",
    )
    with _patch_mongo_ownership(owns=False):
        with pytest.raises(HTTPException) as exc_info:
            await _resolve_workspace(key, None, required=False)
    assert exc_info.value.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.asyncio
async def test_scoped_key_with_no_owned_workspaces_fails_closed() -> None:
    """Fail-closed edge case: Mongo says the user owns NOTHING at all (every
    workspace deleted/transferred). A scoped key must still be rejected,
    never fall back to an empty "search everything" or crash on an
    out-of-range index."""
    key = APIKeyInfo(
        key_id="key-ws",
        user_id="user-1",
        workspace_id="ws-revoked",
        permissions=["read", "search"],
        rate_limit=100,
        expires_at=None,
        status="active",
    )
    with _patch_mongo_ownership(owns=False):
        with pytest.raises(HTTPException) as exc_info:
            await _resolve_workspace(key, None, required=False)
    assert exc_info.value.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.asyncio
async def test_scoped_key_binding_check_never_consults_the_union_helper() -> None:
    """#138 blocker-2 (the actual regression): a scoped key's binding
    validation must call ``user_owns_workspace_in_mongo`` and must NEVER call
    ``get_user_workspace_ids`` — the union helper that includes Postgres
    upload history. This is the mistake the previous round made: intersecting
    against the union re-admitted a workspace transferred away from its owner
    whenever the owner had ever uploaded to it (the realistic case, since a
    workspace worth protecting has content). Configuring ONLY
    ``user_owns_workspace_in_mongo`` (no ``get_user_workspace_ids`` mock at
    all) and asserting a successful resolution proves the union path is
    never touched — if it were, the unconfigured AsyncMock would still
    "work" (return a MagicMock, which is truthy in an `in` check only by
    accident), but the awaited-call assertion below would still catch it.
    """
    key = APIKeyInfo(
        key_id="key-ws",
        user_id="user-1",
        workspace_id="ws-a",
        permissions=["read", "search"],
        rate_limit=100,
        expires_at=None,
        status="active",
    )
    mock_db = AsyncMock()
    mock_db.user_owns_workspace_in_mongo = AsyncMock(return_value=True)
    with patch("src.services.auth.get_database", AsyncMock(return_value=mock_db)):
        resolved = await _resolve_workspace(key, None, required=False)
    assert resolved.workspace_id == "ws-a"
    mock_db.user_owns_workspace_in_mongo.assert_awaited_once_with("user-1", "ws-a")
    mock_db.get_user_workspace_ids.assert_not_awaited()
