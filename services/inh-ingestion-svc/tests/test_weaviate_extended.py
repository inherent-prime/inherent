"""Extended tests for Weaviate service."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.config.settings import Settings
from src.models.document import DocumentChunk
from src.services.weaviate import WeaviateService


class TestWeaviateServiceExtended:
    """Extended tests for WeaviateService."""

    @pytest.fixture
    def mock_settings(self):
        """Create mock settings."""
        settings = MagicMock(spec=Settings)
        settings.weaviate_url = "http://localhost:8080"
        settings.weaviate_api_key = None
        return settings

    @pytest.fixture
    def weaviate_service(self, mock_settings):
        """Create WeaviateService instance."""
        service = WeaviateService(mock_settings)
        service.client = MagicMock()
        service.client.is_ready.return_value = True
        return service

    @pytest.mark.asyncio
    async def test_store_chunks_with_tenant_success(self, weaviate_service):
        """Test storing chunks successfully."""
        # Setup mocks with AsyncMock
        weaviate_service.ensure_workspace_collection = AsyncMock(return_value="Workspace_ws1")
        weaviate_service.ensure_user_tenant = AsyncMock(return_value="User_u1")

        mock_collection = MagicMock()
        mock_tenant_collection = MagicMock()
        mock_batch = MagicMock()

        weaviate_service.client.collections.get.return_value = mock_collection
        mock_collection.with_tenant.return_value = mock_tenant_collection
        mock_tenant_collection.batch.dynamic.return_value.__enter__.return_value = mock_batch
        # No per-object failures (#8): the store now inspects failed_objects.
        mock_tenant_collection.batch.failed_objects = []

        chunks = [
            DocumentChunk(
                document_id="doc1", content="text", chunk_index=0, start_char=0, end_char=4
            )
        ]

        # store_chunks_with_tenant calls embed_texts (HTTP to TEI sidecar);
        # patch it to a fixed-shape stub so the test runs offline.
        with patch(
            "src.services.embedder.embed_texts",
            return_value=[[0.1] * 384 for _ in chunks],
        ):
            count = await weaviate_service.store_chunks_with_tenant(
                chunks, "doc1", "ws1", "u1", "file.txt", "text/plain"
            )

        assert count == 1
        mock_batch.add_object.assert_called_once()

    @pytest.mark.asyncio
    async def test_legacy_search_chunks(self, weaviate_service):
        """Test legacy search chunks."""
        mock_collection = MagicMock()
        weaviate_service.client.collections.get.return_value = mock_collection

        mock_obj = MagicMock()
        mock_obj.uuid = "uuid1"
        mock_obj.metadata.score = 0.8
        mock_obj.properties = {"content": "text"}

        mock_results = MagicMock()
        mock_results.objects = [mock_obj]
        mock_collection.query.bm25.return_value = mock_results

        results = await weaviate_service.search_chunks("query", "ws1")

        assert len(results) == 1
        assert results[0]["uuid"] == "uuid1"
        mock_collection.query.bm25.assert_called_once()

    @pytest.mark.asyncio
    async def test_legacy_delete_document_chunks(self, weaviate_service):
        """Test legacy delete chunks."""
        mock_collection = MagicMock()
        weaviate_service.client.collections.get.return_value = mock_collection

        mock_result = MagicMock()
        mock_result.successful = 10
        mock_collection.data.delete_many.return_value = mock_result

        count = await weaviate_service.delete_document_chunks("doc1")

        assert count == 10
        mock_collection.data.delete_many.assert_called_once()

    @pytest.mark.asyncio
    async def test_legacy_store_chunks(self, weaviate_service):
        """Test legacy store chunks routes to new method."""
        # Mock the new method with AsyncMock
        weaviate_service.store_chunks_with_tenant = AsyncMock(return_value=5)

        chunks = []
        count = await weaviate_service.store_chunks(
            chunks, "doc1", "ws1", "u1", "file.txt", "text/plain"
        )

        assert count == 5
        weaviate_service.store_chunks_with_tenant.assert_called_once()

    def test_connect_with_api_key(self, mock_settings):
        """Test connection with API key."""
        mock_settings.weaviate_api_key = "secret-key"
        service = WeaviateService(mock_settings)

        with patch("src.services.weaviate.weaviate") as mock_weaviate_lib:
            mock_client = MagicMock()
            mock_client.is_ready.return_value = True
            mock_weaviate_lib.connect_to_custom.return_value = mock_client

            service.connect()

            # Check if Auth.api_key was used
            mock_weaviate_lib.connect_to_custom.assert_called_once()
            # We can't easily check Auth.api_key return value passed, but we know it was called

    def test_connect_http_vs_https(self, mock_settings):
        """Test URL parsing for http vs https."""
        # Test HTTP
        mock_settings.weaviate_url = "http://weaviate:8080"
        service = WeaviateService(mock_settings)

        with patch("src.services.weaviate.weaviate") as mock_weaviate_lib:
            mock_client = MagicMock()
            mock_client.is_ready.return_value = True
            mock_weaviate_lib.connect_to_custom.return_value = mock_client

            service.connect()

            call_kwargs = mock_weaviate_lib.connect_to_custom.call_args[1]
            assert call_kwargs["http_host"] == "weaviate"
            assert call_kwargs["http_port"] == 8080
            assert call_kwargs["http_secure"] is False

        # Test HTTPS
        mock_settings.weaviate_url = "https://weaviate-cloud:443"
        service = WeaviateService(mock_settings)

        with patch("src.services.weaviate.weaviate") as mock_weaviate_lib:
            mock_client = MagicMock()
            mock_client.is_ready.return_value = True
            mock_weaviate_lib.connect_to_custom.return_value = mock_client

            service.connect()

            call_kwargs = mock_weaviate_lib.connect_to_custom.call_args[1]
            assert call_kwargs["http_host"] == "weaviate-cloud"
            assert call_kwargs["http_port"] == 443
            assert call_kwargs["http_secure"] is True
