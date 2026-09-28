"""Tests for TenantManager error handling."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.config.settings import Settings
from src.services.tenant_manager import TenantManager


class TestTenantManagerErrors:
    @pytest.fixture
    def mock_settings(self):
        settings = MagicMock(spec=Settings)
        return settings

    @pytest.fixture
    def tenant_manager(self, mock_settings):
        return TenantManager(mock_settings)

    @pytest.mark.asyncio
    async def test_ensure_tenant_exists_no_db(self, tenant_manager):
        """Test ensure_tenant_exists without DB service."""
        tenant_manager.db_service = None
        result = await tenant_manager.ensure_tenant_exists("user1")
        assert result == 0

    @pytest.mark.asyncio
    async def test_ensure_tenant_exists_error(self, tenant_manager):
        """Test ensure_tenant_exists error."""
        tenant_manager.db_service = MagicMock()
        tenant_manager.db_service.upsert_tenant = AsyncMock(side_effect=Exception("DB Error"))

        with pytest.raises(Exception, match="DB Error"):
            await tenant_manager.ensure_tenant_exists("user1")

    @pytest.mark.asyncio
    async def test_ensure_workspace_metadata_no_db(self, tenant_manager):
        """Test ensure_workspace_metadata without DB service."""
        tenant_manager.db_service = None
        # Should not raise
        await tenant_manager.ensure_workspace_metadata("ws1", "user1")

    @pytest.mark.asyncio
    async def test_ensure_workspace_metadata_error(self, tenant_manager):
        """Test ensure_workspace_metadata error."""
        tenant_manager.db_service = MagicMock()
        tenant_manager.db_service.upsert_workspace_metadata = AsyncMock(
            side_effect=Exception("DB Error")
        )

        with pytest.raises(Exception, match="DB Error"):
            await tenant_manager.ensure_workspace_metadata("ws1", "user1")

    @pytest.mark.asyncio
    async def test_ensure_workspace_ready_weaviate_error(self, tenant_manager):
        """Test ensure_workspace_ready handles Weaviate error gracefully."""
        tenant_manager.db_service = MagicMock()
        tenant_manager.db_service.upsert_tenant = AsyncMock(return_value=1)
        tenant_manager.db_service.upsert_workspace_metadata = AsyncMock(return_value=1)
        # inherent#395: ensure_workspace_ready now checks purge status first.
        tenant_manager.db_service.get_workspace_purge_status = AsyncMock(return_value=None)

        tenant_manager.weaviate_service = MagicMock()
        tenant_manager.weaviate_service.ensure_workspace_collection = AsyncMock(
            side_effect=Exception("Weaviate Error")
        )

        # Should succeed (return tenant_id) despite Weaviate error
        result = await tenant_manager.ensure_workspace_ready("ws1", "user1")
        assert result == 1

    @pytest.mark.asyncio
    async def test_update_workspace_stats_no_db(self, tenant_manager):
        """Test update_workspace_stats without DB."""
        tenant_manager.db_service = None
        # Should not raise
        await tenant_manager.update_workspace_stats("ws1")

    @pytest.mark.asyncio
    async def test_update_workspace_stats_error(self, tenant_manager):
        """Test update_workspace_stats error."""
        tenant_manager.db_service = MagicMock()
        tenant_manager.db_service.update_workspace_stats = AsyncMock(
            side_effect=Exception("DB Error")
        )

        # Should catch exception and log warning (not raise)
        await tenant_manager.update_workspace_stats("ws1")

    @pytest.mark.asyncio
    async def test_deactivate_idle_tenants_no_services(self, tenant_manager):
        """Test deactivate_idle_tenants without services."""
        tenant_manager.db_service = None
        result = await tenant_manager.deactivate_idle_tenants()
        assert result == 0

    @pytest.mark.asyncio
    async def test_deactivate_idle_tenants_error(self, tenant_manager):
        """Test deactivate_idle_tenants error."""
        tenant_manager.db_service = MagicMock()
        tenant_manager.weaviate_service = MagicMock()
        tenant_manager.db_service.get_idle_tenants = AsyncMock(side_effect=Exception("DB Error"))

        result = await tenant_manager.deactivate_idle_tenants()
        assert result == 0

    @pytest.mark.asyncio
    async def test_reactivate_tenant_no_db(self, tenant_manager):
        """Test reactivate_tenant without DB."""
        tenant_manager.db_service = None
        result = await tenant_manager.reactivate_tenant("user1")
        assert result is False

    @pytest.mark.asyncio
    async def test_reactivate_tenant_error(self, tenant_manager):
        """Test reactivate_tenant error."""
        tenant_manager.db_service = MagicMock()
        tenant_manager.db_service.update_tenant_status = AsyncMock(
            side_effect=Exception("DB Error")
        )

        result = await tenant_manager.reactivate_tenant("user1")
        assert result is False

    @pytest.mark.asyncio
    async def test_delete_workspace_weaviate_error(self, tenant_manager):
        """Test delete_workspace handles Weaviate error."""
        tenant_manager.db_service = MagicMock()
        tenant_manager.db_service.delete_workspace_data = AsyncMock(return_value=1)

        tenant_manager.weaviate_service = MagicMock()
        tenant_manager.weaviate_service.delete_workspace_collection = AsyncMock(
            side_effect=Exception("Weaviate Error")
        )

        result = await tenant_manager.delete_workspace("ws1")
        assert result is False

    @pytest.mark.asyncio
    async def test_delete_workspace_db_error(self, tenant_manager):
        """Test delete_workspace handles DB error."""
        tenant_manager.weaviate_service = MagicMock()
        tenant_manager.weaviate_service.delete_workspace_collection = AsyncMock(return_value=True)

        tenant_manager.db_service = MagicMock()
        tenant_manager.db_service.delete_workspace_data = AsyncMock(
            side_effect=Exception("DB Error")
        )

        result = await tenant_manager.delete_workspace("ws1")
        assert result is False

    @pytest.mark.asyncio
    async def test_get_tenant_info_no_db(self, tenant_manager):
        """Test get_tenant_info without DB."""
        tenant_manager.db_service = None
        result = await tenant_manager.get_tenant_info("user1")
        assert result is None

    @pytest.mark.asyncio
    async def test_get_tenant_info_error(self, tenant_manager):
        """Test get_tenant_info error."""
        tenant_manager.db_service = MagicMock()
        tenant_manager.db_service.get_tenant = AsyncMock(side_effect=Exception("DB Error"))

        result = await tenant_manager.get_tenant_info("user1")
        assert result is None

    @pytest.mark.asyncio
    async def test_get_workspace_info_no_db(self, tenant_manager):
        """Test get_workspace_info without DB."""
        tenant_manager.db_service = None
        result = await tenant_manager.get_workspace_info("ws1")
        assert result is None

    @pytest.mark.asyncio
    async def test_get_workspace_info_error(self, tenant_manager):
        """Test get_workspace_info error."""
        tenant_manager.db_service = MagicMock()
        tenant_manager.db_service.get_workspace_metadata = AsyncMock(
            side_effect=Exception("DB Error")
        )

        result = await tenant_manager.get_workspace_info("ws1")
        assert result is None
