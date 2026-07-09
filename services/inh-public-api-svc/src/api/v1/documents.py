"""Documents endpoint."""

from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, Query, Response, UploadFile, status

from src.config import settings
from src.core.exceptions import BadRequestError, ServiceUnavailableError
from src.models.document import Document, DocumentListResponse, DocumentUploadResponse
from src.services.auth import ResolvedAuth, resolve_workspace_read, resolve_workspace_write
from src.services.database import DatabaseService, get_database
from src.services.deletion import delete_document_everywhere
from src.services.document_intake import intake_document
from src.services.lineage import LineageResponse, build_lineage
from src.services.mq import get_mq_service
from src.utils import get_logger

logger = get_logger(__name__)

router = APIRouter()


@router.post("/documents", response_model=DocumentUploadResponse, status_code=201)
async def upload_document(
    file: UploadFile = File(...),
    auth: Annotated[ResolvedAuth, Depends(resolve_workspace_write)] = ...,  # type: ignore[assignment]
    database: Annotated[DatabaseService, Depends(get_database)] = ...,  # type: ignore[assignment]
) -> DocumentUploadResponse:
    """
    Upload a document for ingestion.

    The file is stored in S3 and a message is published to the ingestion
    pipeline.  Processing happens asynchronously; the returned status will
    be ``"pending"`` until the ingestion service completes.

    Requires an API key with **write** permission.
    Workspace can be specified via ``X-Workspace-Id`` header.
    """
    # resolve_workspace_write guarantees workspace_id is set, but guard anyway
    workspace_id = auth.workspace_id
    if not workspace_id:
        raise BadRequestError(
            detail="Workspace ID required. Provide X-Workspace-Id header.",
        )

    content_type = file.content_type or "application/octet-stream"
    file_content = await file.read()
    filename = file.filename or "unnamed"

    # Validation, dedup, S3 upload, pending-row persistence and MQ publish are
    # all shared with the upload_document MCP tool via intake_document (#87).
    return await intake_document(
        database=database,
        workspace_id=workspace_id,
        user_id=auth.key_info.user_id,
        content_bytes=file_content,
        filename=filename,
        content_type=content_type,
    )


