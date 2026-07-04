"""Tests for Weaviate service."""

from unittest.mock import MagicMock, patch

import pytest
from weaviate.classes.tenants import TenantActivityStatus

from src.config.settings import Settings
from src.services.weaviate import (
    WeaviateService,
    get_user_tenant_name,
    get_workspace_collection_name,
)

# Derive expected names via the (injective) contract fn so tests are drift-proof (#1).
WS1 = get_workspace_collection_name("ws1")
U_USER1 = get_user_tenant_name("user1")


class TestWeaviateService:
    """Tests for WeaviateService."""

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

    @patch("src.services.weaviate.weaviate")
    def test_connect(self, mock_weaviate_lib, mock_settings):
        """Test connecting to Weaviate."""
        service = WeaviateService(mock_settings)
        mock_client = MagicMock()
        mock_client.is_ready.return_value = True
        mock_weaviate_lib.connect_to_custom.return_value = mock_client

        service.connect()

        assert service.client == mock_client
        mock_weaviate_lib.connect_to_custom.assert_called_once()

    def test_disconnect(self, weaviate_service):
        """Test disconnecting from Weaviate."""
        mock_client = MagicMock()
        weaviate_service.client = mock_client

        weaviate_service.disconnect()

        mock_client.close.assert_called_once()
        assert weaviate_service.client is None

    @pytest.mark.asyncio
    async def test_deactivate_user_tenant(self, weaviate_service):
        """Test deactivating a user tenant."""
        workspace_id = "ws1"
        user_id = "user1"

        # Mock collection get
        mock_collection = MagicMock()
        weaviate_service.client.collections.get.return_value = mock_collection

        result = await weaviate_service.deactivate_user_tenant(workspace_id, user_id)

        assert result is True
        mock_collection.tenants.update.assert_called_once()
        # Verify call args
        args = mock_collection.tenants.update.call_args[0][0]
        assert args[0].name == U_USER1
        assert args[0].activity_status == TenantActivityStatus.INACTIVE

    @pytest.mark.asyncio
    async def test_delete_workspace_collection(self, weaviate_service):
        """Test deleting a workspace collection."""
        workspace_id = "ws1"

        # Mock exists
        weaviate_service.client.collections.exists.return_value = True

        result = await weaviate_service.delete_workspace_collection(workspace_id)

        assert result is True
        weaviate_service.client.collections.delete.assert_called_with(WS1)

    @pytest.mark.asyncio
    async def test_delete_document_chunks_with_tenant(self, weaviate_service):
        """Test deleting document chunks from tenant."""
        document_id = "doc1"
        workspace_id = "ws1"
        user_id = "user1"

        mock_collection = MagicMock()
        mock_tenant_collection = MagicMock()
        weaviate_service.client.collections.get.return_value = mock_collection
        mock_collection.with_tenant.return_value = mock_tenant_collection

        mock_result = MagicMock()
        mock_result.successful = 5
        mock_tenant_collection.data.delete_many.return_value = mock_result

        count = await weaviate_service.delete_document_chunks_with_tenant(
            document_id, workspace_id, user_id
        )

        assert count == 5
        mock_collection.with_tenant.assert_called_with(U_USER1)
        mock_tenant_collection.data.delete_many.assert_called_once()

    @pytest.mark.asyncio
    async def test_search_chunks_with_tenant(self, weaviate_service):
        """Test searching chunks with tenant."""
        query = "test"
        workspace_id = "ws1"
        user_id = "user1"

        mock_collection = MagicMock()
        mock_tenant_collection = MagicMock()
        weaviate_service.client.collections.get.return_value = mock_collection
        mock_collection.with_tenant.return_value = mock_tenant_collection

        # Mock search results
        mock_obj = MagicMock()
        mock_obj.uuid = "uuid1"
        mock_obj.metadata.score = 0.9
        mock_obj.properties = {"content": "test content"}

        mock_results = MagicMock()
        mock_results.objects = [mock_obj]
        mock_tenant_collection.query.bm25.return_value = mock_results

        results = await weaviate_service.search_chunks_with_tenant(query, workspace_id, user_id)

        assert len(results) == 1
        assert results[0]["uuid"] == "uuid1"
        assert results[0]["content"] == "test content"
        mock_tenant_collection.query.bm25.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_all_chunks_for_document(self, weaviate_service):
        """Test getting all chunks for a document."""
        document_id = "doc1"
        workspace_id = "ws1"
        user_id = "user1"

        mock_collection = MagicMock()
        mock_tenant_collection = MagicMock()
        weaviate_service.client.collections.get.return_value = mock_collection
        mock_collection.with_tenant.return_value = mock_tenant_collection

        mock_obj = MagicMock()
        mock_obj.uuid = "uuid1"
        mock_obj.properties = {"chunk_index": 0}

        mock_results = MagicMock()
        mock_results.objects = [mock_obj]
        mock_tenant_collection.query.fetch_objects.return_value = mock_results

        chunks = await weaviate_service.get_all_chunks_for_document(
            document_id, workspace_id, user_id
        )

        assert len(chunks) == 1
        assert chunks[0]["chunk_index"] == 0
        mock_tenant_collection.query.fetch_objects.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_tenant_stats(self, weaviate_service):
        """Test getting tenant stats."""
        workspace_id = "ws1"

        mock_collection = MagicMock()
        weaviate_service.client.collections.get.return_value = mock_collection

        # Mock tenants
        mock_tenant = MagicMock()
        mock_tenant.activity_status.name = "ACTIVE"
        mock_collection.tenants.get.return_value = {U_USER1: mock_tenant}

        stats = await weaviate_service.get_tenant_stats(workspace_id)

        assert stats["workspace_id"] == workspace_id
        assert stats["tenant_count"] == 1
        assert stats["tenants"][0]["name"] == U_USER1
        assert stats["tenants"][0]["status"] == "ACTIVE"

    def test_list_workspace_collections(self, weaviate_service):
        """Test listing workspace collections."""
        weaviate_service.client.collections.list_all.return_value = {
            WS1: {},
            "Other_collection": {},
        }

        collections = weaviate_service.list_workspace_collections()

        assert len(collections) == 1
        assert collections[0] == WS1

    @pytest.mark.asyncio
    async def test_store_chunks_with_tenant_failure(self, weaviate_service):
        """Test failure when storing chunks."""
        # Setup mocks to raise exception
        weaviate_service.ensure_workspace_collection = MagicMock(side_effect=Exception("Failed"))

        with pytest.raises(Exception, match="Failed"):
            await weaviate_service.store_chunks_with_tenant(
                [], "doc1", "ws1", "user1", "file.txt", "text/plain"
            )

    @pytest.mark.asyncio
    async def test_delete_workspace_collection_failure(self, weaviate_service):
        """Test failure when deleting collection."""
        weaviate_service.client.collections.exists.side_effect = Exception("Failed")

        result = await weaviate_service.delete_workspace_collection("ws1")
        assert result is False

    @pytest.mark.asyncio
    async def test_deactivate_user_tenant_failure(self, weaviate_service):
        """Test failure when deactivating tenant."""
        weaviate_service.client.collections.get.side_effect = Exception("Failed")

        result = await weaviate_service.deactivate_user_tenant("ws1", "user1")
        assert result is False
