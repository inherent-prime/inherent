"""Single-chunk write orchestration for public-api (#133).

Both REST (Sprint 2) and MCP (Sprint 3) must call these helpers so Create /
Update / Delete never drift across surfaces. Dual-store order:

* **Create / Update** — PostgreSQL first, then Weaviate (with vector). Vector
  failure compensates PG (delete row / restore prior content).
* **Delete** — Weaviate first, then PostgreSQL (same rationale as
  ``delete_document_everywhere``: orphan vectors must not survive a delete).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from src.models.document import DocumentChunk
from src.services.compensation import (
    delete_chunk_with_retry,
    restore_chunk_content_with_retry,
)
from src.services.search import get_search_service
from src.utils import get_logger

if TYPE_CHECKING:
    from src.services.database import DatabaseService

logger = get_logger(__name__)


@dataclass
class ChunkWriteOutcome:
    """Result of Create / Update — ``found=False`` means not-found (404)."""

    found: bool
    chunk: DocumentChunk | None = None


@dataclass
class ChunkDeleteOutcome:
    """Result of Delete — ``found=False`` means not-found (404)."""

    found: bool


async def create_chunk_everywhere(
    database: DatabaseService,
    document_id: str,
    workspace_id: str,
    content: str,
) -> ChunkWriteOutcome:
    """Append a chunk in PG then upsert its vector (#133 Option A).

    On vector failure, compensates by deleting the new PG row (retry + loud
    exhaustion). Re-raises the vector error so the caller returns 503.
    """
    fields = await database.get_document_upload_fields(document_id, workspace_id)
    if not fields:
        return ChunkWriteOutcome(found=False)

    chunk = await database.append_document_chunk(document_id, workspace_id, content)
    if chunk is None:
        # Race: document vanished between lookup and append.
        return ChunkWriteOutcome(found=False)

    content_hash = (chunk.metadata or {}).get("content_hash") or ""
    search = await get_search_service()
    try:
        await search.upsert_chunk_vector(
            workspace_id=workspace_id,
            user_id=fields["user_id"],
            document_id=document_id,
            chunk_index=chunk.chunk_index,
            content=content,
            content_hash=content_hash,
            original_filename=fields.get("original_filename") or fields.get("filename"),
            content_type=fields.get("content_type"),
            source_uri=fields.get("storage_path") or fields.get("storage_url"),
            create=True,
        )
    except Exception:
        await delete_chunk_with_retry(
            database,
            document_id,
            workspace_id,
            chunk.chunk_index,
            operation="chunk_create_vector_rollback",
        )
        raise

    logger.info(
        "Chunk created",
        document_id=document_id,
        workspace_id=workspace_id,
        chunk_index=chunk.chunk_index,
    )
    return ChunkWriteOutcome(found=True, chunk=chunk)


async def update_chunk_everywhere(
    database: DatabaseService,
    document_id: str,
    workspace_id: str,
    chunk_index: int,
    content: str,
) -> ChunkWriteOutcome:
    """Update PG content then re-embed in Weaviate (#133).

    On vector failure, restores prior PG content via compensated retry, then
    re-raises so the caller returns 503.
    """
    fields = await database.get_document_upload_fields(document_id, workspace_id)
    if not fields:
        return ChunkWriteOutcome(found=False)

    prior = await database.get_document_chunk_by_index(document_id, workspace_id, chunk_index)
    if prior is None:
        return ChunkWriteOutcome(found=False)

    updated = await database.update_document_chunk(document_id, workspace_id, chunk_index, content)
    if updated is None:
        return ChunkWriteOutcome(found=False)

    content_hash = (updated.metadata or {}).get("content_hash") or ""
    search = await get_search_service()
    try:
        await search.upsert_chunk_vector(
            workspace_id=workspace_id,
            user_id=fields["user_id"],
            document_id=document_id,
            chunk_index=chunk_index,
            content=content,
            content_hash=content_hash,
            create=False,
        )
    except Exception:
        await restore_chunk_content_with_retry(
            database,
            document_id,
            workspace_id,
            chunk_index,
            prior.content,
            operation="chunk_update_vector_rollback",
        )
        raise

    logger.info(
        "Chunk updated",
        document_id=document_id,
        workspace_id=workspace_id,
        chunk_index=chunk_index,
    )
    return ChunkWriteOutcome(found=True, chunk=updated)


async def delete_chunk_everywhere(
    database: DatabaseService,
    document_id: str,
    workspace_id: str,
    chunk_index: int,
) -> ChunkDeleteOutcome:
    """Delete one chunk vector first, then the PG row (#133).

    Vector failure aborts before PG delete (retryable; row still visible).
    """
    fields = await database.get_document_upload_fields(document_id, workspace_id)
    if not fields:
        return ChunkDeleteOutcome(found=False)

    existing = await database.get_document_chunk_by_index(document_id, workspace_id, chunk_index)
    if existing is None:
        return ChunkDeleteOutcome(found=False)

    search = await get_search_service()
    await search.delete_chunk_vector(workspace_id, fields["user_id"], document_id, chunk_index)

    deleted = await database.delete_document_chunk(document_id, workspace_id, chunk_index)
    if deleted is None:
        # Concurrent delete won — report not-found.
        return ChunkDeleteOutcome(found=False)

    logger.info(
        "Chunk deleted",
        document_id=document_id,
        workspace_id=workspace_id,
        chunk_index=chunk_index,
    )
    return ChunkDeleteOutcome(found=True)
