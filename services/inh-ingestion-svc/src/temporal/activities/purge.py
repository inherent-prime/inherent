"""Activities backing `PurgeWorkspaceWorkflow` (inherent#395).

Operator-triggered, idempotent purge of everything a workspace left
behind, for data-deletion commitments (pilot end, customer request). Each
activity below is safe to re-run: every delete is a plain
``WHERE workspace_id = ...`` (or, for Weaviate, "delete the collection if
it still exists") so a retry after a step already ran just does nothing
the second time.

Store coverage (see the workflow module docstring for ordering):
  - Postgres: processed_documents + document_chunks (FK cascade),
    dead_letter_jobs, ingestion_events, redaction_audit,
    workspace_stats_ledger, the (public-api-owned) eval_* tables, and
    workspace_metadata itself.
  - Weaviate: the workspace's whole collection.
  - Mongo: audit_logs referencing the workspace (default; skippable via
    `retain_audit_logs`).
  - API keys: revoked, not deleted (see `revoke_api_keys`'s docstring).

Explicitly OUT of scope, and why (see also CHANGELOG/docs): there is no
per-workspace object-storage bucket or Redis/Valkey state to purge in this
codebase today -- `StorageService` only fetches from caller-supplied source
URLs (nothing durable to delete), the ingestion_staging table is keyed by
`workflow_run_id`/self-cleaning on workflow completion (not workspace_id),
and the Redis/Valkey message queue is a transient, topic-scoped stream with
no per-workspace keys. `cancel_inflight_ingestion` still covers the "stop
new writes" half of that staging table's story: cancelling in-flight
DocumentIngestionWorkflow runs stops them from writing MORE staging rows or
processed_documents/chunks for this workspace.
"""

from __future__ import annotations

import structlog
from temporalio import activity
from temporalio.client import Client
from temporalio.service import RPCError

from src.temporal.models import PurgeWorkspaceInput

logger = structlog.get_logger(__name__)


@activity.defn
async def mark_workspace_purging(input: PurgeWorkspaceInput) -> None:
    """Set the workspace's purge marker so `ensure_workspace_ready` refuses new ingest.

    Must run BEFORE any delete step below -- this is the "stop new writes
    first" half of the ordering the workflow enforces.
    """
    from src.temporal.shared_services import get_db_service

    db = get_db_service()
    await db.set_workspace_purge_status(input.workspace_id, "purging")
    logger.info("Marked workspace purging", workspace_id=input.workspace_id)


@activity.defn
async def mark_workspace_purged(input: PurgeWorkspaceInput) -> None:
    """Flip the purge marker to its terminal state, once every delete step has run."""
    from src.temporal.shared_services import get_db_service

    db = get_db_service()
    await db.set_workspace_purge_status(input.workspace_id, "purged")
    logger.info("Marked workspace purged", workspace_id=input.workspace_id)


@activity.defn
async def cancel_inflight_ingestion(input: PurgeWorkspaceInput) -> int:
    """Cancel any DocumentIngestionWorkflow still mid-run for this workspace.

    Ingestion workflows are found via PostgreSQL (documents still
    `pending`/`processing` in this workspace), not Temporal's visibility
    API: workflow ids are deterministic (``f"ingest-{document_id}"``,
    trigger.py) but not searchable by workspace_id without a custom search
    attribute this deployment does not register, and every in-flight
    document for the workspace already has a PostgreSQL row naming it. A
    workflow that has already finished (or a stale/deleted document_id) is
    a no-op cancel, caught below -- idempotent by construction, same as
    every other step here.

    Returns the number of workflows a cancel request was actually sent to
    (best-effort; a handle that no longer exists does not count).
    """
    from src.temporal.shared_services import get_db_service, get_temporal_client

    db = get_db_service()
    document_ids = await db.get_in_flight_document_ids(input.workspace_id)

    client: Client = await get_temporal_client()
    cancelled = 0
    for document_id in document_ids:
        workflow_id = f"ingest-{document_id}"
        try:
            handle = client.get_workflow_handle(workflow_id)
            await handle.cancel()
            cancelled += 1
            logger.info(
                "Cancelled in-flight ingestion workflow",
                workspace_id=input.workspace_id,
                document_id=document_id,
                workflow_id=workflow_id,
            )
        except RPCError as e:
            # Already closed/never existed -- fine, nothing to cancel.
            logger.debug(
                "In-flight ingestion workflow already gone",
                workflow_id=workflow_id,
                error=str(e),
            )

    return cancelled


