"""Documents endpoint."""

import hashlib
import uuid
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, Query, Response, UploadFile, status

from src.config import settings
from src.config.constants import ALLOWED_MIME_TYPES, MAX_UPLOAD_SIZE_BYTES
from src.core.exceptions import BadRequestError, ServiceUnavailableError
from src.models.document import Document, DocumentListResponse, DocumentUploadResponse
from src.services.auth import ResolvedAuth, resolve_workspace_read, resolve_workspace_write
from src.services.database import DatabaseService, get_database
from src.services.deletion import delete_document_everywhere
from src.services.lineage import LineageResponse, build_lineage
from src.services.mq import get_mq_service
from src.services.storage import get_storage_service
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
    # --- 1. Validate content type -------------------------------------------
    content_type = file.content_type or "application/octet-stream"
    if content_type not in ALLOWED_MIME_TYPES:
        raise BadRequestError(
            detail=(
                f"Unsupported file type '{content_type}'. "
                f"Allowed types: {', '.join(ALLOWED_MIME_TYPES)}"
            ),
        )

    # --- 2. Read file content & validate size --------------------------------
    file_content = await file.read()
    size_bytes = len(file_content)

    if size_bytes == 0:
        raise BadRequestError(detail="Uploaded file is empty.")

    if size_bytes > MAX_UPLOAD_SIZE_BYTES:
        max_mb = MAX_UPLOAD_SIZE_BYTES // (1024 * 1024)
        raise BadRequestError(
            detail=f"File size ({size_bytes} bytes) exceeds the {max_mb} MB limit.",
        )

    # --- 3. Determine identifiers -------------------------------------------
    workspace_id = auth.workspace_id
    # resolve_workspace_write guarantees workspace_id is set, but guard anyway
    if not workspace_id:
        raise BadRequestError(
            detail="Workspace ID required. Provide X-Workspace-Id header.",
        )

    filename = file.filename or "unnamed"
    content_hash = hashlib.sha256(file_content).hexdigest()

    # --- 3b. Dedup: reuse document_id rather than flood the workspace --------
    # Two re-upload shapes must collapse onto an existing document_id so
    # ingestion reindexes it instead of creating a duplicate document (with
    # duplicate chunks + embeddings) that floods top-k search results (#75):
    #   1. Same CONTENT under any filename — keyed on (workspace, content_hash).
    #      Checked first so a verbatim copy uploaded as ``guide-copy.md``
    #      collapses onto the original ``guide.md`` instead of multiplying it.
    #   2. Same FILENAME with changed content — keyed on (workspace, filename).
    #      Preserves the existing reindex-on-edit behaviour (#60) for a file
    #      whose bytes changed but whose logical identity (name) is unchanged.
    existing_document_id = await database.get_document_id_by_content_hash(
        workspace_id, content_hash
    )
    dedup_reason = "content_hash" if existing_document_id else None
    if not existing_document_id:
        existing_document_id = await database.get_document_id_by_filename(workspace_id, filename)
        dedup_reason = "filename" if existing_document_id else None

    if existing_document_id:
        document_id = existing_document_id
        logger.info(
            "Reusing existing document_id for re-upload (reindex)",
            document_id=document_id,
            workspace_id=workspace_id,
            filename=filename,
            dedup_reason=dedup_reason,
        )
    else:
        document_id = str(uuid.uuid4())
        logger.info(
            "Assigning new document_id for upload",
            document_id=document_id,
            workspace_id=workspace_id,
            filename=filename,
        )

    # --- 4. Upload to S3 ----------------------------------------------------
    try:
        storage = get_storage_service()
        s3_key = storage.generate_key(workspace_id, filename)
        await storage.upload_file(file_content, s3_key, content_type)
        storage_url = storage.build_storage_url(s3_key)
    except Exception as exc:
        logger.error("S3 upload failed", error=str(exc), document_id=document_id)
        raise ServiceUnavailableError(
            service_name="storage",
            detail="Failed to store the uploaded file. Please try again later.",
        ) from exc

    # --- 5. Persist a durable 'pending' row BEFORE enqueueing ----------------
    # This makes the upload recoverable and lets GET /v1/documents/{id} return
    # the document (status='pending') immediately, instead of 404ing until
    # ingestion finishes. On re-upload of the same document_id, this resets the
    # row to a clean pending state.
    try:
        await database.create_or_reset_pending_document(
            document_id=document_id,
            workspace_id=workspace_id,
            user_id=auth.key_info.user_id,
            filename=s3_key.rsplit("/", 1)[-1],
            original_filename=filename,
            content_type=content_type,
            size_bytes=size_bytes,
            storage_backend="s3",
            storage_path=s3_key,
            storage_bucket=storage._bucket,
            storage_url=storage_url,
            content_hash=content_hash,
        )
    except Exception as exc:
        logger.error(
            "Failed to persist pending document row",
            error=str(exc),
            document_id=document_id,
        )
        raise ServiceUnavailableError(
            service_name="database",
            detail="Failed to record the upload. Please try again later.",
        ) from exc

    # --- 6. Publish MQ message ----------------------------------------------
    now_iso = datetime.now(timezone.utc).isoformat()
    mq_message = {
        "event_type": "document.uploaded",
        "document_id": document_id,
        "workspace_id": workspace_id,
        "user_id": auth.key_info.user_id,
        "filename": s3_key.rsplit("/", 1)[-1],
        "original_filename": filename,
        "content_type": content_type,
        "size_bytes": size_bytes,
        "storage_backend": "s3",
        "storage_path": s3_key,
        "storage_bucket": storage._bucket,
        "storage_url": storage_url,
        "timestamp": now_iso,
        "contract_version": "1.0.0",
    }

    try:
        mq = await get_mq_service()
        await mq.publish(settings.mq_topic_document_uploaded, mq_message)
    except Exception as exc:
        # The file is in S3 and a durable 'pending' row exists, so the upload
        # is recoverable. But ingestion was NOT triggered, so we must NOT
        # report success: mark the row 'failed' and reflect that in the
        # response. We keep HTTP 201 because the file IS stored.
        logger.error(
            "MQ publish failed — file stored but ingestion not enqueued",
            error=str(exc),
            document_id=document_id,
        )
        enqueue_error = "ingestion enqueue failed"
        try:
            await database.mark_document_failed(document_id, workspace_id, enqueue_error)
        except Exception as mark_exc:
            logger.error(
                "Failed to mark document as failed after enqueue failure",
                error=str(mark_exc),
                document_id=document_id,
            )

        return DocumentUploadResponse(
            document_id=document_id,
            name=filename,
            workspace_id=workspace_id,
            storage_url=storage_url,
            mime_type=content_type,
            size_bytes=size_bytes,
            status="failed",
            message=(
                "Document was stored but could not be queued for processing "
                "(ingestion enqueue failed). Please retry the upload."
            ),
        )

    # --- 7. Return response --------------------------------------------------
    logger.info(
        "Document upload accepted",
        document_id=document_id,
        workspace_id=workspace_id,
        filename=filename,
        size_bytes=size_bytes,
    )

    return DocumentUploadResponse(
        document_id=document_id,
        name=filename,
        workspace_id=workspace_id,
        storage_url=storage_url,
        mime_type=content_type,
        size_bytes=size_bytes,
        status="pending",
        message="Document uploaded successfully. Processing will begin shortly.",
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