@router.get("/documents", response_model=DocumentListResponse)
async def list_documents(
    auth: Annotated[ResolvedAuth, Depends(resolve_workspace_read)],
    database: Annotated[DatabaseService, Depends(get_database)],
    page: int = Query(default=1, ge=1, description="Page number"),
    page_size: int = Query(default=20, ge=1, le=100, description="Items per page"),
) -> DocumentListResponse:
    """
    List documents in the workspace.

    Requires an API key with 'read' permission.
    Workspace can be specified via ``X-Workspace-Id`` header.
    """
    if auth.workspace_id:
        documents, total = await database.get_documents(
            workspace_id=auth.workspace_id,
            page=page,
            page_size=page_size,
        )
    else:
        # User-scoped key with no workspace specified — list across all workspaces
        user_workspaces = await database.get_user_workspace_ids(auth.key_info.user_id)
        if user_workspaces:
            documents, total = await database.get_documents_multi_workspace(
                workspace_ids=user_workspaces,
                page=page,
                page_size=page_size,
            )
        else:
            documents, total = [], 0

    return DocumentListResponse(
        documents=documents,
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/documents/{document_id}", response_model=Document)
async def get_document(
    document_id: str,
    auth: Annotated[ResolvedAuth, Depends(resolve_workspace_read)],
    database: Annotated[DatabaseService, Depends(get_database)],
) -> Document:
    """
    Get a specific document by ID.

    Requires an API key with 'read' permission.
    """
    if auth.workspace_id:
        document = await database.get_document(
            document_id=document_id,
            workspace_id=auth.workspace_id,
        )
    else:
        # User-scoped key — search across all user's workspaces
        user_workspaces = await database.get_user_workspace_ids(auth.key_info.user_id)
        document = None
        for ws_id in user_workspaces:
            document = await database.get_document(
                document_id=document_id,
                workspace_id=ws_id,
            )
            if document:
                break

    if not document:
        raise HTTPException(status_code=404, detail="Document not found")

    return document


@router.delete("/documents/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(
    document_id: str,
    auth: Annotated[ResolvedAuth, Depends(resolve_workspace_write)] = ...,  # type: ignore[assignment]
    database: Annotated[DatabaseService, Depends(get_database)] = ...,  # type: ignore[assignment]
) -> Response:
    """
    Delete a document and all of its derived data (#87).

    Removes the document's Weaviate objects (tenant-scoped), its PostgreSQL
    row + chunks (transactional), and best-effort the stored S3 bytes. The
    lookup is workspace-scoped, so a document in a workspace the caller
    can't see returns ``404`` — existence never leaks across workspaces.

    Returns ``204`` on success. Repeating the delete returns ``404`` (the
    document is already gone). A vector-store outage returns ``503`` and
    leaves the document intact — safe to retry.

    Requires an API key with **write** permission.
    Workspace can be specified via ``X-Workspace-Id`` header.
    """
    workspace_id = auth.workspace_id
    # resolve_workspace_write guarantees workspace_id is set, but guard anyway
    if not workspace_id:
        raise BadRequestError(
            detail="Workspace ID required. Provide X-Workspace-Id header.",
        )

    try:
        outcome = await delete_document_everywhere(database, document_id, workspace_id)
    except Exception as exc:
        # Vectors (or the row) survived — nothing user-visible was half-deleted,
        # so surface a retryable failure instead of a silent partial delete.
        logger.error(
            "Document deletion failed; document left intact",
            document_id=document_id,
            workspace_id=workspace_id,
            error=str(exc),
        )
        raise ServiceUnavailableError(
            service_name="deletion",
            detail="Failed to delete the document. Please try again later.",
        ) from exc

    if not outcome.found:
        raise HTTPException(status_code=404, detail="Document not found")

    logger.info(
        "Document deletion accepted",
        document_id=document_id,
        workspace_id=workspace_id,
        chunks_deleted=outcome.chunks_deleted,
        vectors_deleted=outcome.vectors_deleted,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/documents/{document_id}/lineage", response_model=LineageResponse)
async def get_document_lineage(
    document_id: str,
    auth: Annotated[ResolvedAuth, Depends(resolve_workspace_read)],
    database: Annotated[DatabaseService, Depends(get_database)],
    chunk_id: str | None = Query(
        default=None, description="Optional chunk ID for chunk-level provenance"
    ),
) -> LineageResponse:
    """Explain a document's (or chunk's) provenance and freshness (#40).

    Returns ``source_uri``, ``content_hash``, ``ingested_at``, ``is_stale`` and
    ``document_name`` projected from already-ingested data. ``is_stale`` is
    computed with the same freshness logic the search path uses, so lineage and
    search agree. Requires an API key with **read** permission.
    """
    if auth.workspace_id:
        document = await database.get_document(
            document_id=document_id,
            workspace_id=auth.workspace_id,
        )
    else:
        user_workspaces = await database.get_user_workspace_ids(auth.key_info.user_id)
        document = None
        for ws_id in user_workspaces:
            document = await database.get_document(document_id=document_id, workspace_id=ws_id)
            if document:
                break

    if not document:
        raise HTTPException(status_code=404, detail="Document not found")

    chunks = await database.get_document_chunks(document_id, document.workspace_id)
    try:
        return build_lineage(document, chunks, chunk_id=chunk_id)
    except KeyError as exc:
        raise HTTPException(
            status_code=404,
            detail=f"Chunk '{chunk_id}' not found in document '{document_id}'",
        ) from exc


@router.post("/documents/{document_id}/refresh", response_model=DocumentUploadResponse)
async def refresh_document(
    document_id: str,
    auth: Annotated[ResolvedAuth, Depends(resolve_workspace_write)] = ...,  # type: ignore[assignment]
    database: Annotated[DatabaseService, Depends(get_database)] = ...,  # type: ignore[assignment]
) -> DocumentUploadResponse:
    """Re-ingest an already-uploaded document (#42 freshness refresh).

    Rebuilds the original ``document.uploaded`` event from the stored
    ``processed_documents`` row and re-publishes it to the ingestion MQ topic,
    reusing the same publish path as upload. Ingestion is idempotent (M2
    #11/#60): the document_id is unchanged, so existing chunks are replaced and
    their ``ingested_at`` is reset — clearing any ``is_stale`` flag.

    Requires an API key with **write** permission (which also implies read for
    these keys); the workspace is resolved from the API key / X-Workspace-Id.

    Limits: the stored S3 object referenced by ``storage_path`` must still
    exist; this endpoint does NOT re-upload bytes, it only re-triggers
    processing of the already-stored file. If the original bytes were deleted,
    ingestion will fail downstream (surfaced via the document's status).
    """
    workspace_id = auth.workspace_id
    if not workspace_id:
        raise BadRequestError(
            detail="Workspace ID required. Provide X-Workspace-Id header.",
        )

    # Defense in depth: write keys are expected to also carry read, and refresh
    # both reads the stored row and triggers a mutation. Require read explicitly.
    if not auth.key_info.has_permission("read"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="API key does not have 'read' permission",
        )

    fields = await database.get_document_upload_fields(document_id, workspace_id)
    if not fields:
        raise HTTPException(status_code=404, detail="Document not found")

    # Rebuild the upload event from the stored row and reset the row to pending
    # so status reflects the in-flight re-ingestion (mirrors the upload path).
    try:
        await database.create_or_reset_pending_document(
            document_id=fields["document_id"],
            workspace_id=fields["workspace_id"],
            user_id=fields["user_id"],
            filename=fields["filename"],
            original_filename=fields["original_filename"],
            content_type=fields["content_type"],
            size_bytes=fields["size_bytes"] or 0,
            storage_backend=fields["storage_backend"],
            storage_path=fields["storage_path"],
            storage_bucket=fields.get("storage_bucket"),
            storage_url=fields.get("storage_url"),
        )
    except Exception as exc:
        logger.error(
            "Failed to reset document to pending for refresh",
            error=str(exc),
            document_id=document_id,
        )
        raise ServiceUnavailableError(
            service_name="database",
            detail="Failed to record the refresh. Please try again later.",
        ) from exc

    now_iso = datetime.now(timezone.utc).isoformat()
    mq_message = {
        "event_type": "document.uploaded",
        "document_id": fields["document_id"],
        "workspace_id": fields["workspace_id"],
        "user_id": fields["user_id"],
        "filename": fields["filename"],
        "original_filename": fields["original_filename"],
        "content_type": fields["content_type"],
        "size_bytes": fields["size_bytes"],
        "storage_backend": fields["storage_backend"],
        "storage_path": fields["storage_path"],
        "storage_bucket": fields.get("storage_bucket"),
        "storage_url": fields.get("storage_url"),
        "timestamp": now_iso,
        "contract_version": "1.0.0",
    }

    try:
        mq = await get_mq_service()
        await mq.publish(settings.mq_topic_document_uploaded, mq_message)
    except Exception as exc:
        logger.error(
            "MQ publish failed during refresh — re-ingestion not enqueued",
            error=str(exc),
            document_id=document_id,
        )
        enqueue_error = "refresh enqueue failed"
        try:
            await database.mark_document_failed(document_id, workspace_id, enqueue_error)
        except Exception as mark_exc:
            logger.error(
                "Failed to mark document failed after refresh enqueue failure",
                error=str(mark_exc),
                document_id=document_id,
            )
        raise ServiceUnavailableError(
            service_name="mq",
            detail="Failed to queue the document for re-processing. Please try again later.",
        ) from exc

    logger.info(
        "Document refresh accepted",
        document_id=document_id,
        workspace_id=workspace_id,
    )

    return DocumentUploadResponse(
        document_id=fields["document_id"],
        name=fields["original_filename"],
        workspace_id=fields["workspace_id"],
        storage_url=fields.get("storage_url") or "",
        mime_type=fields["content_type"],
        size_bytes=fields["size_bytes"] or 0,
        status="pending",
        message="Document queued for re-ingestion (refresh).",
    )