@activity.defn
async def purge_postgres_documents(input: PurgeWorkspaceInput) -> int:
    """Delete processed_documents (+ cascaded document_chunks) and the workspace_metadata row.

    Reuses the existing `delete_workspace_data` (database.py) rather than
    duplicating it -- this was the ONE store `tenant_manager.delete_workspace`
    already covered before this feature; everything else in this module is
    new coverage.
    """
    from src.temporal.shared_services import get_db_service

    db = get_db_service()
    deleted_count: int = await db.delete_workspace_data(input.workspace_id)
    return deleted_count


@activity.defn
async def purge_postgres_side_tables(input: PurgeWorkspaceInput) -> dict[str, int]:
    """Delete dead_letter_jobs, ingestion_events, redaction_audit, workspace_stats_ledger, and eval_* rows."""
    from src.temporal.shared_services import get_db_service

    db = get_db_service()
    deleted: dict[str, int] = await db.delete_workspace_side_tables(input.workspace_id)
    deleted.update(await db.delete_workspace_eval_data(input.workspace_id))
    return deleted


@activity.defn
async def revoke_workspace_api_keys(input: PurgeWorkspaceInput) -> int:
    """Revoke every active API key scoped to this workspace (see database.py's docstring for revoke-vs-delete)."""
    from src.temporal.shared_services import get_db_service

    db = get_db_service()
    revoked_count: int = await db.revoke_workspace_api_keys(input.workspace_id)
    return revoked_count


@activity.defn
async def purge_weaviate_collection(input: PurgeWorkspaceInput) -> bool:
    """Delete the workspace's whole Weaviate collection (idempotent -- checks existence first)."""
    from src.temporal.shared_services import get_weaviate_service

    weaviate = get_weaviate_service()
    if weaviate is None:
        return False
    deleted_collection: bool = await weaviate.delete_workspace_collection(input.workspace_id)
    return deleted_collection


@activity.defn
async def purge_audit_logs(input: PurgeWorkspaceInput) -> int:
    """Delete Mongo audit_logs referencing this workspace, unless the operator opted to retain them.

    Default behavior (see PurgeWorkspaceInput.retain_audit_logs docstring):
    purges, because the deletion commitment this workflow exists for names
    "index and logs" explicitly.
    """
    if input.retain_audit_logs:
        logger.info(
            "Retaining audit logs for workspace (operator opt-in)", workspace_id=input.workspace_id
        )
        return 0

    from src.config.settings import get_settings
    from src.services.audit_mongo_writer import delete_workspace_audit_logs

    settings = get_settings()
    deleted_audit_count: int = await delete_workspace_audit_logs(
        input.workspace_id,
        mongo_uri=settings.mongodb_uri,
        db_name=settings.mongodb_db_name,
    )
    return deleted_audit_count


@activity.defn
async def verify_purge(input: PurgeWorkspaceInput) -> dict[str, int]:
    """Count remaining rows/objects per store. All zeros (retained-audit rows aside) means the purge is proven.

    Runs LAST, after every delete activity above, so its counts are the
    real post-purge residue -- this dict IS the verification report the
    issue asks for (`{store: remaining_count}`).
    """
    from src.config.settings import get_settings
    from src.temporal.shared_services import get_db_service, get_weaviate_service

    db = get_db_service()
    residue: dict[str, int] = await db.count_workspace_residue(input.workspace_id)

    weaviate = get_weaviate_service()
    residue["weaviate_collection_objects"] = (
        weaviate.workspace_collection_object_count(input.workspace_id) if weaviate else 0
    )

    if input.retain_audit_logs:
        # Retained on purpose -- not residue. Left out of the report
        # entirely rather than reported as a non-zero "failure", since a
        # non-zero count elsewhere means `verified` below must be False.
        pass
    else:
        from src.services.audit_mongo_writer import count_workspace_audit_logs

        settings = get_settings()
        residue["audit_logs"] = await count_workspace_audit_logs(
            input.workspace_id,
            mongo_uri=settings.mongodb_uri,
            db_name=settings.mongodb_db_name,
        )

    return residue


@activity.defn
async def record_purge_receipt(
    input: PurgeWorkspaceInput,
    purge_workflow_id: str,
    counts_before: dict[str, int],
    counts_after: dict[str, int],
) -> None:
    """Write the durable, content-free purge receipt (idempotent upsert keyed by purge_workflow_id)."""
    from src.temporal.shared_services import get_db_service

    db = get_db_service()
    verified = all(count <= 0 for count in counts_after.values())
    await db.record_purge_receipt(
        purge_workflow_id=purge_workflow_id,
        workspace_id=input.workspace_id,
        operator=input.operator,
        retain_audit_logs=input.retain_audit_logs,
        counts_before=counts_before,
        counts_after=counts_after,
        verified=verified,
    )
