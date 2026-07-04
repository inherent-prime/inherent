"""Storage activities for persisting documents to PostgreSQL and Weaviate.

Reads chunks from staging (instead of receiving them via gRPC).
Uses shared connection pools from shared_services.
"""

import time

import structlog
from temporalio import activity

from src.models.document import DocumentChunk, DocumentUploadMessage
from src.temporal.models import StoreDocumentInput, StoreDocumentOutput

logger = structlog.get_logger(__name__)


def _risk_metadata(chunk_dict: dict) -> dict | None:
    """Build chunk metadata carrying the RAG-poisoning risk signal (#44).

    Only emitted when a risk level is present in the staged chunk so benign
    chunks keep a clean/None metadata payload. Additive: any future metadata
    keys can be merged here.
    """
    risk = chunk_dict.get("content_risk")
    if not risk or risk == "none":
        return None
    return {
        "content_risk": risk,
        "content_risk_reasons": list(chunk_dict.get("content_risk_reasons") or []),
    }


@activity.defn
async def store_in_postgresql(input: StoreDocumentInput) -> StoreDocumentOutput:
    """Store processed document and chunks in PostgreSQL.

    This activity:
    1. Reads chunks from staging
    2. Stores document metadata in processed_documents table
    3. Stores all chunks in document_chunks table with FK relationship
    4. Updates document status to 'processed'

    Args:
        input: Contains document metadata and workflow_run_id to read chunks from staging

    Returns:
        StoreDocumentOutput with success status and chunks stored count
    """
    from src.temporal.shared_services import get_db_service, get_staging_service

    staging = get_staging_service()
    chunk_dicts = staging.read_chunks(input.workflow_run_id)

    db_service = get_db_service()
    start = time.monotonic()

    try:
        # Convert chunk dicts to DocumentChunk objects. The per-chunk risk
        # signal (#44) is carried in metadata so it lands in the document_chunks
        # metadata JSONB column without a new migration.
        chunks = [
            DocumentChunk(
                document_id=c["document_id"],
                content=c["content"],
                chunk_index=c["chunk_index"],
                start_char=c["start_char"],
                end_char=c["end_char"],
                token_count=c.get("token_count"),
                metadata=_risk_metadata(c),
            )
            for c in chunk_dicts
        ]

        # Create a DocumentUploadMessage-like object for the database service
        message = DocumentUploadMessage(
            event_type="document.uploaded",
            document_id=input.document_id,
            workspace_id=input.workspace_id,
            user_id=input.user_id,
            filename=input.filename,
            original_filename=input.original_filename,
            content_type=input.content_type,
            size_bytes=input.size_bytes,
            storage_backend=input.storage_backend,  # type: ignore[arg-type]
            storage_path=input.storage_path,
            storage_bucket=None,
            storage_url=None,
            timestamp="",  # Not needed for storage
        )

        await db_service.store_processed_document(
            message=message,
            chunks=chunks,
            text_length=input.text_length,
            processing_time_ms=input.processing_time_ms,
            tenant_id=input.tenant_id,
        )

        duration_ms = int((time.monotonic() - start) * 1000)

        logger.info(
            "Stored document in PostgreSQL",
            document_id=input.document_id,
            chunks_stored=len(chunks),
            tenant_id=input.tenant_id,
        )

        # Record lineage event on success
        try:
            await db_service.record_ingestion_event(
                workflow_run_id=input.workflow_run_id,
                document_id=input.document_id,
                workspace_id=input.workspace_id,
                event_type="stored_postgresql",
                status="succeeded",
                duration_ms=duration_ms,
            )
        except Exception as rec_err:
            logger.warning(
                "Failed to record lineage event",
                event_type="stored_postgresql",
                error=str(rec_err),
            )

        return StoreDocumentOutput(
            success=True,
            chunks_stored=len(chunks),
            error=None,
        )

    except Exception as e:
        duration_ms = int((time.monotonic() - start) * 1000)

        logger.error(
            "Failed to store document in PostgreSQL",
            document_id=input.document_id,
            error=str(e),
            exc_info=True,
        )

        # Record lineage event on failure
        try:
            await db_service.record_ingestion_event(
                workflow_run_id=input.workflow_run_id,
                document_id=input.document_id,
                workspace_id=input.workspace_id,
                event_type="stored_postgresql",
                status="failed",
                duration_ms=duration_ms,
                metadata={"error": str(e)},
            )
        except Exception as rec_err:
            logger.warning(
                "Failed to record lineage event",
                event_type="stored_postgresql",
                error=str(rec_err),
            )

        # Re-raise so Temporal's RetryPolicy (maximum_attempts=5) fires. Returning
        # success=False here is a *successful* activity completion → no retry and
        # an instant dead-letter on the first transient blip. If retries are
        # exhausted, the workflow's outer handler dead-letters the doc (#2).
        raise


