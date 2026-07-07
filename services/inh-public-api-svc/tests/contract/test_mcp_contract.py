"""MCP tool contract regression tests (M6 #30).

Locks down the MCP agent surface so agents do not silently break. For each tool
(search_documents, search_memory, get_citations, verify_claim, explain_lineage,
refresh_stale_source, get_document_context, list_documents) we assert:

- **inputSchema** advertises the documented required fields with the documented
  JSON types (and ``api_key`` is always required).
- **output** of a successful call is ``list[TextContent]`` (the MCP convention).
- **permission-denied** path returns an ``Error: ...`` ``TextContent`` and NEVER
  invokes the underlying service (search / db / verify) — mirroring the REST 403.

The tools are exercised through the real registered ``list_tools`` /
``call_tool`` handlers on the server, so the permission map and the schemas are
the actual ones agents see. ``get_database`` / ``get_search_service`` are patched
at the ``mcp_server.server`` boundary exactly like
tests/security/test_mcp_workspace_boundaries.py.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import mcp.types as mcp_types
import pytest
from mcp.types import TextContent

from src.mcp_server import server as mcp_server
from src.models.api_key import APIKeyInfo

pytestmark = [pytest.mark.contract]


# Documented per-tool contract: required input fields (besides api_key, which is
# always required). Kept here as the GOLDEN spec — if the server's advertised
# schema drifts from it, these tests fail. (Required permissions live in
# ``_PERMISSION`` below, mirroring the server's _TOOL_PERMISSIONS map.)
TOOL_SPEC: dict[str, dict] = {
    "search_documents": {"required": ["api_key", "query"]},
    "search_memory": {"required": ["api_key", "query"]},
    "get_citations": {"required": ["api_key", "query"]},
    "get_document_context": {"required": ["api_key", "document_id"]},
    "list_documents": {"required": ["api_key"]},
    "verify_claim": {"required": ["api_key", "claim"]},
    "explain_lineage": {"required": ["api_key", "document_id"]},
    "refresh_stale_source": {"required": ["api_key", "document_id"]},
    "delete_document": {"required": ["api_key", "document_id"]},
}

# Permission each tool requires (mirrors src/mcp_server/server._TOOL_PERMISSIONS).
_PERMISSION: dict[str, str] = {
    "search_documents": "search",
    "search_memory": "search",
    "get_citations": "search",
    "get_document_context": "read",
    "list_documents": "read",
    "verify_claim": "read",
    "explain_lineage": "read",
    "refresh_stale_source": "write",
    "delete_document": "write",
}

# A key that LACKS the tool's required permission (so the denied path triggers).
# Any permission set without the required one works; pick a single other perm.
_DENY_KEY_PERMS: dict[str, list[str]] = {
    "search": ["read"],  # has read but not search
    "read": ["search"],  # has search but not read
    "write": ["read", "search"],  # has read+search but not write
}

# Minimal arguments to actually drive each tool past schema/permission checks.
_TOOL_ARGS: dict[str, dict] = {
    "search_documents": {"query": "q"},
    "search_memory": {"query": "q"},
    "get_citations": {"query": "q"},
    "get_document_context": {"document_id": "doc-1"},
    "list_documents": {},
    "verify_claim": {"claim": "the sky is blue", "evidence": ["the sky is blue"]},
    "explain_lineage": {"document_id": "doc-1"},
    "refresh_stale_source": {"document_id": "doc-1"},
    "delete_document": {"document_id": "doc-1"},
}

ALL_TOOLS = list(_PERMISSION)


def _key(permissions: list[str]) -> APIKeyInfo:
    return APIKeyInfo(
        key_id="key-1",
        user_id="user-1",
        workspace_id=None,
        permissions=permissions,  # type: ignore[arg-type]
        rate_limit=100,
        expires_at=None,
        status="active",
    )


async def _list_tools() -> dict[str, mcp_types.Tool]:
    """Return the server's advertised tools keyed by name (real list_tools)."""
    server = mcp_server.create_mcp_server()
    handler = server.request_handlers[mcp_types.ListToolsRequest]
    result = await handler(mcp_types.ListToolsRequest(method="tools/list"))
    return {tool.name: tool for tool in result.root.tools}


