"""Conversation ingestion endpoints (#306).

    POST   /v1/conversations/{external_id}/turns   -> 202, batch append, idempotent per turn_id
    GET    /v1/conversations/{external_id}          -> turn count, last_flushed_at, chunk count
    DELETE /v1/conversations/{external_id}          -> cascades chunks + vectors

Public-api does not talk to Temporal directly (unchanged boundary): POST
publishes to `core.conversation.turn.v1` and returns immediately (202) --
`ConversationMemoryWorkflow` (inh-ingestion-svc) does the actual buffering,
redaction, chunking, and storage, asynchronously.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status

from src.core.exceptions import BadRequestError, ServiceUnavailableError
from src.models.conversation import (
    ConversationResponse,
    ConversationTurnBatchRequest,
    ConversationTurnBatchResponse,
)
from src.services.auth import ResolvedAuth, resolve_workspace_read, resolve_workspace_write
from src.services.conversation_intake import intake_turns
from src.services.database import DatabaseService, get_database
from src.services.deletion import delete_document_everywhere
from src.utils import get_logger

logger = get_logger(__name__)

router = APIRouter()


@router.post(
    "/conversations/{external_id}/turns",
    response_model=ConversationTurnBatchResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def append_conversation_turns(
    external_id: str,
    body: ConversationTurnBatchRequest,
    auth: Annotated[ResolvedAuth, Depends(resolve_workspace_write)] = ...,  # type: ignore[assignment]
) -> ConversationTurnBatchResponse:
    """Append one or more turns to a conversation.

    The first turn ever published for a given `(workspace_id, external_id)`
    starts `ConversationMemoryWorkflow`; every later turn signals the same
    running workflow (`signal_with_start` + `WorkflowIDConflictPolicy.
    USE_EXISTING`, inh-ingestion-svc's `conversation_trigger.py`). A
    duplicate `turn_id` — a client retry, or an MQ at-least-once redelivery —
    is a no-op on the consumer side, so retrying this whole request is
    always safe.

    Requires an API key with **write** permission.
    Workspace can be specified via ``X-Workspace-Id`` header.
    """
    workspace_id = auth.workspace_id
    if not workspace_id:
        raise BadRequestError(
            detail="Workspace ID required. Provide X-Workspace-Id header.",
        )

    return await intake_turns(
        workspace_id=workspace_id,
        user_id=auth.key_info.user_id,
        external_id=external_id,
        turns=body.turns,
    )


@router.get("/conversations/{external_id}", response_model=ConversationResponse)
async def get_conversation(
    external_id: str,
    auth: Annotated[ResolvedAuth, Depends(resolve_workspace_read)],
    database: Annotated[DatabaseService, Depends(get_database)],
) -> ConversationResponse:
    """Get a conversation's turn count, last flush time, and chunk count.

    Requires an API key with **read** permission.
    Workspace can be specified via ``X-Workspace-Id`` header.
    """
    workspace_id = auth.workspace_id
    if not workspace_id:
        raise BadRequestError(
            detail="Workspace ID required. Provide X-Workspace-Id header.",
        )

    conversation = await database.get_conversation(workspace_id, external_id)
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")

    return ConversationResponse(
        external_id=conversation["external_id"],
        workspace_id=conversation["workspace_id"],
        turn_count=conversation["turn_count"],
        chunk_count=conversation["chunk_count"],
        last_flushed_at=conversation["last_flushed_at"],
        status=conversation["status"],
        created_at=conversation["created_at"],
        updated_at=conversation["updated_at"],
    )


@router.delete("/conversations/{external_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_conversation(
    external_id: str,
    auth: Annotated[ResolvedAuth, Depends(resolve_workspace_write)] = ...,  # type: ignore[assignment]
    database: Annotated[DatabaseService, Depends(get_database)] = ...,  # type: ignore[assignment]
) -> Response:
    """Delete a conversation and all of its derived data (#306).

    Resolves `external_id` -> the conversation's `document_id`
    (`processed_documents`, migration 020), then removes it exactly like
    `DELETE /v1/documents/{document_id}` does: Weaviate vectors first, then
    the PostgreSQL row + chunks — reusing `delete_document_everywhere`
    unmodified rather than forking a conversation-specific delete path. A
    still-running `ConversationMemoryWorkflow` is NOT terminated by this
    call (out of scope for this PR — see the workflow's own 24h idle
    finalize); a turn delivered after deletion re-creates the document row
    on its next flush the same way any store would.

    Returns ``204`` on success. Repeating the delete returns ``404``.

    Requires an API key with **write** permission.
    Workspace can be specified via ``X-Workspace-Id`` header.
    """
    workspace_id = auth.workspace_id
    if not workspace_id:
        raise BadRequestError(
            detail="Workspace ID required. Provide X-Workspace-Id header.",
        )

    document_id = await database.get_document_id_by_external_id(workspace_id, external_id)
    if not document_id:
        raise HTTPException(status_code=404, detail="Conversation not found")

    try:
        outcome = await delete_document_everywhere(database, document_id, workspace_id)
    except Exception as exc:
        logger.error(
            "Conversation deletion failed; document left intact",
            external_id=external_id,
            document_id=document_id,
            workspace_id=workspace_id,
            error=str(exc),
        )
        raise ServiceUnavailableError(
            service_name="deletion",
            detail="Failed to delete the conversation. Please try again later.",
        ) from exc

    if not outcome.found:
        raise HTTPException(status_code=404, detail="Conversation not found")

    logger.info(
        "Conversation deletion accepted",
        external_id=external_id,
        document_id=document_id,
        workspace_id=workspace_id,
        chunks_deleted=outcome.chunks_deleted,
        vectors_deleted=outcome.vectors_deleted,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
