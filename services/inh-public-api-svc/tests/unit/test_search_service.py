"""Tests for search service — BM25, workspace collections, tenant scoping."""

import re
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.models.search import SearchRequest
from src.services.search import (
    SearchService,
    _get_user_tenant_name,
    _get_workspace_collection_name,
)


@pytest.fixture(autouse=True)
def stub_embed_query(monkeypatch):
    """Prevent loading the ~90 MB sentence-transformers model in tests."""

    def _fake(text: str) -> tuple[float, ...]:
        return tuple(0.0 for _ in range(384))

    monkeypatch.setattr("src.services.embedder.embed_query", _fake, raising=False)
    monkeypatch.setattr("src.services.search.embed_query", _fake, raising=False)


# ---------------------------------------------------------------------------
# Helper function tests
# ---------------------------------------------------------------------------


_COLLECTION_RE = re.compile(r"^Workspace_[A-Z2-7]*$")  # prefix + base32 charset
_TENANT_RE = re.compile(r"^User_[A-Z2-7]*$")


class TestGetWorkspaceCollectionName:
    """The derivation must be injective (distinct ids -> distinct collections)
    and stay within Weaviate's charset. It no longer strips punctuation — that
    was the cross-tenant collision bug (#1)."""

    def test_prefixed_and_valid_charset(self):
        for raw in ("abc123", "ws-abc-123", "ws@foo.bar/baz", "ws_abc_123"):
            assert _COLLECTION_RE.match(_get_workspace_collection_name(raw))

    def test_punctuation_variants_do_not_collide(self):
        # These all collapsed onto one collection under the old strip.
        variants = ["ws-abc-123", "ws_abc_123", "wsabc123", "ws.abc.123"]
        names = {_get_workspace_collection_name(v) for v in variants}
        assert len(names) == len(variants)

    def test_deterministic(self):
        assert _get_workspace_collection_name("ws-1") == _get_workspace_collection_name("ws-1")


class TestGetUserTenantName:
    """Same injectivity + charset guarantees for user tenant names."""

    def test_prefixed_and_valid_charset(self):
        for raw in ("user123", "user@domain.com", "user_2abc123", "abc123def456"):
            assert _TENANT_RE.match(_get_user_tenant_name(raw))

    def test_punctuation_variants_do_not_collide(self):
        variants = ["user-1", "user_1", "user1", "user.1"]
        names = {_get_user_tenant_name(v) for v in variants}
        assert len(names) == len(variants)


# ---------------------------------------------------------------------------
# SearchService.close tests
# ---------------------------------------------------------------------------


class TestSearchServiceClose:
    """Tests for SearchService.close() shutdown behavior."""

    @pytest.mark.asyncio
    async def test_close_ignores_event_loop_closed_runtimeerror(self, mock_database):
        search_service = SearchService(mock_database, "http://weaviate:8080")

        mock_client = AsyncMock()
        mock_client.aclose.side_effect = RuntimeError("Event loop is closed")
        search_service._client = mock_client

        # Should not raise during application shutdown.
        await search_service.close()
        assert search_service._client is None


# ---------------------------------------------------------------------------
# SearchService._search_weaviate tests
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_database():
    db = MagicMock()
    db.session = MagicMock()
    return db


@pytest.fixture
def search_service(mock_database):
    return SearchService(mock_database, "http://weaviate:8080")


