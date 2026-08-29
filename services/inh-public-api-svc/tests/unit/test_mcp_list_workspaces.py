"""Unit tests for the list_workspaces MCP tool (#297).

Tests verify authorization boundaries: a workspace-scoped key sees only its
bound workspace, while a user-scoped key sees every authorized workspace.
Tests also verify the response shape and empty-workspace case.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.mcp_server import server as mcp_server
from src.models.api_key import APIKeyInfo

pytestmark = pytest.mark.asyncio


def _key(user_id: str = "user-1") -> APIKeyInfo:
    """User-scoped key: not bound to any workspace."""
    return APIKeyInfo(
        key_id="key-1",
        user_id=user_id,
        workspace_id=None,
        permissions=["read", "search"],
        rate_limit=100,
        expires_at=None,
        status="active",
    )


def _scoped_key(workspace_id: str, user_id: str = "user-1") -> APIKeyInfo:
    """Workspace-scoped key: bound to exactly one workspace (#138)."""
    return APIKeyInfo(
        key_id="key-scoped",
        user_id=user_id,
        workspace_id=workspace_id,
        permissions=["read", "search", "write"],
        rate_limit=100,
        expires_at=None,
        status="active",
    )


def _structured_payload(result) -> dict:
    """Extract the JSON ``structured`` block embedded in a TextContent reply."""
    text = result[0].text
    block = text.split("```json", 1)[1].rsplit("```", 1)[0].strip()
    return json.loads(block)["structured"]


def _mock_workspace_row(workspace_id: str, document_count: int = 5, name: str | None = None):
    """Create a mock database row for workspace_metadata."""
    row = MagicMock()
    row.document_count = document_count
    metadata = {"name": name} if name else {}
    row.metadata = metadata
    return row


@pytest.mark.asyncio
async def test_list_workspaces_user_scoped_key_sees_all_authorized() -> None:
    """User-scoped key returns every workspace the user owns.

    The authorization rule (#138): a user-scoped key (workspace_id=None)
    may act on every workspace its user owns, via get_user_workspace_ids.
    """
    mock_db = AsyncMock()
    authorized_ws = ["ws-a", "ws-b", "ws-c"]
    mock_db.get_user_workspace_ids = AsyncMock(return_value=authorized_ws)

    # Mock session and query results
    mock_session = AsyncMock()
    mock_db.session = MagicMock(return_value=mock_session.__aenter__.return_value)
    mock_db.session.return_value.__aenter__ = AsyncMock(return_value=mock_session)
    mock_db.session.return_value.__aexit__ = AsyncMock(return_value=None)

    # Mock execute for each workspace query
    async def mock_execute(query):
        # Return mock rows for workspace_metadata queries
        mock_result = AsyncMock()
        # Simple approach: return document counts based on workspace id
        if "workspace_id" in str(query):
            mock_result.fetchone = MagicMock(
                return_value=(
                    _mock_workspace_row("ws-a", 10, "Workspace A")
                    if "ws-a" in str(query)
                    else (
                        _mock_workspace_row("ws-b", 20, "Workspace B")
                        if "ws-b" in str(query)
                        else _mock_workspace_row("ws-c", 0, None)
                    )
                )
            )
        return mock_result

    mock_session.execute = mock_execute

    with patch.object(mcp_server, "get_database", AsyncMock(return_value=mock_db)):
        result = await mcp_server._handle_list_workspaces(_key(), {})

    payload = _structured_payload(result)
    assert len(payload["workspaces"]) == 3
    assert payload["workspaces"][0]["workspace_id"] in authorized_ws
    # All results should have is_scoped_binding=False for user-scoped key
    for ws in payload["workspaces"]:
        assert ws["is_scoped_binding"] is False
        assert "workspace_id" in ws
        assert "document_count" in ws
        assert "name" in ws


@pytest.mark.asyncio
async def test_list_workspaces_scoped_key_sees_only_bound_workspace() -> None:
    """Workspace-scoped key returns exactly its bound workspace, not the owner's full set.

    The authorization rule (#138): a workspace-scoped key is validated against
    user_owns_workspace_in_mongo for its one workspace only, never the user's
    full owned set.
    """
    workspace_id = "ws-bound"
    scoped_key = _scoped_key(workspace_id)

    mock_db = AsyncMock()
    # For a scoped key, get_authorized_workspace_ids should return exactly one workspace
    mock_db.user_owns_workspace_in_mongo = AsyncMock(return_value=True)

    # Mock session and query results
    mock_session = AsyncMock()
    mock_db.session = MagicMock(return_value=mock_session.__aenter__.return_value)
    mock_db.session.return_value.__aenter__ = AsyncMock(return_value=mock_session)
    mock_db.session.return_value.__aexit__ = AsyncMock(return_value=None)

    async def mock_execute(query):
        mock_result = AsyncMock()
        mock_result.fetchone = AsyncMock(
            return_value=_mock_workspace_row(workspace_id, 15, "Bound Workspace")
        )
        return mock_result

    mock_session.execute = mock_execute

    with patch.object(mcp_server, "get_database", AsyncMock(return_value=mock_db)):
        result = await mcp_server._handle_list_workspaces(scoped_key, {})

    payload = _structured_payload(result)
    # Scoped key must see exactly one workspace
    assert len(payload["workspaces"]) == 1
    assert payload["workspaces"][0]["workspace_id"] == workspace_id
    # is_scoped_binding should be True for a workspace-scoped key
    assert payload["workspaces"][0]["is_scoped_binding"] is True
    assert payload["workspaces"][0]["document_count"] == 15


@pytest.mark.asyncio
async def test_list_workspaces_empty_case_returns_empty_list() -> None:
    """A principal authorized for zero workspaces gets an empty list, not an error.

    This can happen if a workspace-scoped key's workspace was deleted or access
    was revoked. The tool must return empty, not error (#297).
    """
    mock_db = AsyncMock()
    # No authorized workspaces
    mock_db.get_user_workspace_ids = AsyncMock(return_value=[])

    with patch.object(mcp_server, "get_database", AsyncMock(return_value=mock_db)):
        result = await mcp_server._handle_list_workspaces(_key(), {})

    payload = _structured_payload(result)
    assert payload["workspaces"] == []
    # Should not be an error, just an empty list
    text = result[0].text
    assert "No workspaces found" in text


@pytest.mark.asyncio
async def test_list_workspaces_response_shape() -> None:
    """Response shape matches the tool contract: workspace_id, name, document_count, is_scoped_binding."""
    mock_db = AsyncMock()
    authorized_ws = ["ws-test"]
    mock_db.get_user_workspace_ids = AsyncMock(return_value=authorized_ws)

    # Mock session and query results
    mock_session = AsyncMock()
    mock_db.session = MagicMock(return_value=mock_session.__aenter__.return_value)
    mock_db.session.return_value.__aenter__ = AsyncMock(return_value=mock_session)
    mock_db.session.return_value.__aexit__ = AsyncMock(return_value=None)

    async def mock_execute(query):
        mock_result = AsyncMock()
        mock_result.fetchone = AsyncMock(
            return_value=_mock_workspace_row("ws-test", 42, "Test Workspace")
        )
        return mock_result

    mock_session.execute = mock_execute

    with patch.object(mcp_server, "get_database", AsyncMock(return_value=mock_db)):
        result = await mcp_server._handle_list_workspaces(_key(), {})

    payload = _structured_payload(result)
    ws = payload["workspaces"][0]

    # Verify all required fields are present
    assert "workspace_id" in ws
    assert "name" in ws
    assert "document_count" in ws
    assert "is_scoped_binding" in ws

    # Verify values
    assert ws["workspace_id"] == "ws-test"
    assert ws["name"] == "Test Workspace"
    assert ws["document_count"] == 42
    assert ws["is_scoped_binding"] is False


@pytest.mark.asyncio
async def test_list_workspaces_name_null_when_not_in_metadata() -> None:
    """Workspace with no name in metadata returns name=null."""
    mock_db = AsyncMock()
    authorized_ws = ["ws-unnamed"]
    mock_db.get_user_workspace_ids = AsyncMock(return_value=authorized_ws)

    # Mock session and query results
    mock_session = AsyncMock()
    mock_db.session = MagicMock(return_value=mock_session.__aenter__.return_value)
    mock_db.session.return_value.__aenter__ = AsyncMock(return_value=mock_session)
    mock_db.session.return_value.__aexit__ = AsyncMock(return_value=None)

    async def mock_execute(query):
        mock_result = AsyncMock()
        mock_result.fetchone = AsyncMock(return_value=_mock_workspace_row("ws-unnamed", 5, None))
        return mock_result

    mock_session.execute = mock_execute

    with patch.object(mcp_server, "get_database", AsyncMock(return_value=mock_db)):
        result = await mcp_server._handle_list_workspaces(_key(), {})

    payload = _structured_payload(result)
    assert payload["workspaces"][0]["name"] is None


@pytest.mark.asyncio
async def test_list_workspaces_tool_registered() -> None:
    """list_workspaces is registered in _TOOLS with correct config."""
    assert "list_workspaces" in mcp_server._TOOLS
    tool = mcp_server._TOOLS["list_workspaces"]
    assert tool.permission == "read"
    assert tool.handler == mcp_server._handle_list_workspaces
    assert "api_key" in tool.input_schema["properties"]
    assert "api_key" in tool.input_schema["required"]
