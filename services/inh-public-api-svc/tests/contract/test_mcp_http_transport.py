"""Streamable HTTP MCP transport contract tests (#220).

Covers the acceptance criteria on the HTTP surface mounted at ``POST /mcp``
inside this service's existing FastAPI app (``src/mcp_server/http_transport.py``):

- **Schema**: exactly the documented tools are advertised; the 3 the issue
  excludes (``verify_claim`` / ``search_memory`` / ``get_citations``) --
  plus ``report_feedback``, excluded by the same "10, not 13" intent -- are
  absent from HTTP but UNCHANGED on stdio. No HTTP schema mentions
  ``api_key`` anywhere.
- **Auth**: missing / invalid / expired key rejected at the HTTP layer before
  the MCP session manager ever sees the request (#180); a key lacking a
  tool's permission is rejected by ``call_tool`` before the handler runs
  (#14); a workspace-scoped key cannot reach another workspace (#138).
- **Errors**: ``isError=True`` with a branchable ``error_class`` in
  ``structuredContent`` (#216) -- the defect stdio still has by design
  (unchanged there, see ``server.py``'s module docstring).
- **Transport**: rate limiting applies to ``/mcp`` (#213); a full
  initialize -> tools/list -> tools/call round trip works over the real
  mounted ASGI app.

Two layers of test double as in ``tests/contract/test_mcp_contract.py`` /
``test_rest_contract.py``:

- Tool-registry-level tests call ``create_http_mcp_server()``'s real
  ``list_tools`` / ``call_tool`` handlers directly (no HTTP), patching
  ``get_database`` / ``get_search_service`` at the ``mcp_server.server``
  boundary and setting the ``_current_key_info`` contextvar directly (the
  ASGI auth gate's job, exercised separately below).
- Full-stack tests build the real ``create_app()`` and drive it through
  ``fastapi.testclient.TestClient`` (which runs ASGI lifespan events, unlike
  plain ``httpx.AsyncClient(transport=ASGITransport(...))`` -- required here
  because ``StreamableHTTPSessionManager.handle_request`` raises until its
  ``.run()`` context, entered in ``src/main.py``'s lifespan, has started).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import mcp.types as mcp_types
import pytest
from fastapi.testclient import TestClient
from mcp.types import CallToolResult

import src.services.auth as auth_mod
from src.main import create_app
from src.mcp_server import http_transport
from src.mcp_server import server as mcp_server
from src.models.api_key import APIKeyInfo

pytestmark = [pytest.mark.contract]

# The original issue list plus the later whoami tool (#278).
HTTP_EXPOSED_TOOLS = {
    "whoami",
    "search_documents",
    "list_documents",
    "get_document",
    "list_chunks",
    "get_document_context",
    "explain_lineage",
    "upload_document",
    "delete_document",
    "refresh_stale_source",
    "get_retrieval_health",
}

# Excluded from HTTP: the issue's explicit 3, plus report_feedback (see the
# comment on that ToolDef entry in server.py for why it is grouped here too).
HTTP_EXCLUDED_TOOLS = {"verify_claim", "search_memory", "get_citations", "report_feedback"}


def _key(
    permissions: list[str],
    *,
    workspace_id: str | None = None,
    expires_at=None,
) -> APIKeyInfo:
    return APIKeyInfo(
        key_id="key-http",
        user_id="user-http",
        workspace_id=workspace_id,
        permissions=permissions,  # type: ignore[arg-type]
        rate_limit=100,
        expires_at=expires_at,
        status="active",
    )


# --------------------------------------------------------------------------- #
# Helpers: drive the real HTTP server's list_tools / call_tool handlers
# directly, without an HTTP layer (mirrors test_mcp_contract.py's _list_tools
# / _call_tool for stdio).
# --------------------------------------------------------------------------- #
async def _list_http_tools() -> dict[str, mcp_types.Tool]:
    server = http_transport.create_http_mcp_server()
    handler = server.request_handlers[mcp_types.ListToolsRequest]
    result = await handler(mcp_types.ListToolsRequest(method="tools/list"))
    return {tool.name: tool for tool in result.root.tools}


async def _call_http_tool(
    name: str, arguments: dict, key_info: APIKeyInfo | None
) -> CallToolResult:
    """Invoke the real HTTP call_tool handler with ``key_info`` already
    "authenticated" (i.e. with ``_current_key_info`` pre-set, as the ASGI
    auth gate would do) -- so these tests exercise PERMISSION/dispatch
    behavior without needing a live HTTP request for every case."""
    server = http_transport.create_http_mcp_server()
    handler = server.request_handlers[mcp_types.CallToolRequest]
    token = http_transport._current_key_info.set(key_info)
    try:
        req = mcp_types.CallToolRequest(
            method="tools/call",
            params=mcp_types.CallToolRequestParams(name=name, arguments=arguments),
        )
        result = await handler(req)
    finally:
        http_transport._current_key_info.reset(token)
    return result.root


# =========================================================================== #
# Schema: exactly 10 tools, api_key stripped, excluded 3(+1) absent
# =========================================================================== #
class TestHttpToolSurface:
    async def test_exactly_the_documented_ten_tools_are_advertised(self):
        tools = await _list_http_tools()
        assert set(tools) == HTTP_EXPOSED_TOOLS

    @pytest.mark.parametrize("name", sorted(HTTP_EXCLUDED_TOOLS))
    async def test_excluded_tool_absent_from_http_list_tools(self, name):
        tools = await _list_http_tools()
        assert name not in tools

    @pytest.mark.parametrize("name", sorted(HTTP_EXCLUDED_TOOLS))
    async def test_excluded_tool_still_present_on_stdio(self, name):
        """The exact same tool that is invisible on HTTP is still fully
        advertised over stdio -- stdio is unaffected by #220."""
        server = mcp_server.create_mcp_server()
        handler = server.request_handlers[mcp_types.ListToolsRequest]
        result = await handler(mcp_types.ListToolsRequest(method="tools/list"))
        stdio_tools = {tool.name for tool in result.root.tools}
        assert name in stdio_tools

    @pytest.mark.parametrize("name", sorted(HTTP_EXPOSED_TOOLS))
    async def test_no_http_schema_contains_api_key(self, name):
        """No property, no required entry, and no substring 'api_key'
        anywhere in an HTTP tool's inputSchema."""
        import json

        tools = await _list_http_tools()
        schema = tools[name].inputSchema
        assert "api_key" not in schema.get("properties", {})
        assert "api_key" not in schema.get("required", [])
        assert "api_key" not in json.dumps(schema)

    @pytest.mark.parametrize("name", sorted(HTTP_EXPOSED_TOOLS))
    async def test_http_schema_is_stdio_schema_minus_api_key(self, name):
        """The HTTP schema is COMPUTED from the stdio schema, not a second,
        hand-maintained copy: stripping 'api_key' from stdio's schema and
        stripping it from HTTP's schema must land on the identical dict."""
        http_tools = await _list_http_tools()

        stdio_server = mcp_server.create_mcp_server()
        stdio_handler = stdio_server.request_handlers[mcp_types.ListToolsRequest]
        stdio_result = await stdio_handler(mcp_types.ListToolsRequest(method="tools/list"))
        stdio_tools = {tool.name: tool for tool in stdio_result.root.tools}

        expected = http_transport._strip_api_key(stdio_tools[name].inputSchema)
        assert http_tools[name].inputSchema == expected
        # And they really do differ only by api_key -- proves this isn't a
        # vacuous comparison against an already-api_key-free stdio schema.
        assert "api_key" in stdio_tools[name].inputSchema["properties"]

    @pytest.mark.parametrize("name", sorted(HTTP_EXPOSED_TOOLS))
    async def test_required_fields_other_than_api_key_are_preserved(self, name):
        """Stripping api_key must not touch any OTHER required field."""
        http_tools = await _list_http_tools()
        stdio_server = mcp_server.create_mcp_server()
        stdio_handler = stdio_server.request_handlers[mcp_types.ListToolsRequest]
        stdio_result = await stdio_handler(mcp_types.ListToolsRequest(method="tools/list"))
        stdio_required = {
            tool.name: [f for f in tool.inputSchema.get("required", []) if f != "api_key"]
            for tool in stdio_result.root.tools
        }
        assert http_tools[name].inputSchema.get("required", []) == stdio_required[name]


