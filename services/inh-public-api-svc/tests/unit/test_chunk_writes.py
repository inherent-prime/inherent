"""Unit tests for chunk write orchestration (#133 Sprint 2)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from src.models.document import DocumentChunk
from src.services.chunk_writes import (
    create_chunk_everywhere,
    delete_chunk_everywhere,
    update_chunk_everywhere,
)

pytestmark = pytest.mark.asyncio

WS = "ws-1"
DOC = "doc-1"


def _chunk(index: int = 0, content: str = "text") -> DocumentChunk:
    return DocumentChunk(
        id="1",
        document_id=DOC,
        content=content,
        chunk_index=index,
        token_count=1,
        metadata={"content_hash": "h"},
    )


class TestCreateChunkEverywhere:
    async def test_pg_then_vector_happy_path(self):
        db = AsyncMock()
        db.get_document_upload_fields = AsyncMock(
            return_value={
                "user_id": "u1",
                "original_filename": "f.pdf",
                "filename": "f.pdf",
                "content_type": "application/pdf",
                "storage_path": "s3://b/f",
            }
        )
        db.append_document_chunk = AsyncMock(return_value=_chunk(3, "new"))
        search = AsyncMock()
        search.upsert_chunk_vector = AsyncMock()

        with patch("src.services.chunk_writes.get_search_service", AsyncMock(return_value=search)):
            outcome = await create_chunk_everywhere(db, DOC, WS, "new")

        assert outcome.found is True
        assert outcome.chunk is not None
        assert outcome.chunk.chunk_index == 3
        db.append_document_chunk.assert_awaited_once()
        search.upsert_chunk_vector.assert_awaited_once()
        assert search.upsert_chunk_vector.await_args.kwargs["create"] is True
        assert search.upsert_chunk_vector.await_args.kwargs["chunk_index"] == 3

    async def test_missing_document_is_not_found(self):
        db = AsyncMock()
        db.get_document_upload_fields = AsyncMock(return_value=None)

        outcome = await create_chunk_everywhere(db, DOC, WS, "x")
        assert outcome.found is False
        assert outcome.chunk is None
        db.append_document_chunk.assert_not_awaited()

    async def test_vector_fail_compensates_pg_row(self):
        db = AsyncMock()
        db.get_document_upload_fields = AsyncMock(
            return_value={
                "user_id": "u1",
                "original_filename": "f.pdf",
                "filename": "f.pdf",
                "content_type": "application/pdf",
                "storage_path": None,
            }
        )
        db.append_document_chunk = AsyncMock(return_value=_chunk(4, "new"))
        db.delete_document_chunk = AsyncMock(return_value=_chunk(4, "new"))
        search = AsyncMock()
        search.upsert_chunk_vector = AsyncMock(side_effect=RuntimeError("weaviate down"))

        with (
            patch("src.services.chunk_writes.get_search_service", AsyncMock(return_value=search)),
            pytest.raises(RuntimeError, match="weaviate"),
        ):
            await create_chunk_everywhere(db, DOC, WS, "new")

        db.delete_document_chunk.assert_awaited_once_with(DOC, WS, 4)


class TestUpdateChunkEverywhere:
    async def test_pg_then_reembed(self):
        db = AsyncMock()
        db.get_document_upload_fields = AsyncMock(return_value={"user_id": "u1"})
        prior = _chunk(1, "old")
        updated = _chunk(1, "new")
        updated.metadata = {"content_hash": "newhash"}
        db.get_document_chunk_by_index = AsyncMock(return_value=prior)
        db.update_document_chunk = AsyncMock(return_value=updated)
        search = AsyncMock()
        search.upsert_chunk_vector = AsyncMock()

        with patch("src.services.chunk_writes.get_search_service", AsyncMock(return_value=search)):
            outcome = await update_chunk_everywhere(db, DOC, WS, 1, "new")

        assert outcome.found is True
        assert outcome.chunk.content == "new"
        search.upsert_chunk_vector.assert_awaited_once()
        assert search.upsert_chunk_vector.await_args.kwargs["create"] is False

    async def test_vector_fail_restores_prior_content(self):
        db = AsyncMock()
        db.get_document_upload_fields = AsyncMock(return_value={"user_id": "u1"})
        prior = _chunk(1, "old")
        updated = _chunk(1, "new")
        updated.metadata = {"content_hash": "nh"}
        db.get_document_chunk_by_index = AsyncMock(return_value=prior)
        db.update_document_chunk = AsyncMock(side_effect=[updated, prior])
        search = AsyncMock()
        search.upsert_chunk_vector = AsyncMock(side_effect=RuntimeError("embed fail"))

        with (
            patch("src.services.chunk_writes.get_search_service", AsyncMock(return_value=search)),
            pytest.raises(RuntimeError, match="embed"),
        ):
            await update_chunk_everywhere(db, DOC, WS, 1, "new")

        # Second update_document_chunk call restores prior content, CAS'd on
        # the hash this request wrote so a concurrent winner is not clobbered.
        assert db.update_document_chunk.await_count == 2
        restore_call = db.update_document_chunk.await_args_list[1]
        assert restore_call.args[3] == "old"
        assert restore_call.kwargs.get("only_if_content_hash") == "nh"


class TestDeleteChunkEverywhere:
    async def test_vector_first_then_pg(self):
        db = AsyncMock()
        db.get_document_upload_fields = AsyncMock(return_value={"user_id": "u1"})
        db.get_document_chunk_by_index = AsyncMock(return_value=_chunk(2))
        db.delete_document_chunk = AsyncMock(return_value=_chunk(2))
        search = AsyncMock()
        search.delete_chunk_vector = AsyncMock()
        order: list[str] = []

        async def _vec(*a, **k):
            order.append("vector")

        async def _pg(*a, **k):
            order.append("pg")
            return _chunk(2)

        search.delete_chunk_vector = AsyncMock(side_effect=_vec)
        db.delete_document_chunk = AsyncMock(side_effect=_pg)

        with patch("src.services.chunk_writes.get_search_service", AsyncMock(return_value=search)):
            outcome = await delete_chunk_everywhere(db, DOC, WS, 2)

        assert outcome.found is True
        assert order == ["vector", "pg"]

    async def test_vector_fail_leaves_pg_intact(self):
        db = AsyncMock()
        db.get_document_upload_fields = AsyncMock(return_value={"user_id": "u1"})
        db.get_document_chunk_by_index = AsyncMock(return_value=_chunk(2))
        search = AsyncMock()
        search.delete_chunk_vector = AsyncMock(side_effect=RuntimeError("weaviate down"))

        with (
            patch("src.services.chunk_writes.get_search_service", AsyncMock(return_value=search)),
            pytest.raises(RuntimeError),
        ):
            await delete_chunk_everywhere(db, DOC, WS, 2)

        db.delete_document_chunk.assert_not_awaited()

    async def test_missing_chunk_is_not_found(self):
        db = AsyncMock()
        db.get_document_upload_fields = AsyncMock(return_value={"user_id": "u1"})
        db.get_document_chunk_by_index = AsyncMock(return_value=None)

        outcome = await delete_chunk_everywhere(db, DOC, WS, 99)
        assert outcome.found is False
