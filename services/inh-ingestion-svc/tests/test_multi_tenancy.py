"""Tests for multi-tenancy functionality."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.services.database import DatabaseService, TenantStatus
from src.services.tenant_manager import TenantManager
from src.services.weaviate import (
    WeaviateService,
    get_user_tenant_name,
    get_workspace_collection_name,
)


class TestWeaviateNaming:
    """Naming must be injective (distinct ids -> distinct names). The previous
    strip-based derivation collapsed punctuation variants onto one name — a
    cross-tenant leak (#1). Golden values live in the contract test."""

    def test_names_are_prefixed(self):
        assert get_workspace_collection_name("6953c161551d723ca3e9a107").startswith("Workspace_")
        assert get_user_tenant_name("6952cca0ac4118d38ab723c3").startswith("User_")

    def test_workspace_punctuation_variants_do_not_collide(self):
        variants = ["test-workspace_123", "test_workspace-123", "testworkspace123"]
        names = {get_workspace_collection_name(v) for v in variants}
        assert len(names) == len(variants)

    def test_user_punctuation_variants_do_not_collide(self):
        variants = ["user@example.com", "userexamplecom", "user.example.com"]
        names = {get_user_tenant_name(v) for v in variants}
        assert len(names) == len(variants)


class TestWeaviateServiceMultiTenancy:
    """Tests for WeaviateService multi-tenancy methods."""

    @pytest.fixture
    def mock_settings(self):
        """Create mock settings."""
        settings = MagicMock()
        settings.weaviate_url = "http://localhost:8080"
        settings.weaviate_api_key = None
        return settings

    @pytest.fixture
    def weaviate_service(self, mock_settings):
        """Create WeaviateService instance without connecting."""
        service = WeaviateService(mock_settings)
        service.client = MagicMock()
        service.client.is_ready.return_value = True
        return service

    @pytest.mark.asyncio
    async def test_ensure_workspace_collection_creates_new(self, weaviate_service):
        """Test that a new collection is created when it doesn't exist."""
        workspace_id = "test_workspace_123"

        # Mock collections.exists to return False
        weaviate_service.client.collections.exists.return_value = False
        weaviate_service.client.collections.create = MagicMock()

        result = await weaviate_service.ensure_workspace_collection(workspace_id)

        assert result == get_workspace_collection_name(workspace_id)
        weaviate_service.client.collections.create.assert_called_once()

    @pytest.mark.asyncio
    async def test_ensure_workspace_collection_uses_cache(self, weaviate_service):
        """Test that cached collections don't trigger API calls."""
        workspace_id = "cached_workspace"
        collection_name = get_workspace_collection_name(workspace_id)

        # Add to cache
        weaviate_service._collection_cache.add(collection_name)

        result = await weaviate_service.ensure_workspace_collection(workspace_id)

        assert result == collection_name
        # Should not check existence since it's cached
        weaviate_service.client.collections.exists.assert_not_called()

    @pytest.mark.asyncio
    async def test_ensure_user_tenant_creates_new(self, weaviate_service):
        """Test that a new tenant is created when it doesn't exist."""
        workspace_id = "test_workspace"
        user_id = "test_user"

        collection_name = get_workspace_collection_name(workspace_id)
        weaviate_service._collection_cache.add(collection_name)

        # Mock collection and tenants
        mock_collection = MagicMock()
        mock_collection.tenants.get.return_value = {}
        weaviate_service.client.collections.get.return_value = mock_collection

        result = await weaviate_service.ensure_user_tenant(workspace_id, user_id)

        assert result == get_user_tenant_name(user_id)
        mock_collection.tenants.create.assert_called_once()


class TestTenantManager:
    """Tests for TenantManager."""

    @pytest.fixture
    def mock_settings(self):
        """Create mock settings."""
        settings = MagicMock()
        settings.auto_create_tenants = True
        return settings

    @pytest.fixture
    def mock_db_service(self):
        """Create mock database service."""
        db_service = MagicMock(spec=DatabaseService)
        db_service.upsert_tenant = AsyncMock(return_value=1)
        db_service.upsert_workspace_metadata = AsyncMock(return_value=1)
        db_service.update_workspace_stats = AsyncMock(return_value=True)
        db_service.get_tenant = AsyncMock(return_value={"user_id": "test_user", "status": "active"})
        return db_service

    @pytest.fixture
    def mock_weaviate_service(self):
        """Create mock Weaviate service."""
        weaviate_service = MagicMock(spec=WeaviateService)
        weaviate_service.ensure_workspace_collection = AsyncMock(return_value="Workspace_test")
        weaviate_service.ensure_user_tenant = AsyncMock(return_value="User_test")
        return weaviate_service

    @pytest.fixture
    def tenant_manager(self, mock_settings, mock_db_service, mock_weaviate_service):
        """Create TenantManager instance."""
        return TenantManager(
            settings=mock_settings,
            db_service=mock_db_service,
            weaviate_service=mock_weaviate_service,
        )

    @pytest.mark.asyncio
    async def test_ensure_tenant_exists(self, tenant_manager, mock_db_service):
        """Test tenant creation/update."""
        user_id = "test_user_123"

        result = await tenant_manager.ensure_tenant_exists(user_id)

        assert result == 1
        mock_db_service.upsert_tenant.assert_called_once_with(user_id)

    @pytest.mark.asyncio
    async def test_ensure_tenant_exists_uses_cache(self, tenant_manager, mock_db_service):
        """Test that cached tenants don't trigger database calls."""
        user_id = "cached_user"
        tenant_manager._tenant_cache[user_id] = 42

        result = await tenant_manager.ensure_tenant_exists(user_id)

        assert result == 42
        mock_db_service.upsert_tenant.assert_not_called()

    @pytest.mark.asyncio
    async def test_ensure_workspace_ready(
        self, tenant_manager, mock_db_service, mock_weaviate_service
    ):
        """Test full workspace setup."""
        workspace_id = "test_workspace"
        user_id = "test_user"

        result = await tenant_manager.ensure_workspace_ready(workspace_id, user_id)

        assert result == 1  # tenant_id
        mock_db_service.upsert_tenant.assert_called_once_with(user_id)
        mock_db_service.upsert_workspace_metadata.assert_called_once()
        mock_weaviate_service.ensure_workspace_collection.assert_called_once_with(workspace_id)
        mock_weaviate_service.ensure_user_tenant.assert_called_once_with(workspace_id, user_id)

    @pytest.mark.asyncio
    async def test_ensure_workspace_ready_caches_result(
        self, tenant_manager, mock_db_service, mock_weaviate_service
    ):
        """Test that workspace setup is cached."""
        workspace_id = "test_workspace"
        user_id = "test_user"

        # First call
        await tenant_manager.ensure_workspace_ready(workspace_id, user_id)

        # Reset mocks
        mock_db_service.upsert_tenant.reset_mock()
        mock_weaviate_service.ensure_workspace_collection.reset_mock()

        # Second call should use cache
        await tenant_manager.ensure_workspace_ready(workspace_id, user_id)

        # Should not call these again due to caching
        mock_weaviate_service.ensure_workspace_collection.assert_not_called()

    @pytest.mark.asyncio
    async def test_update_workspace_stats(self, tenant_manager, mock_db_service):
        """Test workspace stats update."""
        workspace_id = "test_workspace"

        await tenant_manager.update_workspace_stats(
            workspace_id=workspace_id,
            document_delta=1,
            chunk_delta=10,
            size_delta=1024,
        )

        mock_db_service.update_workspace_stats.assert_called_once_with(
            workspace_id=workspace_id,
            document_delta=1,
            chunk_delta=10,
            size_delta=1024,
            workflow_run_id=None,
        )

    def test_clear_cache(self, tenant_manager):
        """Test cache clearing."""
        tenant_manager._tenant_cache["user1"] = 1
        tenant_manager._workspace_cache.add("ws1:user1")

        tenant_manager.clear_cache()

        assert len(tenant_manager._tenant_cache) == 0
        assert len(tenant_manager._workspace_cache) == 0


class TestDatabaseServiceTenancy:
    """Tests for DatabaseService tenant methods."""

    @pytest.fixture
    def mock_settings(self):
        """Create mock settings."""
        settings = MagicMock()
        settings.database_url = "postgresql://test:test@localhost:5432/test"
        return settings

    def test_tenant_status_enum(self):
        """Test TenantStatus enum values."""
        assert TenantStatus.ACTIVE.value == "active"
        assert TenantStatus.INACTIVE.value == "inactive"
        assert TenantStatus.SUSPENDED.value == "suspended"