class TestSearchWeaviateBM25:
    """Tests for _search_weaviate() BM25 queries."""

    @pytest.mark.asyncio
    async def test_builds_correct_semantic_query(self, search_service):
        """Default mode (semantic) emits nearVector with a float vector; no nearText."""
        workspace_id = "69c7e8b4587daa4c20d7fc12"
        user_id = "user_2abc"
        # Default search_mode is "semantic"
        request = SearchRequest(query="machine learning", limit=5)

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()

        collection_name = _get_workspace_collection_name(workspace_id)
        mock_response.json.return_value = {
            "data": {
                "Get": {
                    collection_name: [
                        {
                            "document_id": "d1",
                            "original_filename": "doc.pdf",
                            "content": "ML is great",
                            "chunk_index": 0,
                            "_additional": {"id": "c1-uuid", "score": "0.85"},
                        }
                    ]
                }
            }
        }

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response
        search_service._client = mock_client

        results = await search_service._search_weaviate(workspace_id, user_id, request)

        # Verify the POST call was made
        mock_client.post.assert_called_once()
        call_args = mock_client.post.call_args
        assert call_args[0][0] == "/v1/graphql"

        # Extract the query string
        query_body = call_args[1]["json"]["query"]

        # Should reference the workspace collection, not DocumentChunk
        assert collection_name in query_body
        assert "DocumentChunk" not in query_body

        # ENG-S082: semantic mode must use nearVector (client-side embedding), not nearText
        assert "nearVector" in query_body
        assert "nearText" not in query_body

        # Should include tenant
        tenant_name = _get_user_tenant_name(user_id)
        assert tenant_name in query_body

        # Should request score AND certainty/distance — semantic mode falls back
        # to certainty/distance because nearVector doesn't populate score
        assert "score" in query_body
        assert "certainty" in query_body
        assert "distance" in query_body

        # Should use original_filename, not document_name
        assert "original_filename" in query_body
        assert "document_name" not in query_body

        # Verify results
        assert len(results) == 1
        assert results[0].chunk_id == "c1-uuid"
        assert results[0].document_name == "doc.pdf"

    @pytest.mark.asyncio
    async def test_bm25_score_parsed_as_float(self, search_service):
        """BM25 scores come as strings and must be parsed to float."""
        workspace_id = "ws1"
        user_id = "u1"
        request = SearchRequest(query="test", limit=5, min_score=0.0)

        collection_name = _get_workspace_collection_name(workspace_id)
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "data": {
                "Get": {
                    collection_name: [
                        {
                            "document_id": "d1",
                            "original_filename": "a.txt",
                            "content": "hello",
                            "chunk_index": 0,
                            "_additional": {"id": "c1-uuid", "score": "0.7321"},
                        },
                        {
                            "document_id": "d2",
                            "original_filename": "b.txt",
                            "content": "world",
                            "chunk_index": 1,
                            "_additional": {"id": "c2-uuid", "score": "0.45"},
                        },
                    ]
                }
            }
        }

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response
        search_service._client = mock_client

        results = await search_service._search_weaviate(workspace_id, user_id, request)

        assert len(results) == 2
        assert results[0].score == 0.7321
        assert results[1].score == 0.45
        assert isinstance(results[0].score, float)

    @pytest.mark.asyncio
    async def test_min_score_filters_results(self, search_service):
        """Results below min_score should be excluded."""
        workspace_id = "ws1"
        user_id = "u1"
        request = SearchRequest(query="test", limit=5, min_score=0.5)

        collection_name = _get_workspace_collection_name(workspace_id)
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "data": {
                "Get": {
                    collection_name: [
                        {
                            "document_id": "d1",
                            "original_filename": "a.txt",
                            "content": "high score",
                            "chunk_index": 0,
                            "_additional": {"id": "c1-uuid", "score": "0.9"},
                        },
                        {
                            "document_id": "d2",
                            "original_filename": "b.txt",
                            "content": "low score",
                            "chunk_index": 1,
                            "_additional": {"id": "c2-uuid", "score": "0.3"},
                        },
                    ]
                }
            }
        }

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response
        search_service._client = mock_client

        results = await search_service._search_weaviate(workspace_id, user_id, request)

        assert len(results) == 1
        assert results[0].chunk_id == "c1-uuid"

    @pytest.mark.asyncio
    async def test_document_ids_filter_included(self, search_service):
        """When document_ids is set, a where filter should be in the query."""
        workspace_id = "ws1"
        user_id = "u1"
        request = SearchRequest(query="test", limit=5, document_ids=["doc1", "doc2"])

        collection_name = _get_workspace_collection_name(workspace_id)
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {"data": {"Get": {collection_name: []}}}

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response
        search_service._client = mock_client

        await search_service._search_weaviate(workspace_id, user_id, request)

        query_body = mock_client.post.call_args[1]["json"]["query"]
        assert "where" in query_body
        assert "doc1" in query_body
        assert "doc2" in query_body

    @pytest.mark.asyncio
    async def test_weaviate_http_error_propagates(self, search_service):
        """ENG-S082: Weaviate HTTP errors must propagate — no silent fallback."""
        workspace_id = "ws1"
        user_id = "u1"
        request = SearchRequest(query="test", limit=5, search_mode="keyword")

        mock_client = AsyncMock()
        mock_client.post.side_effect = httpx.HTTPError("connection refused")
        search_service._client = mock_client

        with pytest.raises(httpx.HTTPError):
            await search_service._search_weaviate(workspace_id, user_id, request)

    @pytest.mark.asyncio
    async def test_graphql_error_propagates(self, search_service):
        """ENG-S082: GraphQL-level errors (200 with errors field) must propagate."""
        workspace_id = "ws1"
        user_id = "u1"
        request = SearchRequest(query="test", limit=5, search_mode="keyword")

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {"errors": [{"message": "class not found"}]}

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response
        search_service._client = mock_client

        with pytest.raises(httpx.HTTPError, match="Weaviate GraphQL error"):
            await search_service._search_weaviate(workspace_id, user_id, request)


