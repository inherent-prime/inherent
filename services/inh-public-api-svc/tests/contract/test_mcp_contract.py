"""MCP tool contract regression tests (M6 #30).

Locks down the MCP agent surface so agents do not silently break. For each tool
(search_documents, search_memory, get_citations, verify_claim, explain_lineage,
refresh_stale_source, get_document_context, list_documents, get_document,
list_chunks) we assert:

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
    "report_feedback": {"required": ["api_key", "event_id", "verdict"]},
    "get_retrieval_health": {"required": ["api_key", "workspace_id"]},
    "delete_document": {"required": ["api_key", "document_id"]},
    "get_document": {"required": ["api_key", "document_id"]},
    "list_chunks": {"required": ["api_key", "document_id"]},
    "upload_document": {"required": ["api_key", "filename", "content"]},
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
    "report_feedback": "search",
    "get_retrieval_health": "search",
    "delete_document": "write",
    "get_document": "read",
    "list_chunks": "read",
    "upload_document": "write",
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
    "report_feedback": {"event_id": "ev_1", "verdict": "answered"},
    "get_retrieval_health": {"workspace_id": "ws-1"},
    "delete_document": {"document_id": "doc-1"},
    "get_document": {"document_id": "doc-1"},
    "list_chunks": {"document_id": "doc-1"},
    "upload_document": {"filename": "notes.md", "content": "# hello world"},
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

    async def test_upload_document_schema_types(self):
        """content_type is optional (text-only, binary uploads stay REST-only
        by design, #87). It carries NO schema `default` (#193 coordinator
        review): a JSON Schema `default` is advertised to the CLIENT, and
        many MCP clients/tool-calling layers pre-fill an omitted argument
        from its advertised default before the server ever sees the call --
        which would turn #197's filename-extension derivation into a fixed
        "text/markdown" for every upload regardless of the real extension,
        the exact defect #197 fixed. The fallback-to-text/markdown behavior
        for an unrecognized/absent extension is documented in
        `content_type`'s description text instead, which does not get
        auto-populated onto omitted calls. See
        `test_omitted_content_type_derives_from_filename_extension` and
        `test_explicit_content_type_is_never_overridden_by_extension` below
        for the behavior this schema shape protects."""
        tools = await _list_tools()
        schema = tools["upload_document"].inputSchema
        props = schema["properties"]
        assert props["filename"]["type"] == "string"
        assert props["content"]["type"] == "string"
        assert props["content_type"]["type"] == "string"
        assert "default" not in props["content_type"], (
            "content_type must not carry a schema 'default' -- see the "
            "docstring above for why a client-visible default reintroduces "
            "#197's bug for every MCP client that auto-fills omitted args "
            "from their advertised default."
        )
        assert "content_type" not in schema["required"]
        assert "workspace_id" in props


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
        db.get_document_id_by_content_hash = AsyncMock(return_value=None)
        db.get_document_id_by_filename = AsyncMock(return_value=None)
        db.get_eval_event = AsyncMock(
            return_value={
                "event_id": "ev_1",
                "workspace_id": "ws-1",
                "query_text": "refund policy",
                "search_mode": "hybrid",
                "result_doc_ids": ["doc-1"],
                "result_chunk_ids": ["chunk-1"],
            }
        )
        db.upsert_eval_feedback = AsyncMock(return_value=None)
        db.upsert_eval_case = AsyncMock(return_value="case_1")
        db.eval_scorecard_counts = AsyncMock(
            return_value={
                "captured_events": 10,
                "verdict_distribution": {},
                "feedback_distribution": {},
                "eval_case_count": 0,
                "corpus_gaps": [],
            }
        )
        db.get_last_eval_run = AsyncMock(return_value=None)
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
        storage.generate_key = MagicMock(return_value="ws-1/uuid/notes.md")
        storage.upload_file = AsyncMock(return_value=None)
        storage.build_storage_url = MagicMock(return_value="s3://bucket/ws-1/uuid/notes.md")
        storage._bucket = "bucket"

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
            # upload_document reaches storage/MQ through the shared
            # document_intake service (same one REST uses, #87 Task 3).
            patch(
                "src.services.document_intake.get_storage_service",
                new=MagicMock(return_value=storage),
            ),
            patch(
                "src.services.document_intake.get_mq_service",
                new=AsyncMock(return_value=mq),
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


# =========================================================================== #
# evals v1: report_feedback / get_retrieval_health (Task 10)
# =========================================================================== #
class TestEvalsMcpTools:
    """report_feedback and get_retrieval_health wrap submit_feedback /
    build_scorecard (evals v1) and go through the same permission-check path
    as every other tool (permission parity is covered generically above via
    ALL_TOOLS)."""

    def _key(self, permissions: list[str] = ("search",)) -> APIKeyInfo:
        return APIKeyInfo(
            key_id="key-1",
            user_id="user-1",
            workspace_id=None,
            permissions=list(permissions),  # type: ignore[arg-type]
            rate_limit=100,
            expires_at=None,
            status="active",
        )

    async def test_report_feedback_promotes_and_returns_case_id(self):
        db = AsyncMock()
        db.validate_api_key = AsyncMock(return_value=self._key())
        db.get_user_workspace_ids = AsyncMock(return_value=["ws-1"])
        db.get_eval_event = AsyncMock(
            return_value={
                "event_id": "ev_1",
                "workspace_id": "ws-1",
                "query_text": "refund policy",
                "search_mode": "hybrid",
                "result_doc_ids": ["doc-1"],
                "result_chunk_ids": ["chunk-1"],
            }
        )
        db.upsert_eval_feedback = AsyncMock(return_value=None)
        db.upsert_eval_case = AsyncMock(return_value="case_1")

        with patch.object(mcp_server, "get_database", AsyncMock(return_value=db)):
            content = await _call_tool(
                "report_feedback",
                {"api_key": "x", "event_id": "ev_1", "verdict": "answered"},
            )

        assert isinstance(content[0], TextContent)
        assert '"promoted"' in content[0].text
        assert '"case_1"' in content[0].text
        db.upsert_eval_feedback.assert_awaited_once()
        db.upsert_eval_case.assert_awaited_once()

    async def test_report_feedback_denied_without_search_permission(self):
        """A key lacking 'search' gets the standard permission error and the
        feedback service is never reached."""
        db = AsyncMock()
        db.validate_api_key = AsyncMock(return_value=self._key(["read"]))
        db.get_user_workspace_ids = AsyncMock(return_value=["ws-1"])
        db.get_eval_event = AsyncMock()

        with patch.object(mcp_server, "get_database", AsyncMock(return_value=db)):
            content = await _call_tool(
                "report_feedback",
                {"api_key": "x", "event_id": "ev_1", "verdict": "answered"},
            )

        assert content[0].text == "Error: API key does not have 'search' permission"
        db.get_eval_event.assert_not_called()

    async def test_report_feedback_unknown_event_names_event_id_in_error(self):
        db = AsyncMock()
        db.validate_api_key = AsyncMock(return_value=self._key())
        db.get_user_workspace_ids = AsyncMock(return_value=["ws-1"])
        db.get_eval_event = AsyncMock(return_value=None)

        with patch.object(mcp_server, "get_database", AsyncMock(return_value=db)):
            content = await _call_tool(
                "report_feedback",
                {"api_key": "x", "event_id": "ev_missing", "verdict": "answered"},
            )

        assert "ev_missing" in content[0].text
        assert content[0].text.startswith("Error:")

    async def test_get_retrieval_health_returns_scorecard_json(self):
        db = AsyncMock()
        db.validate_api_key = AsyncMock(return_value=self._key())
        db.get_user_workspace_ids = AsyncMock(return_value=["ws-1"])
        db.eval_scorecard_counts = AsyncMock(
            return_value={
                "captured_events": 10,
                "verdict_distribution": {},
                "feedback_distribution": {"answered": 2},
                "eval_case_count": 3,
                "corpus_gaps": [],
            }
        )
        db.get_last_eval_run = AsyncMock(return_value=None)

        with patch.object(mcp_server, "get_database", AsyncMock(return_value=db)):
            content = await _call_tool(
                "get_retrieval_health", {"api_key": "x", "workspace_id": "ws-1"}
            )

        assert isinstance(content[0], TextContent)
        assert '"summary"' in content[0].text
        assert (
            '"workspace_id":"ws-1"' in content[0].text
            or '"workspace_id": "ws-1"' in content[0].text
        )

    async def test_get_retrieval_health_rejects_foreign_workspace(self):
        """A workspace_id the key does not own is rejected before build_scorecard runs."""
        db = AsyncMock()
        db.validate_api_key = AsyncMock(return_value=self._key())
        db.get_user_workspace_ids = AsyncMock(return_value=["ws-owned"])
        db.eval_scorecard_counts = AsyncMock()

        with patch.object(mcp_server, "get_database", AsyncMock(return_value=db)):
            content = await _call_tool(
                "get_retrieval_health", {"api_key": "x", "workspace_id": "ws-foreign"}
            )

        assert content[0].text.startswith("Error:")
        # Wording unified with every other workspace-argument rejection
        # (#138 follow-up: describe_workspace_denial) — a user-scoped key
        # gets the generic "you don't have access" message.
        assert "don't have access" in content[0].text


# =========================================================================== #
# get_document / list_chunks (#87 API parity Task 2): REST GET /v1/documents/{id}
# and GET /v1/chunks/{document_id} equivalents. Access check mirrors
# _handle_get_context / _resolve_document_for_user: get_document_by_id then
# verify the caller owns the document's workspace, so a foreign document 404s
# without ever leaking its data.
# =========================================================================== #
class TestGetDocumentTool:
    def _key(self, permissions: list[str] = ("read",)) -> APIKeyInfo:
        return APIKeyInfo(
            key_id="key-1",
            user_id="user-1",
            workspace_id=None,
            permissions=list(permissions),  # type: ignore[arg-type]
            rate_limit=100,
            expires_at=None,
            status="active",
        )

    async def test_returns_document_metadata_as_json(self, sample_document):
        db = AsyncMock()
        db.validate_api_key = AsyncMock(return_value=self._key())
        db.get_user_workspace_ids = AsyncMock(return_value=["ws-1"])
        db.get_document_by_id = AsyncMock(return_value=sample_document)

        with patch.object(mcp_server, "get_database", AsyncMock(return_value=db)):
            content = await _call_tool("get_document", {"api_key": "x", "document_id": "doc-1"})

        assert isinstance(content[0], TextContent)
        assert '"doc-1"' in content[0].text
        assert '"report.pdf"' in content[0].text
        assert '"ws-1"' in content[0].text
        db.get_document_by_id.assert_awaited_once_with("doc-1")

    async def test_unknown_document_returns_not_found_error(self):
        db = AsyncMock()
        db.validate_api_key = AsyncMock(return_value=self._key())
        db.get_user_workspace_ids = AsyncMock(return_value=["ws-1"])
        db.get_document_by_id = AsyncMock(return_value=None)

        with patch.object(mcp_server, "get_database", AsyncMock(return_value=db)):
            content = await _call_tool(
                "get_document", {"api_key": "x", "document_id": "doc-missing"}
            )

        assert content[0].text.startswith("Error:")
        assert "doc-missing" in content[0].text

    async def test_cross_workspace_document_denies_access_without_leak(self, sample_document):
        """A document belonging to a workspace the caller does not own must not
        leak its metadata — mirrors _resolve_document_for_user's access check.

        Answers with the SAME undifferentiated "not found" used for a document
        that doesn't exist at all (#138 blocker-1 follow-up), not a
        distinguishable "you don't have access" — that distinction is a
        cross-workspace existence oracle. See
        tests/security/test_mcp_workspace_boundaries.py for the paired
        not-found-vs-unauthorized proof.
        """
        foreign = sample_document.model_copy(update={"workspace_id": "ws-foreign"})
        db = AsyncMock()
        db.validate_api_key = AsyncMock(return_value=self._key())
        db.get_user_workspace_ids = AsyncMock(return_value=["ws-owned"])
        db.get_document_by_id = AsyncMock(return_value=foreign)

        with patch.object(mcp_server, "get_database", AsyncMock(return_value=db)):
            content = await _call_tool("get_document", {"api_key": "x", "document_id": "doc-1"})

        assert content[0].text == "Error: Document 'doc-1' not found"
        # No leaked document fields (e.g. the foreign workspace id) in the error.
        assert "ws-foreign" not in content[0].text

    async def test_denied_without_read_permission(self):
        db = AsyncMock()
        db.validate_api_key = AsyncMock(return_value=self._key(["search"]))
        db.get_document_by_id = AsyncMock()

        with patch.object(mcp_server, "get_database", AsyncMock(return_value=db)):
            content = await _call_tool("get_document", {"api_key": "x", "document_id": "doc-1"})

        assert content[0].text == "Error: API key does not have 'read' permission"
        db.get_document_by_id.assert_not_called()


class TestListChunksTool:
    def _key(self, permissions: list[str] = ("read",)) -> APIKeyInfo:
        return APIKeyInfo(
            key_id="key-1",
            user_id="user-1",
            workspace_id=None,
            permissions=list(permissions),  # type: ignore[arg-type]
            rate_limit=100,
            expires_at=None,
            status="active",
        )

    async def test_returns_chunk_list_as_json(self, sample_document):
        from src.models.document import DocumentChunk

        chunks = [
            DocumentChunk(id="chunk-1", document_id="doc-1", content="hello", chunk_index=0),
            DocumentChunk(id="chunk-2", document_id="doc-1", content="world", chunk_index=1),
        ]
        db = AsyncMock()
        db.validate_api_key = AsyncMock(return_value=self._key())
        db.get_user_workspace_ids = AsyncMock(return_value=["ws-1"])
        db.get_document_by_id = AsyncMock(return_value=sample_document)
        db.get_document_chunks_by_doc_id = AsyncMock(return_value=chunks)

        with patch.object(mcp_server, "get_database", AsyncMock(return_value=db)):
            content = await _call_tool("list_chunks", {"api_key": "x", "document_id": "doc-1"})

        assert isinstance(content[0], TextContent)
        assert '"chunk-1"' in content[0].text
        assert '"chunk-2"' in content[0].text
        assert '"hello"' in content[0].text
        db.get_document_chunks_by_doc_id.assert_awaited_once_with("doc-1")

    async def test_unknown_document_returns_not_found_error(self):
        db = AsyncMock()
        db.validate_api_key = AsyncMock(return_value=self._key())
        db.get_user_workspace_ids = AsyncMock(return_value=["ws-1"])
        db.get_document_by_id = AsyncMock(return_value=None)
        db.get_document_chunks_by_doc_id = AsyncMock()

        with patch.object(mcp_server, "get_database", AsyncMock(return_value=db)):
            content = await _call_tool(
                "list_chunks", {"api_key": "x", "document_id": "doc-missing"}
            )

        assert content[0].text.startswith("Error:")
        assert "doc-missing" in content[0].text
        db.get_document_chunks_by_doc_id.assert_not_called()

    async def test_cross_workspace_document_denies_access_without_leak(self, sample_document):
        """Undifferentiated not-found, matching the missing-document case
        above — not a distinguishable "you don't have access" (#138
        blocker-1 follow-up: that distinction was a cross-workspace
        existence oracle)."""
        foreign = sample_document.model_copy(update={"workspace_id": "ws-foreign"})
        db = AsyncMock()
        db.validate_api_key = AsyncMock(return_value=self._key())
        db.get_user_workspace_ids = AsyncMock(return_value=["ws-owned"])
        db.get_document_by_id = AsyncMock(return_value=foreign)
        db.get_document_chunks_by_doc_id = AsyncMock()

        with patch.object(mcp_server, "get_database", AsyncMock(return_value=db)):
            content = await _call_tool("list_chunks", {"api_key": "x", "document_id": "doc-1"})

        assert content[0].text == "Error: Document 'doc-1' not found"
        db.get_document_chunks_by_doc_id.assert_not_called()

    async def test_denied_without_read_permission(self):
        db = AsyncMock()
        db.validate_api_key = AsyncMock(return_value=self._key(["search"]))
        db.get_document_by_id = AsyncMock()
        db.get_document_chunks_by_doc_id = AsyncMock()

        with patch.object(mcp_server, "get_database", AsyncMock(return_value=db)):
            content = await _call_tool("list_chunks", {"api_key": "x", "document_id": "doc-1"})

        assert content[0].text == "Error: API key does not have 'read' permission"
        db.get_document_by_id.assert_not_called()
        db.get_document_chunks_by_doc_id.assert_not_called()
        db.eval_scorecard_counts.assert_not_called()


def test_mcp_supported_text_mime_types_matches_registry():
    """#117: SUPPORTED_TEXT_MIME_TYPES must be exactly the registry's
    mcp-surfaced MIME types, not a re-derived guess. Before #117 this was a
    ``.startswith("text/")`` filter over ALLOWED_MIME_TYPES -- correct only
    by coincidence, since nothing enforced that every text/* type was
    actually MCP-safe or that no non-text/* type ever should be. Pinning
    equality here means the registry's explicit `surfaces` field is the only
    place this can be decided."""
    from inh_contracts.file_types import mcp_mime_types

    assert mcp_server.SUPPORTED_TEXT_MIME_TYPES == mcp_mime_types()


# =========================================================================== #
# upload_document (#87 API parity Task 3): text-only counterpart of
# POST /v1/documents. Binary uploads stay REST-only by design — content_type
# must be a text/* MIME type or the tool errors and points the caller at the
# REST endpoint. Shares src/services/document_intake.intake_document with
# REST so validation, dedup, storage and MQ publish never drift.
# =========================================================================== #
class TestUploadDocumentTool:
    def _key(self, permissions: list[str] = ("write",)) -> APIKeyInfo:
        return APIKeyInfo(
            key_id="key-1",
            user_id="user-1",
            workspace_id=None,
            permissions=list(permissions),  # type: ignore[arg-type]
            rate_limit=100,
            expires_at=None,
            status="active",
        )

    def _storage(self) -> MagicMock:
        storage = MagicMock()
        storage.generate_key = MagicMock(return_value="ws-1/uuid/notes.md")
        storage.upload_file = AsyncMock(return_value=None)
        storage.build_storage_url = MagicMock(return_value="s3://bucket/ws-1/uuid/notes.md")
        storage._bucket = "bucket"
        return storage

    def _mq(self) -> AsyncMock:
        mq = AsyncMock()
        mq.publish = AsyncMock(return_value=None)
        return mq

    def _intake_patches(self, storage, mq):
        return (
            patch(
                "src.services.document_intake.get_storage_service",
                new=MagicMock(return_value=storage),
            ),
            patch(
                "src.services.document_intake.get_mq_service",
                new=AsyncMock(return_value=mq),
            ),
        )

    async def test_upload_happy_path_returns_pending_doc_json(self):
        """A single-workspace caller uploads text content and gets back the
        DocumentUploadResponse JSON with status='pending' — same shape as
        POST /v1/documents."""
        db = AsyncMock()
        db.validate_api_key = AsyncMock(return_value=self._key())
        db.get_user_workspace_ids = AsyncMock(return_value=["ws-1"])
        db.get_document_id_by_content_hash = AsyncMock(return_value=None)
        db.get_document_id_by_filename = AsyncMock(return_value=None)
        db.create_or_reset_pending_document = AsyncMock(return_value=None)

        storage = self._storage()
        mq = self._mq()
        p1, p2 = self._intake_patches(storage, mq)

        with patch.object(mcp_server, "get_database", AsyncMock(return_value=db)), p1, p2:
            content = await _call_tool(
                "upload_document",
                {"api_key": "x", "filename": "notes.md", "content": "# hello world"},
            )

        assert isinstance(content[0], TextContent)
        assert '"status":"pending"' in content[0].text or '"status": "pending"' in content[0].text
        assert '"workspace_id":"ws-1"' in content[0].text or '"workspace_id": "ws-1"' in (
            content[0].text
        )
        assert '"notes.md"' in content[0].text
        mq.publish.assert_awaited_once()
        storage.upload_file.assert_awaited_once()

    @pytest.mark.parametrize(
        "filename,content_type,content",
        [
            # #121: structured text
            ("config.yaml", "application/yaml", "service: inherent"),
            ("config.toml", "application/toml", 'service = "inherent"'),
            ("config.xml", "application/xml", "<service>inherent</service>"),
            # #122: source code
            ("main.py", "text/x-python", "def main():\n    pass\n"),
            # #127: subtitle transcripts
            (
                "talk.srt",
                "application/x-subrip",
                "1\n00:00:00,000 --> 00:00:03,000\nHello there.\n",
            ),
            (
                "talk.vtt",
                "text/vtt",
                "WEBVTT\n\n00:00:00.000 --> 00:00:03.000\nHello there.\n",
            ),
        ],
    )
    async def test_new_text_family_types_accepted(self, filename, content_type, content):
        """#121/#122/#127: all three text-family additions are `rest+mcp` --
        MCP `upload_document` must accept them, not just REST. Pins the MCP
        half of each issue's 'MCP upload accepted' acceptance criterion."""
        db = AsyncMock()
        db.validate_api_key = AsyncMock(return_value=self._key())
        db.get_user_workspace_ids = AsyncMock(return_value=["ws-1"])
        db.get_document_id_by_content_hash = AsyncMock(return_value=None)
        db.get_document_id_by_filename = AsyncMock(return_value=None)
        db.create_or_reset_pending_document = AsyncMock(return_value=None)

        storage = self._storage()
        mq = self._mq()
        p1, p2 = self._intake_patches(storage, mq)

        with patch.object(mcp_server, "get_database", AsyncMock(return_value=db)), p1, p2:
            result = await _call_tool(
                "upload_document",
                {
                    "api_key": "x",
                    "filename": filename,
                    "content": content,
                    "content_type": content_type,
                },
            )

        assert not result[0].text.startswith(
            "Error:"
        ), f"{content_type} should be MCP-accepted, got: {result[0].text}"
        mq.publish.assert_awaited_once()
        storage.upload_file.assert_awaited_once()

    @pytest.mark.parametrize(
        "filename,expected_content_type",
        [
            ("notes.txt", "text/plain"),
            ("data.csv", "text/csv"),
            ("page.html", "text/html"),
            ("notes.md", "text/markdown"),
            ("notes", "text/markdown"),  # no extension -> the historical default
            ("notes.log", "text/markdown"),  # unrecognized extension -> default
            # #197: the "code" spec pools 22 MIME aliases across 21 distinct
            # languages under ONE registry entry -- `mime_types[0]` used to
            # answer "text/x-python" for every one of these regardless of
            # the real language (the issue's own verified repro list).
            ("app.js", "text/javascript"),
            ("lib.go", "text/x-go"),
            ("Main.java", "text/x-java-source"),
            ("q.sql", "application/sql"),
            ("s.sh", "application/x-sh"),
            ("x.rs", "text/x-rustsrc"),
        ],
    )
    async def test_omitted_content_type_derives_from_filename_extension(
        self, filename, expected_content_type
    ):
        """#117 review BLOCKER 2: the schema advertises 'content_type'
        defaulting to text/markdown, but a flat default broke itself the
        moment the extension-consistency check landed -- calling
        upload_document(filename="notes.txt", ...) and omitting the optional
        content_type (exactly as the schema invites) must NOT self-reject.
        The default is now derived from the filename's extension, falling
        back to text/markdown only when the extension is absent/unknown.

        Extended for #197 (review of #121/#122/#127): the "code" spec's
        added multi-MIME shape broke the "one MIME type per spec" assumption
        this default derivation previously relied on -- see
        `_default_upload_content_type` and
        `inh_contracts.file_types.mime_type_for_extension`.
        """
        db = AsyncMock()
        db.validate_api_key = AsyncMock(return_value=self._key())
        db.get_user_workspace_ids = AsyncMock(return_value=["ws-1"])
        db.get_document_id_by_content_hash = AsyncMock(return_value=None)
        db.get_document_id_by_filename = AsyncMock(return_value=None)
        db.create_or_reset_pending_document = AsyncMock(return_value=None)

        storage = self._storage()
        mq = self._mq()
        p1, p2 = self._intake_patches(storage, mq)

        with patch.object(mcp_server, "get_database", AsyncMock(return_value=db)), p1, p2:
            content = await _call_tool(
                "upload_document",
                {"api_key": "x", "filename": filename, "content": "some content"},
            )

        assert not content[0].text.startswith("Error"), content[0].text
        assert f'"mime_type":"{expected_content_type}"' in content[0].text or (
            f'"mime_type": "{expected_content_type}"' in content[0].text
        )

    async def test_go_file_with_omitted_content_type_is_not_labelled_python(self):
        """#197 regression, asserted directly against the value persisted to
        storage/DB (not just the JSON response body): a .go file with
        `content_type` omitted must resolve to 'text/x-go', never
        'text/x-python' -- the exact defect the issue reports. Checked at
        `create_or_reset_pending_document`'s `content_type` kwarg (what
        actually lands in the document's stored metadata and the MQ message
        `extract_text` reads `content_type` from) so this can't pass on a
        response-body coincidence alone."""
        db = AsyncMock()
        db.validate_api_key = AsyncMock(return_value=self._key())
        db.get_user_workspace_ids = AsyncMock(return_value=["ws-1"])
        db.get_document_id_by_content_hash = AsyncMock(return_value=None)
        db.get_document_id_by_filename = AsyncMock(return_value=None)
        db.create_or_reset_pending_document = AsyncMock(return_value=None)

        storage = self._storage()
        mq = self._mq()
        p1, p2 = self._intake_patches(storage, mq)

        with patch.object(mcp_server, "get_database", AsyncMock(return_value=db)), p1, p2:
            content = await _call_tool(
                "upload_document",
                {"api_key": "x", "filename": "lib.go", "content": "package main\n"},
            )

        assert not content[0].text.startswith("Error"), content[0].text
        stored_content_type = db.create_or_reset_pending_document.call_args.kwargs["content_type"]
        assert stored_content_type == "text/x-go"
        assert stored_content_type != "text/x-python"

    async def test_explicit_content_type_is_never_overridden_by_extension(self):
        """Coordinator adversarial-review regression pin (#193 blocker): an
        EXPLICITLY declared content_type must always be honored as-is, even
        when it disagrees with what the filename's extension would have
        derived. This is the deliberate flip side of removing the schema's
        `"default": "text/markdown"` -- that default was REMOVED (not just
        left undocumented) specifically because a JSON Schema `default` is
        advertised to the CLIENT, and several real MCP clients / tool-calling
        layers pre-fill an omitted argument from its advertised default
        before the server ever observes an omission. With the default
        present, `content_type = declared_content_type or
        _default_upload_content_type(filename)` received an explicit
        "text/markdown" for EVERY upload whose caller omitted content_type
        (not just genuinely-ambiguous ones), short-circuiting #197's
        extension derivation entirely -- reproduced on this repo pre-fix:
        uploading `main.go` this way stored content_type=text/markdown /
        chunking_hint=prose instead of text/x-go / code.

        Decision (explicit stated by review): an explicit declaration is
        NEVER second-guessed against the filename -- this test pins that a
        caller who legitimately wants "text/markdown" for a .go file (e.g.
        a markdown-fenced code snippet saved with a misleading name) still
        gets it. `check_extension_consistency` only rejects a BINARY-format
        extension (`magic is not None`) declared under a mismatched type;
        .go's "code" spec has no magic signature, so any declared text
        content_type is accepted for it by design (see that function's own
        docstring)."""
        db = AsyncMock()
        db.validate_api_key = AsyncMock(return_value=self._key())
        db.get_user_workspace_ids = AsyncMock(return_value=["ws-1"])
        db.get_document_id_by_content_hash = AsyncMock(return_value=None)
        db.get_document_id_by_filename = AsyncMock(return_value=None)
        db.create_or_reset_pending_document = AsyncMock(return_value=None)

        storage = self._storage()
        mq = self._mq()
        p1, p2 = self._intake_patches(storage, mq)

        with patch.object(mcp_server, "get_database", AsyncMock(return_value=db)), p1, p2:
            content = await _call_tool(
                "upload_document",
                {
                    "api_key": "x",
                    "filename": "main.go",
                    "content": "package main\n",
                    "content_type": "text/markdown",
                },
            )

        assert not content[0].text.startswith("Error"), content[0].text
        stored_content_type = db.create_or_reset_pending_document.call_args.kwargs["content_type"]
        assert stored_content_type == "text/markdown", (
            "an EXPLICIT content_type must be honored as-is, never re-derived "
            "from the filename extension"
        )
        assert stored_content_type != "text/x-go"

    async def test_legacy_doc_rejected_with_explicit_content_type(self):
        """#124/#126 review blocker 3: application/msword must get the same
        actionable "convert to .docx" message as REST, not the generic
        SUPPORTED_TEXT_MIME_TYPES allow-list dump."""
        db = AsyncMock()
        db.validate_api_key = AsyncMock(return_value=self._key())
        db.get_user_workspace_ids = AsyncMock(return_value=["ws-1"])
        db.create_or_reset_pending_document = AsyncMock()

        storage = self._storage()
        mq = self._mq()
        p1, p2 = self._intake_patches(storage, mq)

        with patch.object(mcp_server, "get_database", AsyncMock(return_value=db)), p1, p2:
            content = await _call_tool(
                "upload_document",
                {
                    "api_key": "x",
                    "filename": "report.doc",
                    "content": "pasted document text",
                    "content_type": "application/msword",
                },
            )

        assert content[0].text.startswith("Error")
        assert ".docx" in content[0].text
        storage.upload_file.assert_not_awaited()
        db.create_or_reset_pending_document.assert_not_awaited()

    async def test_legacy_doc_rejected_even_with_content_type_omitted(self):
        """The exact accept-then-garble gap the review found: with
        content_type omitted, `_default_upload_content_type("report.doc")`
        used to fall through to 'text/markdown' (MCP-eligible) since '.doc'
        has no FILE_TYPE_REGISTRY entry -- silently accepting and indexing
        the exact format #126 says must be rejected. The extension itself
        must be checked, not just a declared MIME type."""
        db = AsyncMock()
        db.validate_api_key = AsyncMock(return_value=self._key())
        db.get_user_workspace_ids = AsyncMock(return_value=["ws-1"])
        db.get_document_id_by_content_hash = AsyncMock(return_value=None)
        db.get_document_id_by_filename = AsyncMock(return_value=None)
        db.create_or_reset_pending_document = AsyncMock()

        storage = self._storage()
        mq = self._mq()
        p1, p2 = self._intake_patches(storage, mq)

        with patch.object(mcp_server, "get_database", AsyncMock(return_value=db)), p1, p2:
            content = await _call_tool(
                "upload_document",
                {
                    "api_key": "x",
                    "filename": "report.doc",
                    "content": (
                        "Q3 revenue was 4.2M and the CEO approved the layoffs, "
                        "pasted straight from a .doc file"
                    ),
                },
            )

        assert content[0].text.startswith("Error"), content[0].text
        assert ".docx" in content[0].text
        storage.upload_file.assert_not_awaited()
        db.create_or_reset_pending_document.assert_not_awaited()

    async def test_outlook_msg_rejected_even_with_content_type_omitted(self):
        db = AsyncMock()
        db.validate_api_key = AsyncMock(return_value=self._key())
        db.get_user_workspace_ids = AsyncMock(return_value=["ws-1"])
        db.get_document_id_by_content_hash = AsyncMock(return_value=None)
        db.get_document_id_by_filename = AsyncMock(return_value=None)
        db.create_or_reset_pending_document = AsyncMock()

        storage = self._storage()
        mq = self._mq()
        p1, p2 = self._intake_patches(storage, mq)

        with patch.object(mcp_server, "get_database", AsyncMock(return_value=db)), p1, p2:
            content = await _call_tool(
                "upload_document",
                {"api_key": "x", "filename": "message.msg", "content": "pasted email text"},
            )

        assert content[0].text.startswith("Error"), content[0].text
        assert ".eml" in content[0].text
        storage.upload_file.assert_not_awaited()
        db.create_or_reset_pending_document.assert_not_awaited()

    async def test_write_permission_denied(self):
        """A key without 'write' gets the standard permission error and never
        reaches storage/db."""
        db = AsyncMock()
        db.validate_api_key = AsyncMock(return_value=self._key(["read", "search"]))
        db.get_user_workspace_ids = AsyncMock(return_value=["ws-1"])
        db.create_or_reset_pending_document = AsyncMock()

        with patch.object(mcp_server, "get_database", AsyncMock(return_value=db)):
            content = await _call_tool(
                "upload_document",
                {"api_key": "x", "filename": "notes.md", "content": "# hello world"},
            )

        assert content[0].text == "Error: API key does not have 'write' permission"
        db.create_or_reset_pending_document.assert_not_called()

    async def test_binary_content_type_rejected_with_rest_only_message(self):
        """A non-text/* content_type is rejected before storage/db are ever
        touched, and the error directs the caller to REST."""
        db = AsyncMock()
        db.validate_api_key = AsyncMock(return_value=self._key())
        db.get_user_workspace_ids = AsyncMock(return_value=["ws-1"])
        db.create_or_reset_pending_document = AsyncMock()

        with patch.object(mcp_server, "get_database", AsyncMock(return_value=db)):
            content = await _call_tool(
                "upload_document",
                {
                    "api_key": "x",
                    "filename": "report.pdf",
                    "content": "not really a pdf",
                    "content_type": "application/pdf",
                },
            )

        assert content[0].text.startswith("Error:")
        assert "REST" in content[0].text
        db.create_or_reset_pending_document.assert_not_called()

    async def test_unsupported_text_content_type_rejected_at_mcp_boundary(self):
        """A text/* subtype that is NOT in the shared allow-list (e.g.
        text/rtf) is rejected at the MCP gate with the supported-types
        message — not passed through to intake for a confusing two-step
        rejection (#87 review S1).

        text/xml was this test's original example, but #121 registered XML
        (`application/xml`/`text/xml`) as `rest+mcp` -- it is now a
        legitimately MCP-eligible type (see
        TestUploadDocumentTool::test_new_text_family_types_accepted below),
        so it no longer demonstrates "unsupported". text/rtf remains
        genuinely unregistered."""
        db = AsyncMock()
        db.validate_api_key = AsyncMock(return_value=self._key())
        db.get_user_workspace_ids = AsyncMock(return_value=["ws-1"])
        db.create_or_reset_pending_document = AsyncMock()

        with patch.object(mcp_server, "get_database", AsyncMock(return_value=db)):
            content = await _call_tool(
                "upload_document",
                {
                    "api_key": "x",
                    "filename": "data.rtf",
                    "content": "{\\rtf1 hello}",
                    "content_type": "text/rtf",
                },
            )

        assert content[0].text.startswith("Error:")
        assert "text/markdown" in content[0].text  # names the supported set
        db.create_or_reset_pending_document.assert_not_called()

    async def test_empty_content_rejected(self):
        db = AsyncMock()
        db.validate_api_key = AsyncMock(return_value=self._key())
        db.get_user_workspace_ids = AsyncMock(return_value=["ws-1"])
        db.create_or_reset_pending_document = AsyncMock()

        with patch.object(mcp_server, "get_database", AsyncMock(return_value=db)):
            content = await _call_tool(
                "upload_document",
                {"api_key": "x", "filename": "notes.md", "content": ""},
            )

        assert content[0].text.startswith("Error:")
        db.create_or_reset_pending_document.assert_not_called()

    async def test_no_workspace_returns_error(self):
        """A caller who owns zero workspaces cannot upload anywhere."""
        db = AsyncMock()
        db.validate_api_key = AsyncMock(return_value=self._key())
        db.get_user_workspace_ids = AsyncMock(return_value=[])

        with patch.object(mcp_server, "get_database", AsyncMock(return_value=db)):
            content = await _call_tool(
                "upload_document",
                {"api_key": "x", "filename": "notes.md", "content": "# hello world"},
            )

        assert content[0].text.startswith("Error:")

    async def test_multiple_workspaces_without_workspace_id_returns_error(self):
        """A caller owning multiple workspaces must disambiguate via
        workspace_id — uploading needs exactly one target."""
        db = AsyncMock()
        db.validate_api_key = AsyncMock(return_value=self._key())
        db.get_user_workspace_ids = AsyncMock(return_value=["ws-1", "ws-2"])
        db.create_or_reset_pending_document = AsyncMock()

        with patch.object(mcp_server, "get_database", AsyncMock(return_value=db)):
            content = await _call_tool(
                "upload_document",
                {"api_key": "x", "filename": "notes.md", "content": "# hello world"},
            )

        assert content[0].text.startswith("Error:")
        db.create_or_reset_pending_document.assert_not_called()

    async def test_multiple_workspaces_with_explicit_workspace_id_succeeds(self):
        """Passing workspace_id disambiguates among multiple owned workspaces."""
        db = AsyncMock()
        db.validate_api_key = AsyncMock(return_value=self._key())
        db.get_user_workspace_ids = AsyncMock(return_value=["ws-1", "ws-2"])
        db.get_document_id_by_content_hash = AsyncMock(return_value=None)
        db.get_document_id_by_filename = AsyncMock(return_value=None)
        db.create_or_reset_pending_document = AsyncMock(return_value=None)

        storage = self._storage()
        mq = self._mq()
        p1, p2 = self._intake_patches(storage, mq)

        with patch.object(mcp_server, "get_database", AsyncMock(return_value=db)), p1, p2:
            content = await _call_tool(
                "upload_document",
                {
                    "api_key": "x",
                    "filename": "notes.md",
                    "content": "# hello world",
                    "workspace_id": "ws-2",
                },
            )

        assert '"workspace_id":"ws-2"' in content[0].text or '"workspace_id": "ws-2"' in (
            content[0].text
        )

    async def test_foreign_workspace_id_denied(self):
        """A workspace_id the caller does not own is rejected — tenant
        scoping, same convention as _get_workspace_ids elsewhere."""
        db = AsyncMock()
        db.validate_api_key = AsyncMock(return_value=self._key())
        db.get_user_workspace_ids = AsyncMock(return_value=["ws-owned"])
        db.create_or_reset_pending_document = AsyncMock()

        with patch.object(mcp_server, "get_database", AsyncMock(return_value=db)):
            content = await _call_tool(
                "upload_document",
                {
                    "api_key": "x",
                    "filename": "notes.md",
                    "content": "# hello world",
                    "workspace_id": "ws-foreign",
                },
            )

        assert content[0].text.startswith("Error:")
        assert "don't have access" in content[0].text
        db.create_or_reset_pending_document.assert_not_called()
