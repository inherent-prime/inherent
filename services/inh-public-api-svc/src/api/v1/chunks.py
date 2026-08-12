"""Chunks endpoint — read + single-chunk CRUD (#133)."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from src.core import BadRequestError, ServiceUnavailableError
from src.models.document import (
    DEFAULT_MAX_CHARS,
    MAX_MAX_CHARS,
    MIN_MAX_CHARS,
    ChunkContentRequest,
    DocumentChunk,
    DocumentContextResponse,
    windowed_document_context,
)
from src.services.auth import ResolvedAuth, resolve_workspace_read, resolve_workspace_write
from src.services.chunk_writes import (
    create_chunk_everywhere,
    delete_chunk_everywhere,
    update_chunk_everywhere,
)
from src.services.database import DatabaseService, get_database
from src.utils import get_logger

logger = get_logger(__name__)

router = APIRouter()


@router.get("/chunks/{document_id}", response_model=list[DocumentChunk])
async def get_document_chunks(
    document_id: str,
    auth: Annotated[ResolvedAuth, Depends(resolve_workspace_read)],
    database: Annotated[DatabaseService, Depends(get_database)],
) -> list[DocumentChunk]:
    """
    Get all chunks for a document.

    Requires an API key with 'read' permission.
    Workspace can be specified via ``X-Workspace-Id`` header.
    """
    # Resolve document across workspaces if needed
    document = None
    workspace_id = auth.workspace_id

    if workspace_id:
        document = await database.get_document(
            document_id=document_id,
            workspace_id=workspace_id,
        )
    else:
        user_workspaces = await database.get_user_workspace_ids(auth.key_info.user_id)
        for ws_id in user_workspaces:
            document = await database.get_document(
                document_id=document_id,
                workspace_id=ws_id,
            )
            if document:
                workspace_id = ws_id
                break

    if not document:
        raise HTTPException(status_code=404, detail="Document not found")

    chunks = await database.get_document_chunks(
        document_id=document_id,
        workspace_id=workspace_id,
    )

    return chunks


@router.post(
    "/chunks/{document_id}",
    response_model=DocumentChunk,
    status_code=status.HTTP_201_CREATED,
)
async def create_document_chunk(
    document_id: str,
    body: ChunkContentRequest,
    auth: Annotated[ResolvedAuth, Depends(resolve_workspace_write)] = ...,  # type: ignore[assignment]
    database: Annotated[DatabaseService, Depends(get_database)] = ...,  # type: ignore[assignment]
) -> DocumentChunk:
    """Append a chunk at ``max(chunk_index)+1`` (#133 Option A).

    Writes PostgreSQL then Weaviate (with embedding). Vector failure rolls
    back the PG row. Requires **write** permission. ``chunk_index`` may have
    gaps after deletes — treat it as a stable id, not a dense sequence.
    """
    workspace_id = auth.workspace_id
    if not workspace_id:
        raise BadRequestError(
            detail="Workspace ID required. Provide X-Workspace-Id header.",
        )

    try:
        outcome = await create_chunk_everywhere(database, document_id, workspace_id, body.content)
    except Exception as exc:
        logger.error(
            "Chunk create failed after compensation attempt",
            document_id=document_id,
            workspace_id=workspace_id,
            error=str(exc),
        )
        raise ServiceUnavailableError(
            service_name="chunk_write",
            detail="Failed to create the chunk. Please try again later.",
        ) from exc

    if not outcome.found or outcome.chunk is None:
        raise HTTPException(status_code=404, detail="Document not found")

    return outcome.chunk


@router.get("/chunks/{document_id}/context", response_model=DocumentContextResponse)
async def get_document_context(
    document_id: str,
    auth: Annotated[ResolvedAuth, Depends(resolve_workspace_read)],
    database: Annotated[DatabaseService, Depends(get_database)],
    max_chars: Annotated[
        int,
        Query(
            ge=MIN_MAX_CHARS,
            le=MAX_MAX_CHARS,
            description="Max characters of full_text to return (also bounds the "
            "chunks array to the same window). Default and cap chosen so one "
            "call can't blow an LLM context window (#219).",
        ),
    ] = DEFAULT_MAX_CHARS,
    offset: Annotated[
        int,
        Query(
            ge=0,
            description="Character offset into the combined document text to "
            "resume from -- use the previous response's next_offset to page.",
        ),
    ] = 0,
) -> DocumentContextResponse:
    """
    Get full document context (document metadata + chunks + combined full_text),
    bounded to ``max_chars`` characters starting at ``offset`` (#219).

    ``full_text`` and ``chunks`` are BOTH windowed to the same range -- an
    unbounded response used to concatenate every chunk with no limit (a
    169-chunk PDF returned 298 KB / ~29,300 tokens in one call). ``truncated``
    tells you whether more text remains; when it does, ``next_offset`` is the
    offset to request next. See ``windowed_document_context`` for the exact
    slicing rule.

    Useful for retrieving complete document content for AI context.
    Requires an API key with 'read' permission.
    Workspace can be specified via ``X-Workspace-Id`` header.
    """
    document = None
    workspace_id = auth.workspace_id

    if workspace_id:
        document = await database.get_document(
            document_id=document_id,
            workspace_id=workspace_id,
        )
    else:
        user_workspaces = await database.get_user_workspace_ids(auth.key_info.user_id)
        for ws_id in user_workspaces:
            document = await database.get_document(
                document_id=document_id,
                workspace_id=ws_id,
            )
            if document:
                workspace_id = ws_id
                break

    if not document:
        raise HTTPException(status_code=404, detail="Document not found")

    chunks = await database.get_document_chunks(
        document_id=document_id,
        workspace_id=workspace_id,
    )

    window = windowed_document_context(chunks, offset=offset, max_chars=max_chars)

    return DocumentContextResponse(
        document=document,
        chunks=window.chunks,
        full_text=window.full_text,
        truncated=window.truncated,
        total_chars=window.total_chars,
        offset=window.offset,
        next_offset=window.next_offset,
    )


@router.patch("/chunks/{document_id}/{chunk_index}", response_model=DocumentChunk)
async def update_document_chunk(
    document_id: str,
    chunk_index: int,
    body: ChunkContentRequest,
    auth: Annotated[ResolvedAuth, Depends(resolve_workspace_write)] = ...,  # type: ignore[assignment]
    database: Annotated[DatabaseService, Depends(get_database)] = ...,  # type: ignore[assignment]
) -> DocumentChunk:
    """Edit one chunk by stable ``chunk_index`` (#133).

    Updates PostgreSQL then re-embeds in Weaviate (vector + content_hash).
    Vector failure restores prior PG content. Requires **write** permission.
    """
    workspace_id = auth.workspace_id
    if not workspace_id:
        raise BadRequestError(
            detail="Workspace ID required. Provide X-Workspace-Id header.",
        )

    try:
        outcome = await update_chunk_everywhere(
            database, document_id, workspace_id, chunk_index, body.content
        )
    except Exception as exc:
        logger.error(
            "Chunk update failed after compensation attempt",
            document_id=document_id,
            workspace_id=workspace_id,
            chunk_index=chunk_index,
            error=str(exc),
        )
        raise ServiceUnavailableError(
            service_name="chunk_write",
            detail="Failed to update the chunk. Please try again later.",
        ) from exc

    if not outcome.found or outcome.chunk is None:
        raise HTTPException(status_code=404, detail="Chunk not found")

    return outcome.chunk


@router.delete(
    "/chunks/{document_id}/{chunk_index}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_document_chunk(
    document_id: str,
    chunk_index: int,
    auth: Annotated[ResolvedAuth, Depends(resolve_workspace_write)] = ...,  # type: ignore[assignment]
    database: Annotated[DatabaseService, Depends(get_database)] = ...,  # type: ignore[assignment]
) -> Response:
    """Hard-delete one chunk (gaps allowed — Option A) (#133).

    Deletes the Weaviate object first, then the PG row. Vector failure leaves
    the row intact (retryable). Requires **write** permission.
    """
    workspace_id = auth.workspace_id
    if not workspace_id:
        raise BadRequestError(
            detail="Workspace ID required. Provide X-Workspace-Id header.",
        )

    try:
        outcome = await delete_chunk_everywhere(database, document_id, workspace_id, chunk_index)
    except Exception as exc:
        logger.error(
            "Chunk deletion failed; chunk left intact",
            document_id=document_id,
            workspace_id=workspace_id,
            chunk_index=chunk_index,
            error=str(exc),
        )
        raise ServiceUnavailableError(
            service_name="chunk_write",
            detail="Failed to delete the chunk. Please try again later.",
        ) from exc

    if not outcome.found:
        raise HTTPException(status_code=404, detail="Chunk not found")

    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/chunks/{document_id}/{chunk_id}", response_model=DocumentChunk)
async def get_document_chunk(
    document_id: str,
    chunk_id: str,
    auth: Annotated[ResolvedAuth, Depends(resolve_workspace_read)],
    database: Annotated[DatabaseService, Depends(get_database)],
) -> DocumentChunk:
    """
    Get a single chunk by document_id + chunk_id (#87 API parity).

    Requires an API key with 'read' permission.
    Workspace can be specified via ``X-Workspace-Id`` header. A chunk_id that
    exists but belongs to a different document or a foreign workspace reads
    as 404 (no cross-tenant existence leak). Registered AFTER the literal
    ``/context`` route above so that path is matched first (FastAPI/Starlette
    matches routes in registration order; this generic ``{chunk_id}`` path
    would otherwise shadow it). Also registered AFTER PATCH/DELETE by
    ``chunk_index`` so integer path segments hit the write routes for those
    methods (GET still keys on BIGSERIAL id).
    """
    workspace_id = auth.workspace_id
    chunk = None

    if workspace_id:
        chunk = await database.get_document_chunk(
            document_id=document_id,
            chunk_id=chunk_id,
            workspace_id=workspace_id,
        )
    else:
        user_workspaces = await database.get_user_workspace_ids(auth.key_info.user_id)
        for ws_id in user_workspaces:
            chunk = await database.get_document_chunk(
                document_id=document_id,
                chunk_id=chunk_id,
                workspace_id=ws_id,
            )
            if chunk:
                break

    if not chunk:
        raise HTTPException(status_code=404, detail="Chunk not found")

    return chunk