@activity.defn
async def store_in_weaviate(input: StoreDocumentInput) -> StoreDocumentOutput:
    """Store document chunks in Weaviate for semantic search.

    This activity:
    1. Reads chunks from staging
    2. Ensures workspace collection exists
    3. Ensures user tenant exists within collection
    4. Stores all chunks with multi-tenant isolation

    Args:
        input: Contains document metadata and workflow_run_id to read chunks from staging

    Returns:
        StoreDocumentOutput with success status and chunks stored count
    """
    from src.temporal.shared_services import (
        get_db_service,
        get_staging_service,
        get_weaviate_service,
    )

    staging = get_staging_service()
    chunk_dicts = staging.read_chunks(input.workflow_run_id)

    weaviate_service = get_weaviate_service()
    start = time.monotonic()

    try:
        if weaviate_service is None or not weaviate_service.is_connected():
            logger.warning("Weaviate not connected, skipping storage")

            # Record lineage event for skipped weaviate
            duration_ms = int((time.monotonic() - start) * 1000)
            try:
                db_service = get_db_service()
                await db_service.record_ingestion_event(
                    workflow_run_id=input.workflow_run_id,
                    document_id=input.document_id,
                    workspace_id=input.workspace_id,
                    event_type="stored_weaviate",
                    status="failed",
                    duration_ms=duration_ms,
                    metadata={"error": "Weaviate not connected"},
                )
            except Exception as rec_err:
                logger.warning(
                    "Failed to record lineage event",
                    event_type="stored_weaviate",
                    error=str(rec_err),
                )

            # Raise so Temporal retries — a not-connected Weaviate is often a
            # transient reconnect window; the RetryPolicy should get a shot
            # before the doc is failed/dead-lettered (#2).
            raise RuntimeError("Weaviate not connected")

        # Convert chunk dicts to DocumentChunk objects. metadata carries the
        # per-chunk risk signal (#44) so it can be written as Weaviate properties.
        chunks = [
            DocumentChunk(
                document_id=c["document_id"],
                content=c["content"],
                chunk_index=c["chunk_index"],
                start_char=c["start_char"],
                end_char=c["end_char"],
                token_count=c.get("token_count"),
                metadata=_risk_metadata(c),
            )
            for c in chunk_dicts
        ]

        # Idempotent reindex: delete any existing chunks for this document
        # before writing the new ones. Without this, re-processing that
        # produces fewer chunks leaves stale higher-index chunks orphaned
        # (deterministic UUIDs only overwrite matching indexes). Use the
        # graceful variant so a Weaviate hiccup during delete doesn't
        # hard-fail the activity; we log and proceed to the write.
        deleted_ok, deleted_count = await weaviate_service.delete_document_chunks_graceful(
            workspace_id=input.workspace_id,
            document_id=input.document_id,
            user_id=input.user_id,
        )
        if not deleted_ok:
            logger.warning(
                "Could not delete existing Weaviate chunks before reindex (non-fatal)",
                document_id=input.document_id,
                workspace_id=input.workspace_id,
                user_id=input.user_id,
            )

        await weaviate_service.store_chunks_with_tenant(
            chunks=chunks,
            document_id=input.document_id,
            workspace_id=input.workspace_id,
            user_id=input.user_id,
            original_filename=input.original_filename,
            content_type=input.content_type,
            # Provenance (#41): record where the source bytes live.
            source_uri=input.storage_path,
        )

        duration_ms = int((time.monotonic() - start) * 1000)

        logger.info(
            "Stored document in Weaviate",
            document_id=input.document_id,
            workspace_id=input.workspace_id,
            user_id=input.user_id,
            chunks_stored=len(chunks),
        )

        # Record lineage event on success
        try:
            db_service = get_db_service()
            await db_service.record_ingestion_event(
                workflow_run_id=input.workflow_run_id,
                document_id=input.document_id,
                workspace_id=input.workspace_id,
                event_type="stored_weaviate",
                status="succeeded",
                duration_ms=duration_ms,
            )
        except Exception as rec_err:
            logger.warning(
                "Failed to record lineage event",
                event_type="stored_weaviate",
                error=str(rec_err),
            )

        return StoreDocumentOutput(
            success=True,
            chunks_stored=len(chunks),
            error=None,
        )

    except Exception as e:
        duration_ms = int((time.monotonic() - start) * 1000)

        logger.error(
            "Failed to store document in Weaviate",
            document_id=input.document_id,
            error=str(e),
            exc_info=True,
        )

        # Record lineage event on failure
        try:
            db_service = get_db_service()
            await db_service.record_ingestion_event(
                workflow_run_id=input.workflow_run_id,
                document_id=input.document_id,
                workspace_id=input.workspace_id,
                event_type="stored_weaviate",
                status="failed",
                duration_ms=duration_ms,
                metadata={"error": str(e)},
            )
        except Exception as rec_err:
            logger.warning(
                "Failed to record lineage event",
                event_type="stored_weaviate",
                error=str(rec_err),
            )

        # Re-raise so Temporal's RetryPolicy (maximum_attempts=5) fires; a
        # swallowed success=False is a completion → no retry (#2). Exhausted
        # retries land in the workflow's outer handler → failed + dead-letter.
        raise
