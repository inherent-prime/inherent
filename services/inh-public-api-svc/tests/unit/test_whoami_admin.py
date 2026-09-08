"""Small unit checks for identity/admin defaults and MCP parity (#278, #279)."""

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.config.settings import Settings
from src.mcp_server import http_transport
from src.mcp_server import server as mcp_server
from src.models.api_key import APIKeyInfo
from src.services.database import DatabaseService


def test_admin_api_defaults_off():
    assert Settings().admin_api_enabled is False


@pytest.mark.asyncio
async def test_whoami_mcp_uses_authoritative_workspace_scope():
    key_info = APIKeyInfo(
        key_id="key-a",
        name="A",
        user_id="user-a",
        workspace_id="ws-a",
        permissions=["read"],
    )
    database = AsyncMock()
    database.user_owns_workspace_in_mongo.return_value = False

    with patch.object(mcp_server, "get_database", AsyncMock(return_value=database)):
        content = await mcp_server._TOOLS["whoami"].handler(key_info, {})

    payload = json.loads(content[0].text.split("```json", 1)[1].rsplit("```", 1)[0])["structured"]
    assert payload["workspace_ids"] == []
    assert set(payload) == {
        "key_id",
        "key_name",
        "user_id",
        "workspace_id",
        "workspace_ids",
        "permissions",
        "engine_version",
        "endpoint",
    }
    database.get_user_workspace_ids.assert_not_awaited()


@pytest.mark.asyncio
async def test_rest_and_mcp_whoami_use_identical_fields():
    key_info = APIKeyInfo(
        key_id="key-a", name="A", user_id="user-a", workspace_id=None, permissions=["read"]
    )
    database = AsyncMock()
    database.get_user_workspace_ids.return_value = ["ws-a"]
    endpoint = "https://engine.example"

    from src.api.v1.whoami import build_whoami

    rest = await build_whoami(key_info, database, endpoint)
    # The HTTP transport publishes the caller's URL here; the tool takes no
    # endpoint argument, so this ContextVar is the only way in.
    token = mcp_server.current_mcp_endpoint.set(endpoint)
    try:
        with patch.object(mcp_server, "get_database", AsyncMock(return_value=database)):
            content = await mcp_server._TOOLS["whoami"].handler(key_info, {})
    finally:
        mcp_server.current_mcp_endpoint.reset(token)
    mcp = json.loads(content[0].text.split("```json", 1)[1].rsplit("```", 1)[0])["structured"]
    assert mcp == rest.model_dump(mode="json")


@pytest.mark.asyncio
async def test_whoami_is_advertised_on_http_without_api_key_argument():
    server = http_transport.create_http_mcp_server()
    import mcp.types as types

    result = await server.request_handlers[types.ListToolsRequest](
        types.ListToolsRequest(method="tools/list")
    )
    tool = next(tool for tool in result.root.tools if tool.name == "whoami")
    assert "api_key" not in tool.inputSchema.get("properties", {})


@pytest.mark.asyncio
async def test_admin_workspace_query_combines_mongo_names_and_postgres_counts():
    database = DatabaseService.__new__(DatabaseService)
    result = MagicMock()
    result.fetchall.return_value = [SimpleNamespace(workspace_id="ws-a", document_count=3)]
    session = AsyncMock()
    session.execute.return_value = result

    @asynccontextmanager
    async def fake_session():
        yield session

    database.session = fake_session
    cursor = MagicMock()
    cursor.sort.return_value = cursor
    cursor.skip.return_value = cursor
    cursor.to_list = AsyncMock(return_value=[{"_id": "ws-a", "name": "A", "user_id": "user-a"}])
    collection = MagicMock()
    collection.find.return_value = cursor
    mongo = {"main": {"workspaces": collection}}

    with patch("src.services.mongo_client.get_mongo_client", return_value=mongo):
        rows = await database.list_admin_workspaces(offset=0, limit=20)

    assert rows == [{"workspace_id": "ws-a", "name": "A", "user_id": "user-a", "document_count": 3}]
    sql = str(session.execute.await_args.args[0])
    assert "processed_documents" in sql
    assert "ANY(CAST(:workspace_ids AS text[]))" in sql


@pytest.mark.asyncio
async def test_admin_key_query_projects_no_secret_columns():
    database = DatabaseService.__new__(DatabaseService)
    safe = {
        "key_id": "key-a",
        "key_name": "A",
        "key_prefix": "ink_aaaaaaaa",
        "workspace_id": "ws-a",
        "user_id": "user-a",
        "permissions": ["read"],
        "status": "active",
        "created_at": None,
        "last_used_at": None,
        "expires_at": None,
    }
    result = MagicMock()
    result.fetchall.return_value = [SimpleNamespace(_mapping=safe)]
    session = AsyncMock()
    session.execute.return_value = result

    @asynccontextmanager
    async def fake_session():
        yield session

    database.session = fake_session
    rows = await database.list_admin_keys(offset=0, limit=20)
    sql = str(session.execute.await_args.args[0])

    assert rows == [safe]
    assert "key_hash" not in sql
    assert "OFFSET :offset LIMIT :limit" in sql


def test_engine_version_tracks_the_installed_package_not_a_literal():
    """`whoami` publishes this value, so a hardcoded literal silently drifts.

    The previous default was pinned at "0.2.0" while pyproject.toml said
    "0.3.0", and every whoami response reported the stale number.
    """
    from importlib.metadata import version as package_version

    assert Settings().version == package_version("inh-public-api-svc")


@pytest.mark.parametrize("environment", ["production", "staging"])
def test_openapi_schema_is_not_served_outside_development(environment):
    """An unauthenticated schema fetch must not list the gated admin routes."""
    from fastapi.testclient import TestClient

    from src.main import create_app

    with patch("src.main.settings", Settings(environment=environment)):
        app = create_app()

    assert app.openapi_url is None
    # No lifespan: this asserts on routing, not on a reachable backend.
    assert TestClient(app).get("/openapi.json").status_code == 404


def test_openapi_schema_is_still_served_in_development():
    from src.main import create_app

    with patch("src.main.settings", Settings(environment="development")):
        app = create_app()

    assert app.openapi_url == "/openapi.json"