# =========================================================================== #
# call_tool: permission denial, unknown/excluded tools, error classification
# =========================================================================== #
class TestHttpCallToolDispatch:
    async def test_permission_denied_rejected_before_handler_runs(self):
        """A key lacking 'write' calling delete_document (requires 'write')
        never reaches the database, and gets isError=True + a branchable
        authorization_failed class (#14/#216)."""
        key = _key(["read", "search"])
        db = AsyncMock()
        db.get_document_by_id = AsyncMock()  # spy: must not be called

        with patch.object(mcp_server, "get_database", AsyncMock(return_value=db)):
            result = await _call_http_tool("delete_document", {"document_id": "doc-1"}, key)

        assert result.isError is True
        assert result.structuredContent == {"error_class": "authorization_failed"}
        assert result.content[0].text == "Error: API key does not have 'write' permission"
        db.get_document_by_id.assert_not_called()

    async def test_unknown_tool_returns_unknown_tool_class(self):
        result = await _call_http_tool("not_a_real_tool", {}, _key(["read", "search", "write"]))
        assert result.isError is True
        assert result.structuredContent == {"error_class": "unknown_tool"}

    @pytest.mark.parametrize("name", sorted(HTTP_EXCLUDED_TOOLS))
    async def test_excluded_tool_is_unreachable_via_call_tool(self, name):
        """Excluded tools are not just hidden from tools/list -- calling them
        by name over HTTP is rejected the SAME way as a truly unknown tool
        name, so a caller cannot probe which excluded tools exist."""
        result = await _call_http_tool(name, {}, _key(["read", "search", "write"]))
        assert result.isError is True
        assert result.structuredContent == {"error_class": "unknown_tool"}
        assert result.content[0].text == f"Error: Unknown tool '{name}'"

    async def test_missing_required_field_is_validation_error_class(self):
        """get_document with an EMPTY (not omitted) document_id passes the
        SDK's own JSON-Schema validation (a "" is still a valid string, so
        the SDK's pre-dispatch check does not intercept it -- see
        mcp.server.lowlevel.server.Server.call_tool) and falls through to
        the handler's own "Document ID is required" text, which this
        dispatcher classifies as validation_error. (Omitting document_id
        entirely is instead caught by the SDK's schema validation BEFORE our
        call_tool ever runs, with its own generic error and no
        structuredContent -- a different, earlier gate this test is not
        about.)"""
        key = _key(["read"])
        with patch.object(mcp_server, "get_database", AsyncMock()):
            result = await _call_http_tool("get_document", {"document_id": ""}, key)
        assert result.isError is True
        assert result.structuredContent == {"error_class": "validation_error"}

    async def test_not_found_is_not_found_class(self):
        key = _key(["read"])
        db = AsyncMock()
        db.get_document_by_id = AsyncMock(return_value=None)
        with patch.object(mcp_server, "get_database", AsyncMock(return_value=db)):
            result = await _call_http_tool("get_document", {"document_id": "doc-x"}, key)
        assert result.isError is True
        assert result.structuredContent == {"error_class": "not_found"}
        assert "not found" in result.content[0].text.lower()

    async def test_successful_call_has_iserror_false(self, sample_document):
        key = _key(["read"])
        db = AsyncMock()
        db.get_document_by_id = AsyncMock(return_value=sample_document)
        db.get_user_workspace_ids = AsyncMock(return_value=["ws-1"])
        with patch.object(mcp_server, "get_database", AsyncMock(return_value=db)):
            result = await _call_http_tool("get_document", {"document_id": "doc-1"}, key)
        assert result.isError is False

    async def test_handler_exception_is_internal_error_class(self):
        """An unexpected exception from the handler body must not crash the
        transport -- it becomes isError=True with an internal_error class,
        same as stdio's outer try/except but with a branchable class."""
        key = _key(["read"])
        with patch.object(
            mcp_server, "get_database", AsyncMock(side_effect=RuntimeError("db down"))
        ):
            result = await _call_http_tool("get_document", {"document_id": "doc-1"}, key)
        assert result.isError is True
        assert result.structuredContent == {"error_class": "internal_error"}

    async def test_missing_key_info_context_is_authentication_failed(self):
        """Defensive branch: call_tool invoked with no key in context at all
        (should never happen given the ASGI gate, but must fail safe)."""
        result = await _call_http_tool("get_document", {"document_id": "doc-1"}, None)
        assert result.isError is True
        assert result.structuredContent == {"error_class": "authentication_failed"}


