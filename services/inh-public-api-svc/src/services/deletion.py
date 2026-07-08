"""Document deletion orchestration — the one owner of delete semantics (#87).

Both surfaces (REST ``DELETE /v1/documents/{id}`` and the MCP
``delete_document`` tool) delete through this module so their behavior can
never drift. Deletion spans three stores, in a deliberate order:

1. **Weaviate vectors** (tenant-scoped batch delete) — FIRST, and a failure
   aborts everything. Search reads Weaviate directly, so vectors that outlive
   the database row would keep surfacing "deleted" content; failing early
   leaves a still-visible document and a retryable operation instead.
2. **PostgreSQL row + chunks** — transactional, workspace-scoped.
3. **S3 object bytes** — best-effort; a failure is logged but the delete has
   already succeeded from the caller's perspective (the bytes are unreachable
   through the API once 1–2 are done).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from src.services.search import get_search_service
from src.services.storage import get_storage_service
from src.utils import get_logger

if TYPE_CHECKING:
    from src.services.database import DatabaseService

logger = get_logger(__name__)


@dataclass
class DeletionOutcome:
    """What a delete actually removed, for reporting on both surfaces."""

    found: bool
    vectors_deleted: int = 0
    chunks_deleted: int = 0
    storage_deleted: bool = False


async def delete_document_everywhere(
    database: DatabaseService,
    document_id: str,
    workspace_id: str,
) -> DeletionOutcome:
    """Delete a document from Weaviate, PostgreSQL, and S3 (#87).

    The lookup is keyed on ``(document_id, workspace_id)``: a document in a
    workspace the caller can't see reads as not-found (``found=False``), so
    tenant isolation holds and existence never leaks across workspaces.

    Raises whatever the vector-store cleanup raises — the caller maps that to
    a retryable error (REST 503 / MCP error text). The database row is only
    deleted after vectors are gone (see module docstring for why).
    """
    fields = await database.get_document_upload_fields(document_id, workspace_id)
    if not fields:
        return DeletionOutcome(found=False)

    # 1. Vectors first. Tenant = the STORED row's uploader (where ingestion
    # wrote the objects), not the caller — access was already authorized.
    search_service = await get_search_service()
    vectors_deleted = await search_service.delete_document_vectors(
        workspace_id, fields["user_id"], document_id
    )

    # 2. Database row + chunks (transactional). None here means a concurrent
    # delete won the race — report not-found rather than half-success.
    deleted = await database.delete_document(document_id, workspace_id)
    if deleted is None:
        return DeletionOutcome(found=False, vectors_deleted=vectors_deleted)

    # 3. Object storage, best-effort. Only S3-backed documents have bytes the
    # storage service can reach.
    storage_deleted = False
    storage_path = fields.get("storage_path")
    if fields.get("storage_backend") == "s3" and storage_path:
        try:
            storage = get_storage_service()
            await storage.delete_file(storage_path)
            storage_deleted = True
        except Exception as exc:
            logger.warning(
                "Failed to delete stored object (best-effort; document already deleted)",
                document_id=document_id,
                storage_path=storage_path,
                error=str(exc),
            )

    logger.info(
        "Document deleted",
        document_id=document_id,
        workspace_id=workspace_id,
        vectors_deleted=vectors_deleted,
        chunks_deleted=deleted["chunk_count"],
        storage_deleted=storage_deleted,
    )
    return DeletionOutcome(
        found=True,
        vectors_deleted=vectors_deleted,
        chunks_deleted=deleted["chunk_count"],
        storage_deleted=storage_deleted,
    )
