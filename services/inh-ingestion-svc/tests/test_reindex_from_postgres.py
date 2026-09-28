"""Unit tests for reindex_document_from_postgres (#221).

Reindexes a document straight from its ALREADY-STORED document_chunks rows,
bypassing fetch/extract/chunk entirely — see the module docstring in
src/services/reindex_from_postgres.py for why refresh_stale_source's
re-fetch-the-source path does not fit a document with no verifiable
storage_path (the #221 backfill signature).

Offline: DatabaseService and WeaviateService are both mocked, following the
override pattern in tests/test_weaviate_delete.py (this file doesn't need a
live Postgres/Weaviate, so it overrides the autouse conftest fixtures that
otherwise assume one).
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src.services.reindex_from_postgres import reindex_document_from_postgres

# ---------------------------------------------------------------------------
# Override conftest's autouse DB fixtures -- this module is fully mocked.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
async def cleanup_test_data():
    yield


@pytest.fixture()
def db_service():
    yield None


DOC_STATUS_ROW = {
    "document_id": "doc-orphaned",
    "workspace_id": "ws-1",
    "user_id": "user-1",
    "filename": "stored.pdf",
    "original_filename": "report.pdf",
    "content_type": "application/pdf",
    "storage_backend": "s3",
    "storage_path": "ws-1/doc-orphaned/stored.pdf",
    "storage_url": None,
    "status": "processed",
    "chunk_count": 2,
}


def _chunk_row(chunk_index: int, content: str = "chunk text") -> dict:
    return {
        "content": content,
        "chunk_index": chunk_index,
        "start_char": 0,
        "end_char": len(content),
        "token_count": 5,
        "metadata": None,
    }


def _mock_database(*, doc: dict | None, chunk_pages: list[list[dict]]) -> AsyncMock:
    """chunk_pages: successive return values for paginated get_document_chunks calls."""
    db = AsyncMock()
    db.get_document_status = AsyncMock(return_value=doc)
    db.get_document_chunks = AsyncMock(side_effect=list(chunk_pages) + [[]])
    return db


def _mock_weaviate(*, delete_ok: bool = True, deleted_count: int = 0) -> AsyncMock:
    wv = AsyncMock()
    wv.delete_document_chunks_graceful = AsyncMock(return_value=(delete_ok, deleted_count))
    wv.store_chunks_with_tenant = AsyncMock(side_effect=lambda chunks, **_: len(chunks))
    return wv


class TestReindexDocumentFromPostgres:
    async def test_embeds_existing_chunks_without_touching_extraction(self):
        db = _mock_database(doc=DOC_STATUS_ROW, chunk_pages=[[_chunk_row(0), _chunk_row(1)]])
        weaviate = _mock_weaviate()

        result = await reindex_document_from_postgres(
            database=db, weaviate=weaviate, document_id="doc-orphaned"
        )

        assert result.skipped is False
        assert result.chunks_embedded == 2
        weaviate.store_chunks_with_tenant.assert_awaited_once()
        _, kwargs = weaviate.store_chunks_with_tenant.await_args
        assert kwargs["document_id"] == "doc-orphaned"
        assert kwargs["workspace_id"] == "ws-1"
        assert kwargs["user_id"] == "user-1"
        assert len(kwargs["chunks"]) == 2
        assert [c.chunk_index for c in kwargs["chunks"]] == [0, 1]
        # source_uri falls back to storage_path exactly like store_in_weaviate.
        assert kwargs["source_uri"] == "ws-1/doc-orphaned/stored.pdf"
        # No source_url on the doc row -> None, not a fallback (inherent#391:
        # unlike source_uri there is no internal substitute for it).
        assert kwargs["source_url"] is None

    async def test_reindex_carries_document_row_source_url(self):
        """A stored source_url (inherent#391) rides along on reindex, straight
        from the processed_documents row -- no fallback, unlike source_uri."""
        doc_with_source_url = {**DOC_STATUS_ROW, "source_url": "https://drive.google.com/x"}
        db = _mock_database(doc=doc_with_source_url, chunk_pages=[[_chunk_row(0)]])
        weaviate = _mock_weaviate()

        await reindex_document_from_postgres(
            database=db, weaviate=weaviate, document_id="doc-orphaned"
        )

        _, kwargs = weaviate.store_chunks_with_tenant.await_args
        assert kwargs["source_url"] == "https://drive.google.com/x"

    async def test_clears_any_stale_vectors_before_writing_new_ones(self):
        db = _mock_database(doc=DOC_STATUS_ROW, chunk_pages=[[_chunk_row(0)]])
        weaviate = _mock_weaviate(delete_ok=True, deleted_count=0)

        await reindex_document_from_postgres(
            database=db, weaviate=weaviate, document_id="doc-orphaned"
        )

        weaviate.delete_document_chunks_graceful.assert_awaited_once_with(
            workspace_id="ws-1", document_id="doc-orphaned", user_id="user-1"
        )
        # Delete must happen before the write (idempotent reindex ordering).
        assert weaviate.method_calls[0][0] == "delete_document_chunks_graceful"

    async def test_paginates_beyond_the_first_chunk_page(self):
        # Two full pages (size 200 in the module) plus a short final page --
        # simulate via a small monkeypatched page size by returning fewer
        # rows than the page size on the second call so pagination stops.
        first_page = [_chunk_row(i) for i in range(200)]
        second_page = [_chunk_row(200)]
        db = _mock_database(doc=DOC_STATUS_ROW, chunk_pages=[first_page, second_page])
        weaviate = _mock_weaviate()

        result = await reindex_document_from_postgres(
            database=db, weaviate=weaviate, document_id="doc-orphaned"
        )

        assert result.chunks_embedded == 201
        assert db.get_document_chunks.await_count == 2

    async def test_missing_document_is_skipped_not_raised(self):
        db = _mock_database(doc=None, chunk_pages=[])
        weaviate = _mock_weaviate()

        result = await reindex_document_from_postgres(
            database=db, weaviate=weaviate, document_id="doc-missing"
        )

        assert result.skipped is True
        assert "not found" in result.reason
        weaviate.store_chunks_with_tenant.assert_not_awaited()

    async def test_document_with_no_chunks_is_skipped_not_raised(self):
        db = _mock_database(doc=DOC_STATUS_ROW, chunk_pages=[[]])
        weaviate = _mock_weaviate()

        result = await reindex_document_from_postgres(
            database=db, weaviate=weaviate, document_id="doc-orphaned"
        )

        assert result.skipped is True
        assert "no chunks" in result.reason
        weaviate.store_chunks_with_tenant.assert_not_awaited()

    async def test_falls_back_to_storage_url_when_storage_path_missing(self):
        doc = dict(DOC_STATUS_ROW, storage_path=None, storage_url="https://example/report.pdf")
        db = _mock_database(doc=doc, chunk_pages=[[_chunk_row(0)]])
        weaviate = _mock_weaviate()

        await reindex_document_from_postgres(
            database=db, weaviate=weaviate, document_id="doc-orphaned"
        )

        _, kwargs = weaviate.store_chunks_with_tenant.await_args
        assert kwargs["source_uri"] == "https://example/report.pdf"