# =========================================================================== #
# Workspace scoping (#138): a scoped key cannot reach another workspace
# =========================================================================== #
class TestHttpWorkspaceScoping:
    async def test_scoped_key_cannot_list_documents_in_another_workspace(self):
        """A key bound to ws-a asking for ws-b is rejected with
        authorization_failed, never reaching get_documents for ws-b."""
        key = _key(["read"], workspace_id="ws-a")
        db = AsyncMock()
        db.user_owns_workspace_in_mongo = AsyncMock(return_value=True)
        db.get_documents = AsyncMock()  # spy: must not be called for ws-b

        with patch.object(mcp_server, "get_database", AsyncMock(return_value=db)):
            result = await _call_http_tool("list_documents", {"workspace_id": "ws-b"}, key)

        assert result.isError is True
        assert result.structuredContent == {"error_class": "authorization_failed"}
        assert "ws-a" in result.content[0].text  # actionable: names the key's own binding
        db.get_documents.assert_not_called()

    async def test_scoped_key_can_list_its_own_workspace(self, sample_document):
        key = _key(["read"], workspace_id="ws-a")
        db = AsyncMock()
        db.user_owns_workspace_in_mongo = AsyncMock(return_value=True)
        db.get_documents = AsyncMock(return_value=([sample_document], 1))

        with patch.object(mcp_server, "get_database", AsyncMock(return_value=db)):
            result = await _call_http_tool("list_documents", {"workspace_id": "ws-a"}, key)

        assert result.isError is False


