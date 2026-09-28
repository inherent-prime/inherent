"""DatabaseService reuse-bookkeeping tests (inherent#394).

Exercises get_chunk_for_reuse / record_chunk_reuse against a real Postgres
(migration 024's reuse_count/last_reused_at columns + chunk_reuse_events
table) -- the idempotency (dedupe key) and atomic-increment behaviour these
rely on are exactly what a mocked session would hide.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from src.models.document import DocumentChunk, DocumentUploadMessage
from src.services.database import DatabaseService


async def _store_one_chunk_document(
    db_service: DatabaseService,
    *,
    document_id: str,
    content: str,
) -> int:
    """Store a minimal one-chunk document and return its document_chunks.id."""
    message = DocumentUploadMessage(
        event_type="document.uploaded",
        document_id=document_id,
        workspace_id="test_workspace_reuse",
        user_id="test_user_reuse",
        filename=f"{document_id}.txt",
        original_filename=f"{document_id}.txt",
        content_type="text/plain",
        size_bytes=len(content),
        storage_backend="local",
        storage_path=f"workspaces/test_workspace_reuse/{document_id}.txt",
        storage_bucket=None,
        storage_url=None,
        timestamp=datetime.now(UTC).isoformat(),
    )
    chunk = DocumentChunk(
        document_id=document_id, content=content, chunk_index=0, start_char=0, end_char=len(content)
    )
    doc_id = await db_service.store_processed_document(
        message=message,
        chunks=[chunk],
        text_length=len(content),
        processing_time_ms=1,
        workflow_run_id=f"run_{document_id}",
    )
    assert doc_id is not None
    chunks = await db_service.get_document_chunks(document_id)
    assert len(chunks) == 1
    return int(chunks[0]["id"])


@pytest.mark.asyncio
async def test_get_chunk_for_reuse_returns_id_and_reuse_count(db_service: DatabaseService):
    chunk_id = await _store_one_chunk_document(
        db_service, document_id="test_reuse_source_1", content="shared boilerplate text"
    )

    found = await db_service.get_chunk_for_reuse(document_id="test_reuse_source_1", chunk_index=0)

    assert found is not None
    assert found["id"] == chunk_id
    assert found["reuse_count"] == 0  # fresh chunk, never reused


@pytest.mark.asyncio
async def test_get_chunk_for_reuse_returns_none_for_unknown_chunk(db_service: DatabaseService):
    found = await db_service.get_chunk_for_reuse(document_id="test_reuse_missing", chunk_index=0)
    assert found is None


@pytest.mark.asyncio
async def test_record_chunk_reuse_increments_and_stamps(db_service: DatabaseService):
    source_id = await _store_one_chunk_document(
        db_service, document_id="test_reuse_source_2", content="shared boilerplate text 2"
    )

    new_count = await db_service.record_chunk_reuse(
        source_chunk_id=source_id,
        reusing_document_id="test_reuse_new_doc",
        reusing_chunk_content_hash="a" * 64,
        workspace_id="test_workspace_reuse",
        similarity=0.95,
    )

    assert new_count == 1
    chunks = await db_service.get_document_chunks("test_reuse_source_2")
    assert chunks[0]["reuse_count"] == 1
    assert chunks[0]["last_reused_at"] is not None


@pytest.mark.asyncio
async def test_record_chunk_reuse_is_idempotent_for_same_triple(db_service: DatabaseService):
    """Re-ingesting the SAME unchanged document (same content_hash) must not
    double-count -- the exact idempotency requirement in #394."""
    source_id = await _store_one_chunk_document(
        db_service, document_id="test_reuse_source_3", content="shared boilerplate text 3"
    )

    first = await db_service.record_chunk_reuse(
        source_chunk_id=source_id,
        reusing_document_id="test_reuse_new_doc_3",
        reusing_chunk_content_hash="b" * 64,
        workspace_id="test_workspace_reuse",
        similarity=0.95,
    )
    second = await db_service.record_chunk_reuse(
        source_chunk_id=source_id,
        reusing_document_id="test_reuse_new_doc_3",
        reusing_chunk_content_hash="b" * 64,  # SAME hash -- unchanged re-ingest
        workspace_id="test_workspace_reuse",
        similarity=0.95,
    )

    assert first == 1
    assert second is None  # idempotent no-op, not a second increment

    chunks = await db_service.get_document_chunks("test_reuse_source_3")
    assert chunks[0]["reuse_count"] == 1  # NOT double-counted


@pytest.mark.asyncio
async def test_record_chunk_reuse_counts_again_on_new_content_hash(db_service: DatabaseService):
    """A genuinely edited reusing chunk (new content_hash) is free to be
    recorded again -- distinct from the unchanged-reingest case above."""
    source_id = await _store_one_chunk_document(
        db_service, document_id="test_reuse_source_4", content="shared boilerplate text 4"
    )

    first = await db_service.record_chunk_reuse(
        source_chunk_id=source_id,
        reusing_document_id="test_reuse_new_doc_4",
        reusing_chunk_content_hash="c" * 64,
        workspace_id="test_workspace_reuse",
        similarity=0.95,
    )
    second = await db_service.record_chunk_reuse(
        source_chunk_id=source_id,
        reusing_document_id="test_reuse_new_doc_4",
        reusing_chunk_content_hash="d" * 64,  # DIFFERENT hash -- content changed
        workspace_id="test_workspace_reuse",
        similarity=0.95,
    )

    assert first == 1
    assert second == 2


@pytest.mark.asyncio
async def test_record_chunk_reuse_counts_separately_per_reusing_document(
    db_service: DatabaseService,
):
    """Two DIFFERENT documents reusing the same source chunk must both count,
    even if (by coincidence) their content hashes were to collide -- the
    dedupe key includes reusing_document_id precisely for this."""
    source_id = await _store_one_chunk_document(
        db_service, document_id="test_reuse_source_5", content="shared boilerplate text 5"
    )

    first = await db_service.record_chunk_reuse(
        source_chunk_id=source_id,
        reusing_document_id="test_reuse_doc_a",
        reusing_chunk_content_hash="e" * 64,
        workspace_id="test_workspace_reuse",
        similarity=0.95,
    )
    second = await db_service.record_chunk_reuse(
        source_chunk_id=source_id,
        reusing_document_id="test_reuse_doc_b",
        reusing_chunk_content_hash="e" * 64,  # same hash, DIFFERENT document
        workspace_id="test_workspace_reuse",
        similarity=0.95,
    )

    assert first == 1
    assert second == 2
