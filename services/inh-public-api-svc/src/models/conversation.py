"""Conversation-related models (#306).

Mirrors ``src/models/document.py``'s shape (request/response pairs per
route) for the conversation ingestion API:

    POST   /v1/conversations/{external_id}/turns   -> 202, batch append
    GET    /v1/conversations/{external_id}          -> turn/chunk counts
    DELETE /v1/conversations/{external_id}          -> cascades chunks + vectors
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class ConversationTurnIn(BaseModel):
    """One turn in a `POST /v1/conversations/{external_id}/turns` batch.

    Field-for-field the issue's stated turn shape:
    ``{ turn_id, role, text, ts, client }``.
    """

    turn_id: str = Field(..., min_length=1, description="Idempotency key for this turn")
    role: Literal["user", "assistant"] = Field(..., description="Who produced this turn")
    text: str = Field(..., min_length=1, description="Turn text")
    ts: str = Field(..., description="ISO 8601 timestamp the turn was produced at")
    client: str | None = Field(None, description="Caller-supplied client/application label")


class ConversationTurnBatchRequest(BaseModel):
    """Request body for `POST /v1/conversations/{external_id}/turns`."""

    turns: list[ConversationTurnIn] = Field(
        ..., min_length=1, description="One or more turns to append"
    )


class ConversationTurnBatchResponse(BaseModel):
    """Response for `POST /v1/conversations/{external_id}/turns` (202)."""

    external_id: str
    workspace_id: str
    accepted: int = Field(..., description="Number of turns published for processing")
    message: str = "Turns accepted for processing."


class ConversationResponse(BaseModel):
    """Response for `GET /v1/conversations/{external_id}`."""

    external_id: str
    workspace_id: str
    turn_count: int = 0
    chunk_count: int = 0
    last_flushed_at: datetime | None = None
    status: str = "pending"
    created_at: datetime
    updated_at: datetime
