"""Unit tests for the MCP agent-surface tools (#14 + #40).

Offline: ``get_database`` / ``get_search_service`` / MQ are patched at the
module boundary (same technique as tests/security/test_mcp_workspace_boundaries).
These cover:

- permission parity (#14): a key missing a tool's permission gets an error and
  the tool body NEVER executes (search / verify / db / mq services untouched);
- search-feature parity (#14): search_documents passes the new params through to
  the shared SearchRequest builder;
- the 5 memory primitives (#40) return the expected structured shapes.
"""

from __future__ import annotations

import datetime as _dt
import json
from unittest.mock import AsyncMock, patch

import pytest

from src.mcp_server import server as mcp_server
from src.models.api_key import APIKeyInfo
from src.models.citation import Citation
from src.models.document import Document, DocumentChunk
from src.models.search import SearchResponse, SearchResult

pytestmark = pytest.mark.asyncio


def _key(*, permissions: list[str], user_id: str = "user-1") -> APIKeyInfo:
    return APIKeyInfo(
        key_id="key-1",
        user_id=user_id,
        workspace_id=None,
        permissions=permissions,  # type: ignore[arg-type]
        rate_limit=100,
        expires_at=None,
        status="active",
    )


def _structured_payload(result) -> dict:
    """Extract the JSON ``structured`` block embedded in a TextContent reply."""
    text = result[0].text
    block = text.split("```json", 1)[1].rsplit("```", 1)[0].strip()
    return json.loads(block)["structured"]


def _search_result(doc_id: str = "doc-1") -> SearchResult:
    return SearchResult(
        chunk_id="chunk-1",
        document_id=doc_id,
        document_name="report.pdf",
        content="Paris is the capital of France.",
        score=0.91,
        score_source="vector",
        source_uri="s3://bucket/report.pdf",
        content_hash="abc123",
        citation=Citation(
            chunk_id="chunk-1",
            document_id=doc_id,
            document_name="report.pdf",
            content="Paris is the capital of France.",
            score=0.91,
            score_source="vector",
        ),
    )


def _patch(mock_db: AsyncMock, mock_search: AsyncMock | None = None):
    patches = [patch.object(mcp_server, "get_database", AsyncMock(return_value=mock_db))]
    if mock_search is not None:
        patches.append(
            patch.object(mcp_server, "get_search_service", AsyncMock(return_value=mock_search))
        )
    return patches


async def _call(name: str, arguments: dict, mock_db, mock_search=None):
    """Drive a tool through the real call_tool dispatcher (auth + perm check)."""
    server = mcp_server.create_mcp_server()
    # The decorated call_tool is registered on the server; invoke it through the
    # SDK request handler to exercise auth + permission gates end-to-end.
    mock_db.validate_api_key = AsyncMock(return_value=arguments.pop("_key_info"))
    ctx = _patch(mock_db, mock_search)
    from contextlib import ExitStack

    with ExitStack() as stack:
        for p in ctx:
            stack.enter_context(p)
        return await _dispatch(server, name, arguments)


async def _dispatch(server, name, arguments):
    # mcp Server stores the registered call_tool under its tool-call handler.
    # We call the underlying coroutine the SDK would call.
    from mcp.types import CallToolRequest, CallToolRequestParams

    req = CallToolRequest(
        method="tools/call",
        params=CallToolRequestParams(name=name, arguments=arguments),
    )
    handler = server.request_handlers[CallToolRequest]
    result = await handler(req)
    # ServerResult wraps the tool output; pull out the content list.
    return result.root.content


# --------------------------------------------------------------------------
# Permission parity (#14)
# --------------------------------------------------------------------------


class TestPermissionParity:
    async def test_search_denied_without_search_perm_never_searches(self):
        mock_db = AsyncMock()
        mock_db.get_user_workspace_ids = AsyncMock(return_value=["ws-1"])
        mock_search = AsyncMock()
        result = await _call(
            "search_documents",
            {"api_key": "k", "query": "x", "_key_info": _key(permissions=["read"])},
            mock_db,
            mock_search,
        )
        assert "does not have 'search' permission" in result[0].text
        mock_search.search.assert_not_called()

    async def test_verify_denied_without_read_perm_never_verifies(self):
        mock_db = AsyncMock()
        with patch.object(mcp_server, "verify_claim") as spy:
            result = await _call(
                "verify_claim",
                {
                    "api_key": "k",
                    "claim": "c",
                    "evidence": ["e"],
                    "_key_info": _key(permissions=["search"]),
                },
                mock_db,
            )
        assert "does not have 'read' permission" in result[0].text
        spy.assert_not_called()

    async def test_refresh_denied_without_write_perm_never_publishes(self):
        mock_db = AsyncMock()
        result = await _call(
            "refresh_stale_source",
            {"api_key": "k", "document_id": "doc-1", "_key_info": _key(permissions=["read"])},
            mock_db,
        )
        assert "does not have 'write' permission" in result[0].text
        mock_db.get_document_by_id.assert_not_called()

    async def test_explain_lineage_denied_without_read_perm(self):
        mock_db = AsyncMock()
        result = await _call(
            "explain_lineage",
            {"api_key": "k", "document_id": "doc-1", "_key_info": _key(permissions=["search"])},
            mock_db,
        )
        assert "does not have 'read' permission" in result[0].text
        mock_db.get_document_by_id.assert_not_called()

    async def test_get_citations_denied_without_search_perm(self):
        mock_db = AsyncMock()
        mock_search = AsyncMock()
        result = await _call(
            "get_citations",
            {"api_key": "k", "query": "x", "_key_info": _key(permissions=["read"])},
            mock_db,
            mock_search,
        )
        assert "does not have 'search' permission" in result[0].text
        mock_search.search.assert_not_called()