# =========================================================================== #
# Full stack: the real ASGI app, real /mcp route, real auth gate.
# =========================================================================== #
_HTTP_MCP_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}


def _app_client():
    """TestClient for the real app with DB init stubbed (see
    tests/integration/test_api_path.py) -- runs ASGI lifespan events, which
    is required for the StreamableHTTPSessionManager mounted at /mcp."""
    app = create_app()
    with patch("src.main.get_database", new_callable=AsyncMock):
        with TestClient(app) as client:
            yield client


@pytest.fixture
def client():
    yield from _app_client()


class TestMcpEndpointAuthGate:
    """Connection-level auth on /mcp -- the SAME src.services.auth dependency
    REST uses, so missing/invalid/expired-key behavior can't drift from it
    (#138/#180 closed by construction)."""

    def test_missing_key_returns_401(self, client: TestClient):
        r = client.post(
            "/mcp",
            headers=_HTTP_MCP_HEADERS,
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )
        assert r.status_code == 401

    def test_invalid_key_returns_401(self):
        app = create_app()
        db = AsyncMock()
        db.validate_api_key = AsyncMock(return_value=None)
        auth_mod._auth_service = None
        with (
            patch("src.main.get_database", new_callable=AsyncMock),
            patch("src.services.auth.get_database", new=AsyncMock(return_value=db)),
        ):
            with TestClient(app) as client:
                r = client.post(
                    "/mcp",
                    headers={**_HTTP_MCP_HEADERS, "X-API-Key": "bad"},
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                )
        auth_mod._auth_service = None
        assert r.status_code == 401

    def test_expired_key_returns_401(self):
        from datetime import datetime, timedelta, timezone

        app = create_app()
        expired = _key(
            ["read", "search", "write"], expires_at=datetime.now(timezone.utc) - timedelta(days=1)
        )
        db = AsyncMock()
        db.validate_api_key = AsyncMock(return_value=expired)
        auth_mod._auth_service = None
        with (
            patch("src.main.get_database", new_callable=AsyncMock),
            patch("src.services.auth.get_database", new=AsyncMock(return_value=db)),
        ):
            with TestClient(app) as client:
                r = client.post(
                    "/mcp",
                    headers={**_HTTP_MCP_HEADERS, "X-API-Key": "ink_expired"},
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                )
        auth_mod._auth_service = None
        assert r.status_code == 401

    def test_no_trailing_slash_redirect(self, client: TestClient):
        """POST /mcp (the issue's exact install path) must not 307-redirect
        to /mcp/ -- proven by disabling redirect-following and asserting the
        response is NOT a redirect (no key is sent, so the real, correctly
        -routed outcome is the 401 auth gate, not a 3xx to a different path;
        a regression back to `app.mount()` would show up here as a 307 with
        a `location` header instead)."""
        r = client.post(
            "/mcp",
            headers=_HTTP_MCP_HEADERS,
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            follow_redirects=False,
        )
        assert r.status_code not in (307, 308)
        assert "location" not in r.headers


