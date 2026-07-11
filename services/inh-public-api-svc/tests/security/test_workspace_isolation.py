"""Workspace isolation regression tests (#32).

Guards ``_resolve_workspace`` (the shared core behind ``resolve_workspace_read``
/ ``resolve_workspace_search`` / ``resolve_workspace_write``): a user must never
be able to resolve a workspace that is not in their authorised set
(``get_user_workspace_ids``).
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


def _patch_user_workspaces(ws_ids: list[str]):
    """Patch the DB so get_user_workspace_ids returns *ws_ids*."""
    mock_db = AsyncMock()
    mock_db.get_user_workspace_ids = AsyncMock(return_value=ws_ids)
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
    does not own."""
    key = APIKeyInfo(
        key_id="key-ws",
        user_id="user-1",
        workspace_id="ws-scoped",
        permissions=["read", "search"],
        rate_limit=100,
        expires_at=None,
        status="active",
    )
    # The user only owns ws-scoped; a header asking for ws-other must 403.
    with _patch_user_workspaces(["ws-scoped"]):
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
    # The owner owns BOTH ws-a and ws-b, but the key is scoped to ws-a.
    with _patch_user_workspaces(["ws-a", "ws-b"]):
        with pytest.raises(HTTPException) as exc_info:
            await _resolve_workspace(key, "ws-b", required=False)
    assert exc_info.value.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.asyncio
async def test_workspace_scoped_key_with_matching_header_resolves() -> None:
    """A scoped key with a header naming its own workspace resolves fine."""
    key = APIKeyInfo(
        key_id="key-ws",
        user_id="user-1",
        workspace_id="ws-a",
        permissions=["read", "search"],
        rate_limit=100,
        expires_at=None,
        status="active",
    )
    with _patch_user_workspaces(["ws-a", "ws-b"]):
        resolved = await _resolve_workspace(key, "ws-a", required=False)
    assert resolved.workspace_id == "ws-a"


@pytest.mark.asyncio
async def test_workspace_scoped_key_ignores_absent_header() -> None:
    """A scoped key with no header resolves to its bound workspace."""
    key = APIKeyInfo(
        key_id="key-ws",
        user_id="user-1",
        workspace_id="ws-a",
        permissions=["read", "search"],
        rate_limit=100,
        expires_at=None,
        status="active",
    )
    with _patch_user_workspaces(["ws-a", "ws-b"]):
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
    the key's binding and the requested id for the same diagnostic reason."""
    key = APIKeyInfo(
        key_id="key-ws",
        user_id="user-1",
        workspace_id="ws-scoped",
        permissions=["read", "search"],
        rate_limit=100,
        expires_at=None,
        status="active",
    )
    with patch("src.services.auth.logger") as mock_logger:
        with pytest.raises(HTTPException):
            await _resolve_workspace(key, "ws-other", required=False)
    mock_logger.warning.assert_called_once()
    _, kwargs = mock_logger.warning.call_args
    assert kwargs["requested_workspace_id"] == "ws-other"
    assert kwargs["key_workspace_id"] == "ws-scoped"