# --------------------------------------------------------------------------
# Search-feature parity (#14)
# --------------------------------------------------------------------------


class TestSearchParity:
    async def test_search_passes_new_params_to_request(self):
        mock_db = AsyncMock()
        mock_db.get_user_workspace_ids = AsyncMock(return_value=["ws-1"])
        mock_search = AsyncMock()
        mock_search.search = AsyncMock(
            return_value=SearchResponse(
                results=[_search_result()],
                query="x",
                total_results=1,
                processing_time_ms=1.0,
                search_mode="hybrid",
            )
        )
        await _call(
            "search_documents",
            {
                "api_key": "k",
                "query": "x",
                "limit": 5,
                "min_score": 0.3,
                "search_mode": "hybrid",
                "alpha": 0.2,
                "document_ids": ["doc-9"],
                "include_context": True,
                "context_window": 3,
                "_key_info": _key(permissions=["search"]),
            },
            mock_db,
            mock_search,
        )
        mock_search.search.assert_awaited_once()
        _ws, _user, request = mock_search.search.await_args.args
        assert request.limit == 5
        assert request.min_score == 0.3
        assert request.search_mode == "hybrid"
        assert request.alpha == 0.2
        assert request.document_ids == ["doc-9"]
        assert request.include_context is True
        assert request.context_window == 3

    async def test_search_memory_returns_structured_results(self):
        mock_db = AsyncMock()
        mock_db.get_user_workspace_ids = AsyncMock(return_value=["ws-1"])
        mock_search = AsyncMock()
        mock_search.search = AsyncMock(
            return_value=SearchResponse(
                results=[_search_result()],
                query="x",
                total_results=1,
                processing_time_ms=1.0,
                search_mode="semantic",
            )
        )
        result = await _call(
            "search_memory",
            {"api_key": "k", "query": "x", "_key_info": _key(permissions=["search"])},
            mock_db,
            mock_search,
        )
        payload = _structured_payload(result)
        assert payload["query"] == "x"
        assert payload["results"][0]["document_id"] == "doc-1"
        assert payload["results"][0]["score"] == 0.91
        assert payload["results"][0]["source_uri"] == "s3://bucket/report.pdf"


# --------------------------------------------------------------------------
# Memory primitives (#40) shapes
# --------------------------------------------------------------------------


