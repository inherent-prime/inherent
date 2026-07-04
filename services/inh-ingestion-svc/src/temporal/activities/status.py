"""Activity for writing document processing status during the workflow.

Used to mark a document as 'processing' early in the workflow and as
'failed' on error, so callers can observe progress and failures via the
document status (not just on success). Status writes are best-effort: a
failure here must never fail the overall workflow.
"""

import structlog
from temporalio import activity

from src.temporal.models import CreatePendingDocumentInput, SetDocumentStatusInput

logger = structlog.get_logger(__name__)


@activity.defn
async def create_pending_document(input: CreatePendingDocumentInput) -> bool:
    """Create a minimal 'processing' processed_documents row at workflow start.

    Makes the document observable in the status API before the store step, so a
    failure during fetch/extract/chunk is visible instead of 'not found' (#10).
    Best-effort and idempotent (no-op if the row already exists).
    """
    from src.temporal.shared_services import get_db_service

    db_service = get_db_service()
    created = await db_service.create_pending_document(
        document_id=input.document_id,
        workspace_id=input.workspace_id,
        user_id=input.user_id,
        filename=input.filename,
        original_filename=input.original_filename,
        content_type=input.content_type,
        size_bytes=input.size_bytes,
        storage_backend=input.storage_backend,
        storage_path=input.storage_path,
        storage_bucket=input.storage_bucket,
        storage_url=input.storage_url,
    )
    logger.info(
        "Created pending document row",
        document_id=input.document_id,
        created=created,
    )
    return created


@activity.defn
async def set_document_status(input: SetDocumentStatusInput) -> bool:
    """Update a document's processing status in PostgreSQL.

    Delegates to ``DatabaseService.update_document_status``, which performs
    an UPDATE on the processed_documents row. If the row does not exist yet
    (UPDATE affects 0 rows), this is a safe no-op.

    Args:
        input: document_id, workspace_id, status string, optional error_message

    Returns:
        True if a row was updated, False otherwise (e.g. row not yet present).
    """
    from src.services.database import DocumentStatus
    from src.temporal.shared_services import get_db_service

    db_service = get_db_service()

    updated = await db_service.update_document_status(
        document_id=input.document_id,
        status=DocumentStatus(input.status),
        error_message=input.error_message,
    )

    logger.info(
        "Set document status",
        document_id=input.document_id,
        workspace_id=input.workspace_id,
        status=input.status,
        updated=updated,
    )

    return updated
