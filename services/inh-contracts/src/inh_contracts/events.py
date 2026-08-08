"""Canonical, versioned cross-service event schemas (#17).

These Pydantic models are the SINGLE source of truth for the events exchanged
between the public API (producer) and the ingestion service (consumer). Both
services import them from here so the contract cannot drift.

``CONTRACT_VERSION`` pins the semantic version of these contracts; the per-model
``contract_version`` field defaults to it so older messages produced without the
field still validate (backward compat).
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

CONTRACT_VERSION = "1.0.0"

# Storage backend types.
StorageBackend = Literal["local", "s3", "gcs", "azure"]


class DocumentUploadMessage(BaseModel):
    """Schema for document upload notification from core.document.uploaded.v1 topic.

    This schema matches the DocumentUploadNotification interface from intg-svc.
    Handles Avro wrapped union types for optional fields.
    """

    event_type: Literal["document.uploaded"] = Field(..., description="Event type identifier")
    document_id: str = Field(..., description="Unique document identifier")
    workspace_id: str = Field(..., description="Workspace identifier")
    user_id: str = Field(..., description="User identifier who uploaded the document")
    filename: str = Field(..., description="Storage filename")
    original_filename: str = Field(..., description="Original filename from upload")
    content_type: str = Field(..., description="MIME type of the document")
    size_bytes: int = Field(..., gt=0, description="File size in bytes")
    storage_backend: StorageBackend = Field(
        ..., description="Storage backend used (local, s3, gcs, azure)"
    )
    storage_path: str = Field(..., description="Path to file in storage")
    storage_bucket: str | None = Field(None, description="Storage bucket name (if applicable)")
    storage_url: str | None = Field(
        None, description="Public or signed URL to the file (if available)"
    )
    timestamp: str = Field(..., description="ISO 8601 timestamp of the upload event")
    contract_version: str = Field(
        CONTRACT_VERSION,
        description="Semantic version of the upload-event contract. Defaults so "
        "older messages produced without it still validate.",
    )

    # Ingestion source labeling (inherent-systems/prime#187). intg-svc's
    # uploadDocument() derives one of "connector:<provider>" | "public-api" |
    # "manual" for every upload. Optional + defaulted to None so in-flight/
    # legacy messages produced before this field existed still validate
    # (backward compat) — consumers treat a missing source as "unknown".
    #
    # max_length=500 (#141 adversarial pass): these are meant to be short IDs
    # (UUIDs, provider-prefixed labels) that the ingestion-svc trigger attaches
    # to a Temporal workflow memo. A pathologically oversized value here must
    # fail LOUDLY as a validation error — which trigger.py's existing poison
    # path (PydanticValidationError -> dead-letter, see #6) already handles —
    # rather than being silently truncated into a plausible-looking but wrong
    # value downstream (a truncated connection_id looks like a real one; a
    # rejected message does not).
    source: str | None = Field(
        None,
        max_length=500,
        description="Ingestion source: connector:<provider> | public-api | manual. "
        "Absent on messages produced before this field existed.",
    )
    connection_id: str | None = Field(
        None,
        max_length=500,
        description="Connector connection identifier (connector-sourced uploads only)",
    )
    sync_id: str | None = Field(
        None,
        max_length=500,
        description="Connector sync run identifier (connector-sourced uploads only)",
    )

    @field_validator(
        "storage_bucket", "storage_url", "source", "connection_id", "sync_id", mode="before"
    )
    @classmethod
    def unwrap_avro_union(cls, v: None | str | dict) -> str | None:
        """Unwrap Avro union type format.

        Avro JSON encoding wraps union values:
        - null -> null
        - string -> {"string": "value"}

        This validator handles both formats.
        """
        if v is None:
            return None
        if isinstance(v, str):
            return v
        if isinstance(v, dict) and "string" in v:
            value = v["string"]
            return str(value) if isinstance(value, str) else None
        return None

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "event_type": "document.uploaded",
                "document_id": "507f1f77bcf86cd799439011",
                "workspace_id": "507f1f77bcf86cd799439012",
                "user_id": "507f1f77bcf86cd799439013",
                "filename": "1234567890-abc12345-document.pdf",
                "original_filename": "document.pdf",
                "content_type": "application/pdf",
                "size_bytes": 102400,
                "storage_backend": "gcs",
                "storage_path": "workspaces/507f1f77bcf86cd799439012/1234567890-abc12345-document.pdf",
                "storage_bucket": "documents",
                "storage_url": "https://storage.googleapis.com/documents/workspaces/...",
                "timestamp": "2024-01-15T10:30:00Z",
                "source": "connector:notion",
                "connection_id": "conn_123",
                "sync_id": "sync_456",
            }
        }
    )


class DocumentCompletionMessage(BaseModel):
    """Schema for document processing completion notification.

    Published to the completion topic after ingestion succeeds or fails,
    so that intg-svc can update MongoDB document status accordingly.
    """

    event_type: Literal["document.processed", "document.failed"] = Field(
        ..., description="Event type identifier"
    )
    document_id: str = Field(..., description="Document identifier (MongoDB ObjectId string)")
    workspace_id: str = Field(..., description="Workspace identifier")
    user_id: str = Field(..., description="User who uploaded the document")
    original_filename: str = Field(..., description="Original filename from upload")
    success: bool = Field(..., description="Whether processing succeeded")
    status: Literal["ready", "failed"] = Field(
        ..., description="Target status for intg-svc (ready=processed, failed=error)"
    )
    chunks_created: int = Field(0, description="Number of chunks created")
    processing_time_ms: int = Field(0, description="Total processing time in milliseconds")
    error: str | None = Field(None, description="Error message if processing failed")
    timestamp: str = Field(..., description="ISO 8601 timestamp of completion")

    # Storage metadata (optional for backward compatibility)
    content_type: str | None = Field(None, description="MIME type of the document")
    size_bytes: int | None = Field(None, description="File size in bytes")
    storage_backend: str | None = Field(None, description="Storage backend (s3, local, etc)")
    storage_path: str | None = Field(None, description="Path in storage")
    storage_bucket: str | None = Field(None, description="Storage bucket name")
    storage_url: str | None = Field(None, description="Full storage URL")

    contract_version: str = Field(
        CONTRACT_VERSION,
        description="Semantic version of the completion-event contract. Defaults "
        "so older messages produced without it still validate.",
    )