class TestMemoryPrimitives:
    async def test_get_citations_returns_citation_objects(self):
        mock_db = AsyncMock()
        mock_db.get_user_workspace_ids = AsyncMock(return_value=["ws-1"])
        mock_search = AsyncMock()
        mock_search.search = AsyncMock(
            return_value=SearchResponse(
                results=[_search_result()],
                query="x",
                total_results=1,
                processing_time_ms=1.0,
                search_mode="semantic",
            )
        )
        result = await _call(
            "get_citations",
            {"api_key": "k", "query": "x", "_key_info": _key(permissions=["search"])},
            mock_db,
            mock_search,
        )
        payload = _structured_payload(result)
        assert len(payload["citations"]) == 1
        cit = payload["citations"][0]
        assert cit["chunk_id"] == "chunk-1"
        assert cit["document_id"] == "doc-1"
        assert cit["workspace_id"] == "ws-1"

    async def test_verify_claim_returns_verdict(self):
        mock_db = AsyncMock()
        result = await _call(
            "verify_claim",
            {
                "api_key": "k",
                "claim": "The capital of France is Paris",
                "evidence": ["Paris is the capital of France."],
                "_key_info": _key(permissions=["read"]),
            },
            mock_db,
        )
        payload = _structured_payload(result)
        assert payload["support_level"] == "strong"
        assert 0.0 <= payload["score"] <= 1.0

    async def test_explain_lineage_returns_provenance_and_freshness(self):
        doc = Document(
            id="doc-1",
            name="report.pdf",
            workspace_id="ws-1",
            source_type="s3",
            mime_type="application/pdf",
            size_bytes=10,
            chunk_count=1,
            status="processed",
            created_at=_dt.datetime.now(),
            updated_at=_dt.datetime.now(),
            metadata={"storage_url": "https://example/report.pdf"},
        )
        chunk = DocumentChunk(
            id="chunk-1",
            document_id="doc-1",
            content="text",
            chunk_index=0,
            metadata={
                "source_uri": "s3://bucket/report.pdf",
                "content_hash": "abc123",
                "ingested_at": "2026-06-01T00:00:00Z",
            },
        )
        mock_db = AsyncMock()
        mock_db.get_document_by_id = AsyncMock(return_value=doc)
        mock_db.get_user_workspace_ids = AsyncMock(return_value=["ws-1"])
        mock_db.get_document_chunks_by_doc_id = AsyncMock(return_value=[chunk])
        result = await _call(
            "explain_lineage",
            {"api_key": "k", "document_id": "doc-1", "_key_info": _key(permissions=["read"])},
            mock_db,
        )
        payload = _structured_payload(result)
        assert payload["document_name"] == "report.pdf"
        assert payload["source_uri"] == "s3://bucket/report.pdf"
        assert payload["content_hash"] == "abc123"
        assert payload["ingested_at"].startswith("2026-06-01")
        assert payload["is_stale"] is False

    async def test_explain_lineage_blocks_foreign_document(self):
        doc = Document(
            id="doc-x",
            name="foreign.pdf",
            workspace_id="ws-foreign",
            source_type="s3",
            size_bytes=1,
            chunk_count=0,
            status="processed",
            created_at=_dt.datetime.now(),
            updated_at=_dt.datetime.now(),
        )
        mock_db = AsyncMock()
        mock_db.get_document_by_id = AsyncMock(return_value=doc)
        mock_db.get_user_workspace_ids = AsyncMock(return_value=["ws-owned"])
        mock_db.get_document_chunks_by_doc_id = AsyncMock(return_value=[])
        result = await _call(
            "explain_lineage",
            {"api_key": "k", "document_id": "doc-x", "_key_info": _key(permissions=["read"])},
            mock_db,
        )
        assert "don't have access" in result[0].text
        mock_db.get_document_chunks_by_doc_id.assert_not_called()

    async def test_refresh_stale_source_republishes_event(self):
        doc = Document(
            id="doc-1",
            name="report.pdf",
            workspace_id="ws-1",
            source_type="s3",
            size_bytes=10,
            chunk_count=1,
            status="processed",
            created_at=_dt.datetime.now(),
            updated_at=_dt.datetime.now(),
        )
        fields = {
            "document_id": "doc-1",
            "workspace_id": "ws-1",
            "user_id": "user-1",
            "filename": "ws-1/abc.pdf",
            "original_filename": "abc.pdf",
            "content_type": "application/pdf",
            "size_bytes": 2048,
            "storage_backend": "s3",
            "storage_path": "ws-1/abc.pdf",
            "storage_bucket": "bucket",
            "storage_url": "https://example/abc.pdf",
        }
        mock_db = AsyncMock()
        mock_db.get_document_by_id = AsyncMock(return_value=doc)
        mock_db.get_user_workspace_ids = AsyncMock(return_value=["ws-1"])
        mock_db.get_document_upload_fields = AsyncMock(return_value=fields)
        mock_db.create_or_reset_pending_document = AsyncMock(return_value=None)
        mock_mq = AsyncMock()
        mock_mq.publish = AsyncMock(return_value="1-0")

        with patch("src.services.mq.get_mq_service", AsyncMock(return_value=mock_mq)):
            result = await _call(
                "refresh_stale_source",
                {"api_key": "k", "document_id": "doc-1", "_key_info": _key(permissions=["write"])},
                mock_db,
            )
        payload = _structured_payload(result)
        assert payload["status"] == "pending"
        mock_db.create_or_reset_pending_document.assert_awaited_once()
        mock_mq.publish.assert_awaited_once()
        _topic, message = mock_mq.publish.await_args.args
        assert message["event_type"] == "document.uploaded"
        assert message["document_id"] == "doc-1"


class TestListDocumentsPageBounds:
    """MCP list_documents must clamp page/page_size like the REST route (#13).

    Unbounded page_size lets an agent dump a whole tenant; a non-positive page
    yields a negative SQL OFFSET that crashes into a generic error string.
    """

    async def test_clamps_oversized_page_size_and_nonpositive_page(self):
        mock_db = AsyncMock()
        mock_db.get_user_workspace_ids = AsyncMock(return_value=["ws-1"])
        mock_db.get_documents_multi_workspace = AsyncMock(return_value=([], 0))
        await _call(
            "list_documents",
            {
                "api_key": "k",
                "page": -5,
                "page_size": 1_000_000,
                "_key_info": _key(permissions=["read", "search"]),
            },
            mock_db,
        )
        mock_db.get_documents_multi_workspace.assert_awaited_once()
        _ws, page, page_size = mock_db.get_documents_multi_workspace.await_args.args
        assert page == 1  # clamped up from -5
        assert page_size == 100  # clamped down from 1_000_000 (MAX_PAGE_SIZE)
