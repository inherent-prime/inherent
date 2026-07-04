"""Tenant management activities for multi-tenancy infrastructure setup."""

import structlog
from temporalio import activity

from src.temporal.models import EnsureTenantInput, EnsureTenantOutput, UpdateStatsInput

logger = structlog.get_logger(__name__)


@activity.defn
async def ensure_tenant_ready(input: EnsureTenantInput) -> EnsureTenantOutput:
    """Ensure tenant infrastructure is ready for document processing.

    This activity:
    1. Ensures user tenant exists in PostgreSQL
    2. Ensures workspace metadata exists in PostgreSQL
    3. Ensures Weaviate collection and tenant exist

    Args:
        input: Contains workspace_id and user_id

    Returns:
        EnsureTenantOutput with tenant_id and workspace ready status
    """
    from src.temporal.shared_services import get_db_service, get_weaviate_service

    db_service = get_db_service()
    weaviate_service = get_weaviate_service()  # May return None

    try:
        from src.config.settings import get_settings
        from src.services.tenant_manager import TenantManager

        settings = get_settings()

        tenant_manager = TenantManager(
            settings=settings,
            db_service=db_service,
            weaviate_service=weaviate_service,
        )

        tenant_id = await tenant_manager.ensure_workspace_ready(
            workspace_id=input.workspace_id,
            user_id=input.user_id,
        )

        logger.info(
            "Tenant infrastructure ready",
            workspace_id=input.workspace_id,
            user_id=input.user_id,
            tenant_id=tenant_id,
        )

        # Record lineage event on success
        if input.workflow_run_id and input.document_id:
            try:
                await db_service.record_ingestion_event(
                    workflow_run_id=input.workflow_run_id,
                    document_id=input.document_id,
                    workspace_id=input.workspace_id,
                    event_type="tenant_ready",
                    status="succeeded",
                )
            except Exception as rec_err:
                logger.warning(
                    "Failed to record lineage event",
                    event_type="tenant_ready",
                    error=str(rec_err),
                )

        return EnsureTenantOutput(tenant_id=tenant_id, workspace_ready=True)

    except Exception as e:
        logger.error(
            "Failed to ensure tenant ready",
            workspace_id=input.workspace_id,
            user_id=input.user_id,
            error=str(e),
            exc_info=True,
        )

        # Record lineage event on failure
        if input.workflow_run_id and input.document_id:
            try:
                await db_service.record_ingestion_event(
                    workflow_run_id=input.workflow_run_id,
                    document_id=input.document_id,
                    workspace_id=input.workspace_id,
                    event_type="tenant_ready",
                    status="failed",
                    metadata={"error": str(e)},
                )
            except Exception as rec_err:
                logger.warning(
                    "Failed to record lineage event",
                    event_type="tenant_ready",
                    error=str(rec_err),
                )

        # Re-raise so Temporal's RetryPolicy (maximum_attempts=3) fires. Returning
        # tenant_id=None is a *successful* completion → no retry, and the workflow
        # then stores the document + chunks with a NULL tenant_id, breaking tenant
        # attribution for any query that filters on it (#2). Exhausted retries
        # surface to the workflow's outer handler → failed + dead-letter.
        raise


@activity.defn
async def update_workspace_stats(input: UpdateStatsInput) -> bool:
    """Update workspace statistics after document processing.

    Args:
        input: Contains workspace_id and delta values for stats

    Returns:
        True if stats were updated successfully
    """
    from src.temporal.shared_services import get_db_service

    db_service = get_db_service()

    try:
        from src.config.settings import get_settings
        from src.services.tenant_manager import TenantManager

        settings = get_settings()

        tenant_manager = TenantManager(
            settings=settings,
            db_service=db_service,
            weaviate_service=None,
        )

        await tenant_manager.update_workspace_stats(
            workspace_id=input.workspace_id,
            document_delta=input.document_delta,
            chunk_delta=input.chunk_delta,
            size_delta=input.size_delta,
            # Idempotency key (#7): dedup double-counting on Temporal retry /
            # dead-letter reprocess of the same run.
            workflow_run_id=input.workflow_run_id,
        )

        logger.info(
            "Updated workspace stats",
            workspace_id=input.workspace_id,
            document_delta=input.document_delta,
            chunk_delta=input.chunk_delta,
        )

        # Record lineage event on success
        if input.workflow_run_id and input.document_id:
            try:
                await db_service.record_ingestion_event(
                    workflow_run_id=input.workflow_run_id,
                    document_id=input.document_id,
                    workspace_id=input.workspace_id,
                    event_type="stats_updated",
                    status="succeeded",
                )
            except Exception as rec_err:
                logger.warning(
                    "Failed to record lineage event",
                    event_type="stats_updated",
                    error=str(rec_err),
                )

        return True

    except Exception as e:
        logger.warning(
            "Failed to update workspace stats",
            workspace_id=input.workspace_id,
            error=str(e),
        )

        # Record lineage event on failure
        if input.workflow_run_id and input.document_id:
            try:
                await db_service.record_ingestion_event(
                    workflow_run_id=input.workflow_run_id,
                    document_id=input.document_id,
                    workspace_id=input.workspace_id,
                    event_type="stats_updated",
                    status="failed",
                    metadata={"error": str(e)},
                )
            except Exception as rec_err:
                logger.warning(
                    "Failed to record lineage event",
                    event_type="stats_updated",
                    error=str(rec_err),
                )

        return False
