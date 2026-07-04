"""Advanced logic tests for Weaviate service."""

from unittest.mock import MagicMock

import pytest
from weaviate.classes.tenants import TenantActivityStatus

from src.config.settings import Settings
from src.services.weaviate import (
    WeaviateService,
    get_user_tenant_name,
    get_workspace_collection_name,
)

# Derive expected names via the contract function so these tests stay correct
# across any change to the (injective) name encoding (#1).
WS1 = get_workspace_collection_name("ws1")
U1 = get_user_tenant_name("u1")


class TestWeaviateLogic:
    @pytest.fixture
    def mock_settings(self):
        settings = MagicMock(spec=Settings)
        settings.weaviate_url = "http://localhost:8080"
        return settings

    @pytest.fixture
    def weaviate_service(self, mock_settings):
        service = WeaviateService(mock_settings)
        service.client = MagicMock()
        service.client.is_ready.return_value = True
        return service

    @pytest.mark.asyncio
    async def test_ensure_workspace_collection_race_condition(self, weaviate_service):
        """Test race condition handling in ensure_workspace_collection."""
        weaviate_service.client.collections.exists.return_value = False
        # Raise exception that it already exists during create
        weaviate_service.client.collections.create.side_effect = Exception("already exists")

        result = await weaviate_service.ensure_workspace_collection("ws1")

        assert result == WS1
        assert WS1 in weaviate_service._collection_cache

    @pytest.mark.asyncio
    async def test_ensure_user_tenant_cached(self, weaviate_service):
        """Test tenant retrieval from cache."""
        weaviate_service._tenant_cache[WS1] = {U1}

        result = await weaviate_service.ensure_user_tenant("ws1", "u1")

        assert result == U1
        weaviate_service.client.collections.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_ensure_user_tenant_exists_active(self, weaviate_service):
        """Test ensuring existing active tenant."""
        mock_collection = MagicMock()
        weaviate_service.client.collections.get.return_value = mock_collection

        mock_tenant = MagicMock()
        mock_tenant.name = U1
        mock_tenant.activity_status = TenantActivityStatus.ACTIVE
        mock_collection.tenants.get.return_value = {U1: mock_tenant}

        result = await weaviate_service.ensure_user_tenant("ws1", "u1")

        assert result == U1
        mock_collection.tenants.update.assert_not_called()
        mock_collection.tenants.create.assert_not_called()

    @pytest.mark.asyncio
    async def test_ensure_user_tenant_exists_inactive(self, weaviate_service):
        """Test activating existing inactive tenant."""
        mock_collection = MagicMock()
        weaviate_service.client.collections.get.return_value = mock_collection

        mock_tenant = MagicMock()
        mock_tenant.name = U1
        mock_tenant.activity_status = TenantActivityStatus.INACTIVE
        mock_collection.tenants.get.return_value = {U1: mock_tenant}

        result = await weaviate_service.ensure_user_tenant("ws1", "u1")

        assert result == U1
        mock_collection.tenants.update.assert_called_once()

    @pytest.mark.asyncio
    async def test_ensure_user_tenant_race_condition(self, weaviate_service):
        """Test race condition handling in ensure_user_tenant."""
        mock_collection = MagicMock()
        weaviate_service.client.collections.get.return_value = mock_collection
        mock_collection.tenants.get.return_value = {}

        # Raise exception that it already exists during create
        mock_collection.tenants.create.side_effect = Exception("already exists")

        result = await weaviate_service.ensure_user_tenant("ws1", "u1")

        assert result == U1
        assert U1 in weaviate_service._tenant_cache[WS1]

    def test_list_workspace_collections_error(self, weaviate_service):
        """Test error handling in list_workspace_collections."""
        weaviate_service.client.collections.list_all.side_effect = Exception("Error")

        result = weaviate_service.list_workspace_collections()
        assert result == []

    @pytest.mark.asyncio
    async def test_get_tenant_stats_error(self, weaviate_service):
        """Test error handling in get_tenant_stats."""
        weaviate_service.client.collections.get.side_effect = Exception("Error")

        result = await weaviate_service.get_tenant_stats("ws1")
        assert "error" in result

    @pytest.mark.asyncio
    async def test_delete_workspace_collection_missing(self, weaviate_service):
        """Test delete missing workspace collection."""
        weaviate_service.client.collections.exists.return_value = False

        result = await weaviate_service.delete_workspace_collection("ws1")
        assert result is False
