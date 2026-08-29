"""Unit tests for the list_workspaces MCP tool (#297).

Tests verify authorization boundaries: a workspace-scoped key sees only its
bound workspace, while a user-scoped key sees every authorized workspace.
Tests also verify the response shape, single-query efficiency, and empty case.
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
    row.workspace_id = workspace_id
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

    # Mock session with a single query result (no N+1 loop)
    mock_session = AsyncMock()
    mock_db.session = MagicMock(return_value=mock_session.__aenter__.return_value)
    mock_db.session.return_value.__aenter__ = AsyncMock(return_value=mock_session)
    mock_db.session.return_value.__aexit__ = AsyncMock(return_value=None)

    # Single query returns all workspaces at once
    async def mock_execute(query):
        mock_result = AsyncMock()
        mock_result.fetchall = AsyncMock(
            return_value=[
                _mock_workspace_row("ws-a", 10, "Workspace A"),
                _mock_workspace_row("ws-b", 20, "Workspace B"),
                _mock_workspace_row("ws-c", 0, None),
            ]
        )
        return mock_result

    mock_session.execute = mock_execute

    with patch.object(mcp_server, "get_database", AsyncMock(return_value=mock_db)):
        result = await mcp_server._handle_list_workspaces(_key(), {})

    payload = _structured_payload(result)
    assert len(payload["workspaces"]) == 3
    assert payload["is_scoped_binding"] is False
    # Verify order is preserved from authorized list
    assert [ws["workspace_id"] for ws in payload["workspaces"]] == authorized_ws


@pytest.mark.asyncio
async def test_list_workspaces_scoped_key_sees_only_bound_workspace() -> None:
    """Workspace-scoped key returns exactly its bound workspace, not the owner's full set.

    The authorization rule (#138): a workspace-scoped key is validated against
    user_owns_workspace_in_mongo for its one workspace only.
    """
    workspace_id = "ws-bound"
    scoped_key = _scoped_key(workspace_id)

    mock_db = AsyncMock()
    mock_db.user_owns_workspace_in_mongo = AsyncMock(return_value=True)

    # Mock session
    mock_session = AsyncMock()
    mock_db.session = MagicMock(return_value=mock_session.__aenter__.return_value)
    mock_db.session.return_value.__aenter__ = AsyncMock(return_value=mock_session)
    mock_db.session.return_value.__aexit__ = AsyncMock(return_value=None)

    async def mock_execute(query):
        mock_result = AsyncMock()
        mock_result.fetchall = AsyncMock(
            return_value=[_mock_workspace_row(workspace_id, 15, "Bound Workspace")]
        )
        return mock_result

    mock_session.execute = mock_execute

    with patch.object(mcp_server, "get_database", AsyncMock(return_value=mock_db)):
        result = await mcp_server._handle_list_workspaces(scoped_key, {})

    payload = _structured_payload(result)
    # Scoped key must see exactly one workspace
    assert len(payload["workspaces"]) == 1
    assert payload["workspaces"][0]["workspace_id"] == workspace_id
    # is_scoped_binding should be True (top-level, not per-workspace)
    assert payload["is_scoped_binding"] is True
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
    assert payload["is_scoped_binding"] is False
    # Should not be an error, just an empty list
    text = result[0].text
    assert "No workspaces found" in text


@pytest.mark.asyncio
async def test_list_workspaces_response_shape() -> None:
    """Response shape matches the tool contract: workspace_id, name, document_count, is_scoped_binding."""
    mock_db = AsyncMock()
    authorized_ws = ["ws-test"]
    mock_db.get_user_workspace_ids = AsyncMock(return_value=authorized_ws)

    # Mock session
    mock_session = AsyncMock()
    mock_db.session = MagicMock(return_value=mock_session.__aenter__.return_value)
    mock_db.session.return_value.__aenter__ = AsyncMock(return_value=mock_session)
    mock_db.session.return_value.__aexit__ = AsyncMock(return_value=None)

    async def mock_execute(query):
        mock_result = AsyncMock()
        mock_result.fetchall = AsyncMock(
            return_value=[_mock_workspace_row("ws-test", 42, "Test Workspace")]
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

    # is_scoped_binding is top-level
    assert "is_scoped_binding" in payload
    assert "is_scoped_binding" not in ws

    # Verify values
    assert ws["workspace_id"] == "ws-test"
    assert ws["name"] == "Test Workspace"
    assert ws["document_count"] == 42
    assert payload["is_scoped_binding"] is False


@pytest.mark.asyncio
async def test_list_workspaces_single_query_regardless_of_workspace_count() -> None:
    """Verify the tool makes a single query regardless of workspace count.

    This test ensures the N+1 query problem (#297) does not regress.
    With 3+ workspaces, the handler must issue exactly 1 execute() call,
    not one per workspace.
    """
    mock_db = AsyncMock()
    # Simulate user with 5 workspaces
    authorized_ws = ["ws-1", "ws-2", "ws-3", "ws-4", "ws-5"]
    mock_db.get_user_workspace_ids = AsyncMock(return_value=authorized_ws)

    # Mock session with execute call counter
    mock_session = AsyncMock()
    mock_db.session = MagicMock(return_value=mock_session.__aenter__.return_value)
    mock_db.session.return_value.__aenter__ = AsyncMock(return_value=mock_session)
    mock_db.session.return_value.__aexit__ = AsyncMock(return_value=None)

    # Track execute calls
    execute_call_count = 0

    async def mock_execute(query):
        nonlocal execute_call_count
        execute_call_count += 1
        mock_result = AsyncMock()
        # Return all workspaces from single query
        mock_result.fetchall = AsyncMock(
            return_value=[
                _mock_workspace_row(ws_id, i * 10, f"Workspace {i}")
                for i, ws_id in enumerate(authorized_ws)
            ]
        )
        return mock_result

    mock_session.execute = mock_execute

    with patch.object(mcp_server, "get_database", AsyncMock(return_value=mock_db)):
        result = await mcp_server._handle_list_workspaces(_key(), {})

    # Must make exactly 1 query, not 5
    assert execute_call_count == 1, (
        f"Expected 1 query for 5 workspaces, got {execute_call_count}. "
        "N+1 query regression detected."
    )

    payload = _structured_payload(result)
    assert len(payload["workspaces"]) == 5


@pytest.mark.asyncio
async def test_list_workspaces_missing_metadata_row() -> None:
    """Workspace in authorized set but missing workspace_metadata row appears with count=0, name=null."""
    mock_db = AsyncMock()
    authorized_ws = ["ws-existing", "ws-new"]
    mock_db.get_user_workspace_ids = AsyncMock(return_value=authorized_ws)

    # Mock session
    mock_session = AsyncMock()
    mock_db.session = MagicMock(return_value=mock_session.__aenter__.return_value)
    mock_db.session.return_value.__aenter__ = AsyncMock(return_value=mock_session)
    mock_db.session.return_value.__aexit__ = AsyncMock(return_value=None)

    async def mock_execute(query):
        mock_result = AsyncMock()
        # Only ws-existing has a metadata row; ws-new is missing
        mock_result.fetchall = AsyncMock(
            return_value=[_mock_workspace_row("ws-existing", 10, "Existing")]
        )
        return mock_result

    mock_session.execute = mock_execute

    with patch.object(mcp_server, "get_database", AsyncMock(return_value=mock_db)):
        result = await mcp_server._handle_list_workspaces(_key(), {})

    payload = _structured_payload(result)
    assert len(payload["workspaces"]) == 2

    # Find each workspace in result
    ws_by_id = {ws["workspace_id"]: ws for ws in payload["workspaces"]}

    # Existing workspace has real data
    assert ws_by_id["ws-existing"]["document_count"] == 10
    assert ws_by_id["ws-existing"]["name"] == "Existing"

    # Missing workspace appears with defaults
    assert ws_by_id["ws-new"]["document_count"] == 0
    assert ws_by_id["ws-new"]["name"] is None

    # Order preserved from authorized list
    assert [ws["workspace_id"] for ws in payload["workspaces"]] == authorized_ws


@pytest.mark.asyncio
async def test_list_workspaces_tool_registered() -> None:
    """list_workspaces is registered in _TOOLS with correct config."""
    assert "list_workspaces" in mcp_server._TOOLS
    tool = mcp_server._TOOLS["list_workspaces"]
    assert tool.permission == "read"
    assert tool.handler == mcp_server._handle_list_workspaces
    assert "api_key" in tool.input_schema["properties"]
    assert "api_key" in tool.input_schema["required"]