async def _call_tool(name: str, arguments: dict) -> list[TextContent]:
    """Invoke a tool through the real call_tool handler; return its content."""
    server = mcp_server.create_mcp_server()
    handler = server.request_handlers[mcp_types.CallToolRequest]
    req = mcp_types.CallToolRequest(
        method="tools/call",
        params=mcp_types.CallToolRequestParams(name=name, arguments=arguments),
    )
    result = await handler(req)
    return result.root.content


# =========================================================================== #
# inputSchema contract
# =========================================================================== #
class TestToolSchemas:
    async def test_all_documented_tools_are_advertised(self):
        tools = await _list_tools()
        assert set(tools) == set(ALL_TOOLS)

    @pytest.mark.parametrize("name", ALL_TOOLS)
    async def test_input_schema_required_fields(self, name):
        """Each tool's inputSchema requires exactly the documented fields and
        always requires api_key (string)."""
        tools = await _list_tools()
        schema = tools[name].inputSchema
        assert schema["type"] == "object"
        props = schema["properties"]
        required = schema["required"]

        assert "api_key" in required
        assert props["api_key"]["type"] == "string"

        for field in TOOL_SPEC[name]["required"]:
            assert field in required, f"{name}: missing required '{field}'"
            assert field in props, f"{name}: '{field}' not declared in properties"

    async def test_search_tools_share_documented_param_types(self):
        """The search-shaped tools expose the documented knobs with the right
        JSON types (search_mode enum, limit int, min_score number, etc.)."""
        tools = await _list_tools()
        for name in ("search_documents", "search_memory", "get_citations"):
            props = tools[name].inputSchema["properties"]
            assert props["query"]["type"] == "string"
            assert props["limit"]["type"] == "integer"
            assert props["min_score"]["type"] == "number"
            assert props["search_mode"]["enum"] == ["semantic", "hybrid", "keyword"]
            assert props["document_ids"]["type"] == "array"

    async def test_verify_claim_schema_types(self):
        tools = await _list_tools()
        props = tools["verify_claim"].inputSchema["properties"]
        assert props["claim"]["type"] == "string"
        assert props["evidence"]["type"] == "array"

    async def test_explain_lineage_schema_types(self):
        tools = await _list_tools()
        props = tools["explain_lineage"].inputSchema["properties"]
        assert props["document_id"]["type"] == "string"
        assert props["chunk_id"]["type"] == "string"


# =========================================================================== #
# output is list[TextContent]
# =========================================================================== #
class TestToolOutputType:
    @pytest.mark.parametrize("name", ALL_TOOLS)
    async def test_successful_call_returns_list_of_textcontent(self, name, sample_document):
        """A happy-path call returns a non-empty list[TextContent]."""
        key = _key(["read", "search", "write"])

        db = AsyncMock()
        db.validate_api_key = AsyncMock(return_value=key)
        db.get_user_workspace_ids = AsyncMock(return_value=["ws-1"])
        db.get_document_by_id = AsyncMock(return_value=sample_document)
        db.get_document_chunks_by_doc_id = AsyncMock(return_value=[])
        db.get_documents = AsyncMock(return_value=([sample_document], 1))
        db.get_documents_multi_workspace = AsyncMock(return_value=([sample_document], 1))
        db.get_document_upload_fields = AsyncMock(
            return_value={
                "document_id": "doc-1",
                "workspace_id": "ws-1",
                "user_id": "user-1",
                "filename": "report.pdf",
                "original_filename": "report.pdf",
                "content_type": "application/pdf",
                "size_bytes": 2048,
                "storage_backend": "s3",
                "storage_path": "ws-1/report.pdf",
                "storage_bucket": "bucket",
                "storage_url": "s3://bucket/ws-1/report.pdf",
            }
        )
        db.create_or_reset_pending_document = AsyncMock(return_value=None)
        db.delete_document = AsyncMock(
            return_value={"document_id": "doc-1", "chunk_count": 3, "size_bytes": 2048}
        )

        from src.models.search import SearchResponse

        search = AsyncMock()
        search.search = AsyncMock(
            return_value=SearchResponse(
                results=[],
                query="q",
                total_results=0,
                processing_time_ms=1.0,
                search_mode="semantic",
            )
        )
        search.delete_document_vectors = AsyncMock(return_value=3)
        mq = AsyncMock()
        mq.publish = AsyncMock(return_value=None)
        storage = MagicMock()
        storage.delete_file = AsyncMock(return_value=None)

        args = {"api_key": "x", **_TOOL_ARGS[name]}
        with (
            patch.object(mcp_server, "get_database", AsyncMock(return_value=db)),
            patch.object(mcp_server, "get_search_service", AsyncMock(return_value=search)),
            patch(
                "src.services.mq.get_mq_service",
                new=AsyncMock(return_value=mq),
            ),
            # delete_document reaches the vector/object stores through the
            # deletion orchestrator, which resolves its own services.
            patch(
                "src.services.deletion.get_search_service",
                new=AsyncMock(return_value=search),
            ),
            patch(
                "src.services.deletion.get_storage_service",
                new=MagicMock(return_value=storage),
            ),
        ):
            content = await _call_tool(name, args)

        assert isinstance(content, list)
        assert content, f"{name}: empty content"
        assert all(isinstance(c, TextContent) for c in content)
        # And it must NOT be a permission/auth error on the happy path.
        assert not content[0].text.startswith(
            "Error: API key does not have"
        ), f"{name}: unexpected permission error on happy path"


