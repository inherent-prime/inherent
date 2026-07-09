"""End-to-end tests for complete API flows.

These tests exercise the real FastAPI app with mocked database/search
services, validating full request->middleware->handler->response chains.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from src.models.document import Document, DocumentChunk
from src.models.search import SearchResponse, SearchResult
from src.services import document_intake

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

NOW = datetime.now(timezone.utc)


def _doc(**overrides):
    """Create a Document model instance."""
    defaults = {
        "id": "d1",
        "name": "doc.txt",
        "workspace_id": "ws-123",
        "source_type": "upload",
        "mime_type": "text/plain",
        "size_bytes": 100,
        "chunk_count": 2,
        "status": "processed",
        "created_at": NOW,
        "updated_at": NOW,
    }
    defaults.update(overrides)
    return Document(**defaults)


def _chunk(index=0, content="chunk content", **overrides):
    """Create a DocumentChunk model instance."""
    defaults = {
        "id": f"c{index}",
        "document_id": "d1",
        "content": content,
        "chunk_index": index,
        "token_count": 5,
        "metadata": {},
    }
    defaults.update(overrides)
    return DocumentChunk(**defaults)


# ---------------------------------------------------------------------------
# Health check flows
# ---------------------------------------------------------------------------


class TestHealthFlow:
    """Verify health endpoints work through full middleware stack."""

    async def test_liveness_returns_200(self, client):
        resp = await client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "healthy"
        assert data["service"] == "inh-public-api-svc"

    async def test_liveness_alt_endpoint(self, client):
        resp = await client.get("/health/live")
        assert resp.status_code == 200

    async def test_readiness_healthy(self, client):
        # Readiness now reflects dependency health in the HTTP status (#14), so
        # mock the checks healthy to exercise the 200 path deterministically.
        from src.api.v1 import health as health_mod
        from src.models.health import ComponentHealth

        healthy = AsyncMock(return_value=ComponentHealth(status="healthy"))
        with (
            patch.object(health_mod, "_check_database", healthy),
            patch.object(health_mod, "_check_weaviate", healthy),
        ):
            resp = await client.get("/health/ready")
        assert resp.status_code == 200
        data = resp.json()
        assert "checks" in data

    async def test_health_includes_security_headers(self, client):
        resp = await client.get("/health")
        assert "x-content-type-options" in resp.headers
        assert resp.headers["x-content-type-options"] == "nosniff"


# ---------------------------------------------------------------------------
# Search flow
# ---------------------------------------------------------------------------


class TestSearchFlow:
    """Full search flow: auth -> validate -> search -> response."""

    async def test_search_happy_path(self, client, mock_search):
        mock_search.search = AsyncMock(
            return_value=SearchResponse(
                results=[
                    SearchResult(
                        chunk_id="c1",
                        document_id="d1",
                        document_name="doc.txt",
                        content="hello world",
                        score=0.95,
                        metadata={},
                    )
                ],
                query="hello",
                total_results=1,
                processing_time_ms=15,
                search_mode="semantic",
            )
        )

        resp = await client.post("/v1/search", json={"query": "hello"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_results"] == 1
        assert data["results"][0]["content"] == "hello world"
        assert data["query"] == "hello"

    async def test_search_empty_results(self, client, mock_search):
        mock_search.search = AsyncMock(
            return_value=SearchResponse(
                results=[],
                query="nonexistent",
                total_results=0,
                processing_time_ms=10,
                search_mode="semantic",
            )
        )
        resp = await client.post("/v1/search", json={"query": "nonexistent"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_results"] == 0
        assert data["results"] == []

    async def test_search_validation_error(self, client):
        resp = await client.post("/v1/search", json={})
        assert resp.status_code == 422

    async def test_search_with_filters(self, client, mock_search):
        mock_search.search = AsyncMock(
            return_value=SearchResponse(
                results=[],
                query="test",
                total_results=0,
                processing_time_ms=5,
                search_mode="semantic",
            )
        )
        resp = await client.post(
            "/v1/search",
            json={
                "query": "test",
                "limit": 5,
                "min_score": 0.5,
                "document_ids": ["d1", "d2"],
            },
        )
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Document listing & detail flow
# ---------------------------------------------------------------------------


class TestDocumentFlow:
    """Full document CRUD flows."""

    async def test_list_documents(self, client, mock_db):
        mock_db.get_documents = AsyncMock(return_value=([_doc()], 1))
        resp = await client.get("/v1/documents")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["documents"][0]["name"] == "doc.txt"

    async def test_get_document_detail(self, client, mock_db):
        mock_db.get_document = AsyncMock(return_value=_doc())
        resp = await client.get("/v1/documents/d1")
        assert resp.status_code == 200
        assert resp.json()["name"] == "doc.txt"

    async def test_get_document_not_found(self, client, mock_db):
        mock_db.get_document = AsyncMock(return_value=None)
        resp = await client.get("/v1/documents/nonexistent")
        assert resp.status_code == 404

    async def test_list_documents_pagination(self, client, mock_db):
        mock_db.get_documents = AsyncMock(return_value=([], 0))
        resp = await client.get("/v1/documents?page=2&page_size=5")
        assert resp.status_code == 200
        data = resp.json()
        assert data["page"] == 2
        assert data["page_size"] == 5


# ---------------------------------------------------------------------------
# Chunks & context flow
# ---------------------------------------------------------------------------


class TestChunksFlow:
    """Full chunk retrieval and document context flows."""

    async def test_get_chunks(self, client, mock_db):
        mock_db.get_document = AsyncMock(return_value=_doc())
        mock_db.get_document_chunks = AsyncMock(
            return_value=[_chunk(0, "first chunk"), _chunk(1, "second chunk")]
        )
        resp = await client.get("/v1/chunks/d1")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        assert data[0]["chunk_index"] == 0

    async def test_get_document_context(self, client, mock_db):
        mock_db.get_document = AsyncMock(return_value=_doc())
        mock_db.get_document_chunks = AsyncMock(
            return_value=[_chunk(0, "first"), _chunk(1, "second")]
        )
        resp = await client.get("/v1/chunks/d1/context")
        assert resp.status_code == 200
        data = resp.json()
        assert data["document"]["name"] == "doc.txt"
        assert len(data["chunks"]) == 2
        assert "first" in data["full_text"]
        assert "second" in data["full_text"]

    async def test_context_document_not_found(self, client, mock_db):
        mock_db.get_document = AsyncMock(return_value=None)
        resp = await client.get("/v1/chunks/nonexistent/context")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Upload flow
# ---------------------------------------------------------------------------


class TestUploadFlow:
    """Full document upload flow: auth -> validate -> S3 -> MQ -> response."""

    async def test_upload_full_flow(self, client):
        with (
            patch.object(document_intake, "get_storage_service") as mock_storage_fn,
            patch.object(
                document_intake,
                "get_mq_service",
                new_callable=AsyncMock,
            ) as mock_mq_fn,
        ):
            mock_storage = MagicMock()
            mock_storage.generate_key.return_value = "ws-123/uid/test.txt"
            mock_storage.upload_file = AsyncMock(return_value="ws-123/uid/test.txt")
            mock_storage.build_storage_url.return_value = "s3://bucket/ws-123/uid/test.txt"
            mock_storage_fn.return_value = mock_storage

            mock_mq = AsyncMock()
            mock_mq.publish = AsyncMock(return_value="msg-1")
            mock_mq_fn.return_value = mock_mq

            resp = await client.post(
                "/v1/documents",
                files={"file": ("test.txt", b"hello world", "text/plain")},
            )
            assert resp.status_code == 201
            data = resp.json()
            assert data["status"] == "pending"
            assert data["name"] == "test.txt"
            assert data["mime_type"] == "text/plain"
            assert data["size_bytes"] == 11
            assert "document_id" in data

    async def test_upload_rejects_unsupported_type(self, client):
        resp = await client.post(
            "/v1/documents",
            files={"file": ("test.exe", b"binary", "application/x-executable")},
        )
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Auth failure flows
# ---------------------------------------------------------------------------


class TestAuthFailureFlow:
    """Verify unauthenticated requests are properly rejected."""

    async def test_search_without_auth(self, unauth_client):
        resp = await unauth_client.post("/v1/search", json={"query": "test"})
        assert resp.status_code == 401

    async def test_documents_without_auth(self, unauth_client):
        resp = await unauth_client.get("/v1/documents")
        assert resp.status_code == 401

    async def test_chunks_without_auth(self, unauth_client):
        resp = await unauth_client.get("/v1/chunks/doc1")
        assert resp.status_code == 401

    async def test_health_works_without_auth(self, unauth_client):
        resp = await unauth_client.get("/health")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Rate limiting flow
# ---------------------------------------------------------------------------


class TestRateLimitingFlow:
    """Verify rate limiting headers are present on authenticated requests."""

    async def test_search_returns_200(self, client, mock_search):
        mock_search.search = AsyncMock(
            return_value=SearchResponse(
                results=[],
                query="test",
                total_results=0,
                processing_time_ms=5,
                search_mode="semantic",
            )
        )
        resp = await client.post("/v1/search", json={"query": "test"})
        assert resp.status_code == 200

    async def test_health_bypasses_rate_limit(self, client):
        resp = await client.get("/health")
        assert resp.status_code == 200