# ---------------------------------------------------------------------------
# SearchService.search() integration
# ---------------------------------------------------------------------------


class TestSearchMethod:
    """Tests for the public search() method."""

    @pytest.mark.asyncio
    async def test_search_passes_user_id(self, search_service):
        """search() should pass user_id to _search_weaviate."""
        with patch.object(
            search_service, "_search_weaviate", new_callable=AsyncMock
        ) as mock_weaviate:
            mock_weaviate.return_value = []
            request = SearchRequest(query="test")

            await search_service.search(workspace_id="ws1", user_id="u1", request=request)

            # _search_weaviate now also receives an optional precomputed query
            # vector (None for a single-workspace call); assert the leading args
            # without coupling to the new trailing parameter.
            assert mock_weaviate.call_count == 1
            assert mock_weaviate.call_args.args[:3] == ("ws1", "u1", request)


class TestSemanticScoreFallback:
    """nearVector returns certainty/distance, not score; ensure we fall back gracefully."""

    @pytest.mark.asyncio
    async def test_semantic_certainty_used_when_score_missing(self, search_service):
        from src.models.search import SearchRequest as _SearchRequest
        from src.services.search import _get_workspace_collection_name as _workspace_collection

        workspace_id = "ws1"
        user_id = "u1"
        request = _SearchRequest(query="meditations", limit=3)
        collection_name = _workspace_collection(workspace_id)

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "data": {
                "Get": {
                    collection_name: [
                        {
                            "document_id": "d1",
                            "original_filename": "meidtations.pdf",
                            "content": "On the nature of things",
                            "chunk_index": 5,
                            "_additional": {
                                "id": "c1",
                                "score": None,  # nearVector leaves this empty
                                "certainty": 0.83,
                                "distance": 0.34,
                            },
                        }
                    ]
                }
            }
        }
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response
        search_service._client = mock_client

        results = await search_service._search_weaviate(workspace_id, user_id, request)
        assert len(results) == 1
        assert results[0].score == 0.83  # certainty surfaced as score

    @pytest.mark.asyncio
    async def test_semantic_distance_used_when_score_and_certainty_missing(self, search_service):
        from src.models.search import SearchRequest as _SearchRequest
        from src.services.search import _get_workspace_collection_name as _workspace_collection

        workspace_id = "ws1"
        user_id = "u1"
        request = _SearchRequest(query="x", limit=1)
        collection_name = _workspace_collection(workspace_id)

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "data": {
                "Get": {
                    collection_name: [
                        {
                            "document_id": "d1",
                            "original_filename": "x.pdf",
                            "content": "...",
                            "chunk_index": 0,
                            "_additional": {
                                "id": "c1",
                                "score": None,
                                "certainty": None,
                                "distance": 0.5,  # cosine distance ⇒ similarity (1 - 0.5/2) = 0.75
                            },
                        }
                    ]
                }
            }
        }
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response
        search_service._client = mock_client

        results = await search_service._search_weaviate(workspace_id, user_id, request)
        assert results[0].score == 0.75
