"""Reindex a document's ALREADY-STORED Postgres chunks into Weaviate (#221).

Why this exists instead of reusing ``refresh_stale_source`` /
``POST /v1/documents/{id}/refresh``
---------------------------------------------------------------------------
Those two (REST + MCP, both in inh-public-api-svc) republish the ORIGINAL
``document.uploaded`` event, re-running the FULL Temporal pipeline: fetch the
source bytes from ``storage_path`` -> extract -> chunk -> store. That is the
right tool when the stored file is still reachable and re-deriving chunks
from it is safe.

It is the WRONG tool for a document whose ``document_chunks`` rows carry a
NULL ``content_hash``/``source_uri`` and a suspiciously fixed ``ingested_at``
(the #221 signature) — evidence the row was written directly into Postgres,
bypassing ``store_processed_document``, with no guarantee ``storage_path``
still points at bytes that produce the SAME chunks currently on file (or at
any bytes at all). Re-running fetch+extract+chunk against an unverified path
risks a 404 at best, or silently re-deriving different chunks under the same
document_id at worst — while the issue explicitly says the CURRENT Postgres
chunks already return valid text via ``GET /v1/chunks/{id}``.

This module does the narrower, safer thing: it changes nothing about
Postgres, extraction, or chunking. It reads the chunks exactly as they are
today and embeds them into Weaviate via ``WeaviateService.store_chunks_with_tenant``
— the SAME primitive ``store_in_weaviate``
(``src/temporal/activities/store.py``) calls in the normal pipeline — so a
document reindexed this way lands in Weaviate identically to one the
pipeline just processed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog

from src.models.document import DocumentChunk

if TYPE_CHECKING:
    from src.services.database import DatabaseService
    from src.services.weaviate import WeaviateService

logger = structlog.get_logger(__name__)

# get_document_chunks paginates (default limit=100); page through it so a
# document with more chunks than one page doesn't silently reindex only the
# first 100.
_CHUNK_PAGE_SIZE = 200


@dataclass
class ReindexResult:
    """Outcome of reindexing one document from its stored Postgres chunks."""

    document_id: str
    chunks_embedded: int
    skipped: bool = False
    reason: str | None = None


async def _load_all_chunks(database: DatabaseService, document_id: str) -> list[dict]:
    """Page through ``document_chunks`` for ``document_id`` (no page-size cap)."""
    rows: list[dict] = []
    offset = 0
    while True:
        page = await database.get_document_chunks(
            document_id, limit=_CHUNK_PAGE_SIZE, offset=offset
        )
        if not page:
            break
        rows.extend(page)
        if len(page) < _CHUNK_PAGE_SIZE:
            break
        offset += _CHUNK_PAGE_SIZE
    return rows


async def reindex_document_from_postgres(
    *,
    database: DatabaseService,
    weaviate: WeaviateService,
    document_id: str,
) -> ReindexResult:
    """Embed ``document_id``'s existing Postgres chunks into Weaviate.

    Does not touch ``processed_documents``/``document_chunks`` and does not
    re-fetch, re-extract, or re-chunk anything — see module docstring for why.

    Idempotent: any partial/stale vectors for this document are cleared first
    (mirroring the idempotent-reindex step in ``store_in_weaviate``), so
    retrying a failed run never leaves duplicate objects.

    Returns a ``ReindexResult`` with ``skipped=True`` (never raises) when the
    document or its chunks are missing, so an operator script can report a
    clear reason instead of a stack trace for an already-fixed or
    never-actually-orphaned document.
    """
    doc = await database.get_document_status(document_id)
    if not doc:
        return ReindexResult(
            document_id=document_id,
            chunks_embedded=0,
            skipped=True,
            reason="document not found in processed_documents",
        )

    rows = await _load_all_chunks(database, document_id)
    if not rows:
        return ReindexResult(
            document_id=document_id,
            chunks_embedded=0,
            skipped=True,
            reason="no chunks stored in document_chunks — nothing to embed",
        )

    workspace_id = doc["workspace_id"]
    user_id = doc["user_id"]

    chunks = [
        DocumentChunk(
            document_id=document_id,
            content=row["content"],
            chunk_index=row["chunk_index"],
            start_char=row.get("start_char") or 0,
            end_char=row.get("end_char") or 0,
            token_count=row.get("token_count"),
            metadata=row.get("metadata"),
        )
        for row in sorted(rows, key=lambda r: r["chunk_index"])
    ]

    deleted_ok, deleted_count = await weaviate.delete_document_chunks_graceful(
        workspace_id=workspace_id, document_id=document_id, user_id=user_id
    )
    if not deleted_ok:
        logger.warning(
            "Could not clear existing Weaviate chunks before reindex (non-fatal, "
            "proceeding — store_chunks_with_tenant will add alongside any survivors)",
            document_id=document_id,
            workspace_id=workspace_id,
        )

    # Provenance (#41): same fallback order store_in_weaviate uses.
    source_uri = doc.get("storage_path") or doc.get("storage_url")
    # Source link (inherent#391): no fallback -- unlike source_uri, there is
    # no internal substitute for a connector-supplied original-file link.
    source_url = doc.get("source_url")

    stored_count = await weaviate.store_chunks_with_tenant(
        chunks=chunks,
        document_id=document_id,
        workspace_id=workspace_id,
        user_id=user_id,
        original_filename=doc.get("original_filename") or doc.get("filename") or document_id,
        content_type=doc.get("content_type") or "application/octet-stream",
        source_uri=source_uri,
        source_url=source_url,
    )

    logger.info(
        "Reindexed document from existing Postgres chunks",
        document_id=document_id,
        workspace_id=workspace_id,
        chunks_embedded=stored_count,
        vectors_cleared_first=deleted_count,
    )
    return ReindexResult(document_id=document_id, chunks_embedded=stored_count)
