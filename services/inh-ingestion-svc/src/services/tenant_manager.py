"""Tenant Manager Service for multi-tenancy lifecycle management.

Responsibilities:
- Lazy tenant/collection creation on first document upload
- Tenant lifecycle management (activate/deactivate for cost optimization)
- Cross-service consistency (PostgreSQL + Weaviate in sync)
"""

from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from src.config.settings import Settings
from src.services.database import DatabaseService
from src.services.weaviate import WeaviateService

logger = structlog.get_logger(__name__)


class TenantManager:
    """Manages tenant lifecycle across PostgreSQL and Weaviate.

    This service ensures that:
    1. User tenants are registered in PostgreSQL on first document upload
    2. Workspace collections are created in Weaviate on first access
    3. User tenants are created within workspace collections
    4. Idle tenants can be deactivated for cost optimization
    """

    def __init__(
        self,
        settings: Settings,
        db_service: DatabaseService | None = None,
        weaviate_service: WeaviateService | None = None,
    ):
        """Initialize Tenant Manager.

        Args:
            settings: Application settings
            db_service: Database service (optional, can be set later)
            weaviate_service: Weaviate service (optional, can be set later)
        """
        self.settings = settings
        self.db_service = db_service
        self.weaviate_service = weaviate_service
        self._tenant_cache: dict[str, int] = {}  # user_id -> tenant_id
        self._workspace_cache: set[str] = set()  # workspace_ids that are ready

    def set_services(
        self,
        db_service: DatabaseService | None = None,
        weaviate_service: WeaviateService | None = None,
    ) -> None:
        """Set or update service references.

        Args:
            db_service: Database service
            weaviate_service: Weaviate service
        """
        if db_service:
            self.db_service = db_service
        if weaviate_service:
            self.weaviate_service = weaviate_service

    async def ensure_tenant_exists(self, user_id: str) -> int:
        """Ensure user tenant exists in PostgreSQL.

        Creates a tenant record if it doesn't exist, or updates last_activity_at
        if it does exist.

        Args:
            user_id: The user identifier

        Returns:
            The tenant_id (primary key) from PostgreSQL
        """
        # Check cache first
        if user_id in self._tenant_cache:
            logger.debug("Tenant found in cache", user_id=user_id)
            return self._tenant_cache[user_id]

        if not self.db_service:
            logger.warning("Database service not available, returning 0 for tenant_id")
            return 0

        try:
            tenant_id = await self.db_service.upsert_tenant(user_id)
            self._tenant_cache[user_id] = tenant_id
            logger.info("Ensured tenant exists", user_id=user_id, tenant_id=tenant_id)
            return tenant_id
        except Exception as e:
            logger.error(
                "Failed to ensure tenant exists",
                user_id=user_id,
                error=str(e),
                exc_info=True,
            )
            raise

    async def ensure_workspace_metadata(
        self,
        workspace_id: str,
        user_id: str,
    ) -> None:
        """Ensure workspace metadata exists in PostgreSQL.

        Creates a workspace_metadata record if it doesn't exist.

        Args:
            workspace_id: The workspace identifier
            user_id: The owner's user identifier
        """
        if not self.db_service:
            logger.warning("Database service not available, skipping workspace metadata")
            return

        try:
            await self.db_service.upsert_workspace_metadata(
                workspace_id=workspace_id,
                user_id=user_id,
            )
            logger.debug(
                "Ensured workspace metadata exists",
                workspace_id=workspace_id,
                user_id=user_id,
            )
        except Exception as e:
            logger.error(
                "Failed to ensure workspace metadata",
                workspace_id=workspace_id,
                error=str(e),
                exc_info=True,
            )
            raise

    async def ensure_workspace_ready(
        self,
        workspace_id: str,
        user_id: str,
    ) -> int:
        """Ensure workspace is fully ready for document storage.

        This is the main entry point for preparing tenant infrastructure.
        It ensures:
        1. User tenant exists in PostgreSQL
        2. Workspace metadata exists in PostgreSQL
        3. Workspace collection exists in Weaviate (with multi-tenancy enabled)
        4. User tenant exists within the workspace collection

        Args:
            workspace_id: The workspace identifier
            user_id: The user identifier

        Returns:
            The tenant_id from PostgreSQL
        """
        cache_key = f"{workspace_id}:{user_id}"

        # Check if this workspace:user combination is already cached
        if cache_key in self._workspace_cache and user_id in self._tenant_cache:
            return self._tenant_cache[user_id]

        logger.info(
            "Ensuring workspace is ready for tenant",
            workspace_id=workspace_id,
            user_id=user_id,
        )

        # Step 1: Ensure tenant exists in PostgreSQL
        tenant_id = await self.ensure_tenant_exists(user_id)

        # Step 2: Ensure workspace metadata exists in PostgreSQL
        await self.ensure_workspace_metadata(workspace_id, user_id)

        # Step 3: Ensure Weaviate collection and tenant exist
        if self.weaviate_service:
            try:
                # Create workspace collection if it doesn't exist
                await self.weaviate_service.ensure_workspace_collection(workspace_id)

                # Create user tenant within the collection
                await self.weaviate_service.ensure_user_tenant(workspace_id, user_id)

                logger.info(
                    "Weaviate workspace and tenant ready",
                    workspace_id=workspace_id,
                    user_id=user_id,
                )
            except Exception as e:
                logger.error(
                    "Failed to setup Weaviate tenant infrastructure",
                    workspace_id=workspace_id,
                    user_id=user_id,
                    error=str(e),
                    exc_info=True,
                )
                # Don't fail the entire operation if Weaviate setup fails
                # The document can still be processed and stored in PostgreSQL

        # Update cache
        self._workspace_cache.add(cache_key)

        return tenant_id

    async def update_workspace_stats(
        self,
        workspace_id: str,
        document_delta: int = 0,
        chunk_delta: int = 0,
        size_delta: int = 0,
        workflow_run_id: str | None = None,
    ) -> None:
        """Update workspace statistics after document processing.

        Args:
            workspace_id: The workspace identifier
            document_delta: Change in document count (+1 for add, -1 for delete)
            chunk_delta: Change in chunk count
            size_delta: Change in total size bytes
            workflow_run_id: Idempotency key so a retry/reprocess of the same run
                doesn't double-count (#7).
        """
        if not self.db_service:
            return

        try:
            await self.db_service.update_workspace_stats(
                workspace_id=workspace_id,
                document_delta=document_delta,
                chunk_delta=chunk_delta,
                size_delta=size_delta,
                workflow_run_id=workflow_run_id,
            )
        except Exception as e:
            logger.warning(
                "Failed to update workspace stats",
                workspace_id=workspace_id,
                error=str(e),
            )

    async def deactivate_idle_tenants(self, idle_days: int = 30) -> int:
        """Deactivate tenants that have been idle for specified days.

        This is a cost optimization measure. Inactive tenants in Weaviate
        don't consume compute resources.

        Args:
            idle_days: Number of days of inactivity before deactivation

        Returns:
            Number of tenants deactivated
        """
        if not self.db_service or not self.weaviate_service:
            logger.warning("Services not available for tenant deactivation")
            return 0

        cutoff_date = datetime.now(UTC) - timedelta(days=idle_days)
        deactivated_count = 0

        try:
            # Get idle tenants from PostgreSQL
            idle_tenants = await self.db_service.get_idle_tenants(cutoff_date)

            for tenant in idle_tenants:
                user_id = tenant["user_id"]

                # Get all workspaces for this user
                workspaces = await self.db_service.get_user_workspaces(user_id)

                for workspace in workspaces:
                    workspace_id = workspace["workspace_id"]
                    try:
                        await self.weaviate_service.deactivate_user_tenant(
                            workspace_id=workspace_id,
                            user_id=user_id,
                        )
                        deactivated_count += 1
                    except Exception as e:
                        logger.warning(
                            "Failed to deactivate tenant in Weaviate",
                            user_id=user_id,
                            workspace_id=workspace_id,
                            error=str(e),
                        )

                # Update tenant status in PostgreSQL
                await self.db_service.update_tenant_status(user_id, "inactive")

                # Remove from cache
                self._tenant_cache.pop(user_id, None)

            logger.info(
                "Completed idle tenant deactivation",
                idle_days=idle_days,
                deactivated_count=deactivated_count,
            )
            return deactivated_count

        except Exception as e:
            logger.error(
                "Failed to deactivate idle tenants",
                error=str(e),
                exc_info=True,
            )
            return deactivated_count

    async def reactivate_tenant(self, user_id: str) -> bool:
        """Reactivate a previously deactivated tenant.

        Args:
            user_id: The user identifier

        Returns:
            True if reactivation was successful
        """
        if not self.db_service:
            return False

        try:
            # Update status in PostgreSQL
            await self.db_service.update_tenant_status(user_id, "active")

            # Note: Weaviate tenant will be auto-activated on next access
            # due to auto_tenant_activation=True in collection config

            logger.info("Reactivated tenant", user_id=user_id)
            return True

        except Exception as e:
            logger.error(
                "Failed to reactivate tenant",
                user_id=user_id,
                error=str(e),
            )
            return False

    async def delete_workspace(self, workspace_id: str) -> bool:
        """Delete a workspace and all its data.

        This deletes:
        1. Workspace metadata from PostgreSQL
        2. All processed documents and chunks from PostgreSQL
        3. The entire Weaviate collection for the workspace

        Args:
            workspace_id: The workspace identifier

        Returns:
            True if deletion was successful
        """
        success = True

        # Delete from Weaviate
        if self.weaviate_service:
            try:
                await self.weaviate_service.delete_workspace_collection(workspace_id)
            except Exception as e:
                logger.error(
                    "Failed to delete Weaviate collection",
                    workspace_id=workspace_id,
                    error=str(e),
                )
                success = False

        # Delete from PostgreSQL
        if self.db_service:
            try:
                await self.db_service.delete_workspace_data(workspace_id)
            except Exception as e:
                logger.error(
                    "Failed to delete PostgreSQL data",
                    workspace_id=workspace_id,
                    error=str(e),
                )
                success = False

        # Clear from cache
        keys_to_remove = [k for k in self._workspace_cache if k.startswith(f"{workspace_id}:")]
        for key in keys_to_remove:
            self._workspace_cache.discard(key)

        return success

    async def get_tenant_info(self, user_id: str) -> dict[str, Any] | None:
        """Get information about a tenant.

        Args:
            user_id: The user identifier

        Returns:
            Tenant information or None if not found
        """
        if not self.db_service:
            return None

        try:
            return await self.db_service.get_tenant(user_id)
        except Exception as e:
            logger.error(
                "Failed to get tenant info",
                user_id=user_id,
                error=str(e),
            )
            return None

    async def get_workspace_info(self, workspace_id: str) -> dict[str, Any] | None:
        """Get information about a workspace.

        Args:
            workspace_id: The workspace identifier

        Returns:
            Workspace information including stats
        """
        if not self.db_service:
            return None

        try:
            pg_info = await self.db_service.get_workspace_metadata(workspace_id)

            # Optionally enrich with Weaviate stats
            if self.weaviate_service and pg_info:
                try:
                    wv_stats = await self.weaviate_service.get_tenant_stats(workspace_id)
                    if wv_stats and "error" not in wv_stats:
                        pg_info["weaviate_stats"] = wv_stats
                except Exception:
                    pass  # Weaviate stats are optional

            return pg_info

        except Exception as e:
            logger.error(
                "Failed to get workspace info",
                workspace_id=workspace_id,
                error=str(e),
            )
            return None

    def clear_cache(self) -> None:
        """Clear all internal caches.

        Useful for testing or after bulk operations.
        """
        self._tenant_cache.clear()
        self._workspace_cache.clear()
        logger.debug("Cleared tenant manager caches")