# =========================================================================== #
# permission-denied path: returns an error AND never calls the service
# =========================================================================== #
class TestToolPermissionDenied:
    @pytest.mark.parametrize("name", ALL_TOOLS)
    async def test_permission_denied_returns_error_and_skips_service(self, name):
        """A key lacking the tool's required permission gets a clear error and
        the search/db work-doer is never reached."""
        required = _PERMISSION[name]
        key = _key(_DENY_KEY_PERMS[required])

        db = AsyncMock()
        db.validate_api_key = AsyncMock(return_value=key)
        # Spies that MUST NOT be touched once permission is denied.
        db.get_user_workspace_ids = AsyncMock(return_value=["ws-1"])
        db.get_document_by_id = AsyncMock()
        db.get_documents = AsyncMock()
        db.get_document_upload_fields = AsyncMock()
        search = AsyncMock()

        args = {"api_key": "x", **_TOOL_ARGS[name]}
        with (
            patch.object(mcp_server, "get_database", AsyncMock(return_value=db)),
            patch.object(mcp_server, "get_search_service", AsyncMock(return_value=search)),
        ):
            content = await _call_tool(name, args)

        assert isinstance(content, list) and content
        assert isinstance(content[0], TextContent)
        assert content[0].text == (f"Error: API key does not have '{required}' permission")
        # The body never ran: no search, no document/list/upload-field reads.
        search.search.assert_not_called()
        db.get_document_by_id.assert_not_called()
        db.get_documents.assert_not_called()
        db.get_document_upload_fields.assert_not_called()


# =========================================================================== #
# auth: missing / invalid key rejected before any tool body runs
# =========================================================================== #
class TestToolAuthentication:
    async def test_missing_api_key_is_rejected(self):
        """Omitting the required ``api_key`` is rejected before any tool body
        runs. The MCP server validates arguments against the tool inputSchema
        (where api_key is required), so the call returns a validation error and
        never reaches the search service / database."""
        search = AsyncMock()
        with (
            patch.object(mcp_server, "get_database", AsyncMock()),
            patch.object(mcp_server, "get_search_service", AsyncMock(return_value=search)),
        ):
            content = await _call_tool("search_documents", {"query": "q"})
        assert isinstance(content[0], TextContent)
        text = content[0].text.lower()
        assert "api_key" in text and ("required" in text or "valid" in text)
        search.search.assert_not_called()

    async def test_invalid_api_key_returns_error(self):
        db = AsyncMock()
        db.validate_api_key = AsyncMock(return_value=None)
        search = AsyncMock()
        with (
            patch.object(mcp_server, "get_database", AsyncMock(return_value=db)),
            patch.object(mcp_server, "get_search_service", AsyncMock(return_value=search)),
        ):
            content = await _call_tool("search_documents", {"api_key": "bad", "query": "q"})
        assert content[0].text == "Error: Invalid or expired API key"
        search.search.assert_not_called()