class TestMcpEndpointRateLimiting:
    """/mcp is not in any middleware's exempt-paths set, so it is rate
    limited by the SAME RateLimitingMiddleware REST routes share (#213) --
    pinned deterministically (no timing-dependent burst loop) by forcing the
    shared rate limiter to deny."""

    def test_call_tool_is_rate_limited(self, client: TestClient):
        from src.core.rate_limiter import RateLimitInfo, RateLimitResult

        denied = RateLimitResult(
            allowed=False,
            info=RateLimitInfo(limit=1, remaining=0, reset_at=0.0, window_seconds=60),
        )
        with patch("src.middleware.rate_limiting.get_rate_limiter") as mock_get_limiter:
            mock_limiter = AsyncMock()
            mock_limiter.check_rate_limit = AsyncMock(return_value=denied)
            mock_get_limiter.return_value = mock_limiter

            r = client.post(
                "/mcp",
                headers=_HTTP_MCP_HEADERS,
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            )
        assert r.status_code == 429


class TestMcpFullProtocolRoundTrip:
    """initialize -> tools/list -> tools/call over the real mounted app."""

    def test_round_trip(self, sample_document):
        key = _key(["read", "search", "write"])
        db = AsyncMock()
        db.validate_api_key = AsyncMock(return_value=key)
        db.get_user_workspace_ids = AsyncMock(return_value=["ws-1"])
        db.get_documents_multi_workspace = AsyncMock(return_value=([sample_document], 1))

        auth_mod._auth_service = None
        app = create_app()
        with (
            patch("src.main.get_database", new_callable=AsyncMock),
            patch("src.services.auth.get_database", new=AsyncMock(return_value=db)),
            patch.object(mcp_server, "get_database", AsyncMock(return_value=db)),
        ):
            with TestClient(app) as client:
                headers = {**_HTTP_MCP_HEADERS, "X-API-Key": "ink_test"}

                init = client.post(
                    "/mcp",
                    headers=headers,
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2025-06-18",
                            "capabilities": {},
                            "clientInfo": {"name": "test-client", "version": "0"},
                        },
                    },
                )
                assert init.status_code == 200
                assert init.json()["result"]["serverInfo"]["name"] == "inherent-knowledge-base"

                listed = client.post(
                    "/mcp",
                    headers=headers,
                    json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                )
                assert listed.status_code == 200
                names = {t["name"] for t in listed.json()["result"]["tools"]}
                assert names == HTTP_EXPOSED_TOOLS

                called = client.post(
                    "/mcp",
                    headers=headers,
                    json={
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "tools/call",
                        "params": {"name": "list_documents", "arguments": {}},
                    },
                )
                assert called.status_code == 200
                result = called.json()["result"]
                assert result["isError"] is False
                assert "report.pdf" in result["content"][0]["text"]
        auth_mod._auth_service = None

    def test_excluded_tool_call_over_real_http(self):
        """verify_claim, listed nowhere in tools/list, is still rejected the
        same way through a REAL tools/call POST (not just at the handler
        level tested above)."""
        key = _key(["read", "search", "write"])
        db = AsyncMock()
        db.validate_api_key = AsyncMock(return_value=key)

        auth_mod._auth_service = None
        app = create_app()
        with (
            patch("src.main.get_database", new_callable=AsyncMock),
            patch("src.services.auth.get_database", new=AsyncMock(return_value=db)),
            patch.object(mcp_server, "get_database", AsyncMock(return_value=db)),
        ):
            with TestClient(app) as client:
                headers = {**_HTTP_MCP_HEADERS, "X-API-Key": "ink_test"}
                r = client.post(
                    "/mcp",
                    headers=headers,
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {"name": "verify_claim", "arguments": {"claim": "x"}},
                    },
                )
        auth_mod._auth_service = None
        assert r.status_code == 200
        result = r.json()["result"]
        assert result["isError"] is True
        assert result["structuredContent"] == {"error_class": "unknown_tool"}
