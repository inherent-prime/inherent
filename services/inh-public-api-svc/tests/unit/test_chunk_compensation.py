"""Unit tests for chunk-scoped compensation (#133 / #99 pattern).

Create-path vector failure must roll back the PG row via
``delete_chunk_with_retry`` — never swallow, never leave silent divergence.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src.services.compensation import delete_chunk_with_retry
from src.services.metrics import document_compensation_exhausted_total

pytestmark = pytest.mark.asyncio


def _exhausted_count(operation: str) -> float:
    return document_compensation_exhausted_total.labels(operation=operation)._value.get()


class TestDeleteChunkWithRetry:
    async def test_first_attempt_success(self):
        db = AsyncMock()
        db.delete_document_chunk = AsyncMock(return_value=MagicMockChunk())

        ok = await delete_chunk_with_retry(
            db,
            "doc-1",
            "ws-1",
            3,
            operation="chunk_create_vector_rollback",
        )

        assert ok is True
        db.delete_document_chunk.assert_awaited_once_with("doc-1", "ws-1", 3)

    async def test_already_absent_is_success(self):
        """Idempotent: row already gone counts as compensated."""
        db = AsyncMock()
        db.delete_document_chunk = AsyncMock(return_value=None)

        ok = await delete_chunk_with_retry(
            db, "doc-1", "ws-1", 3, operation="chunk_create_vector_rollback"
        )

        assert ok is True

    async def test_transient_failure_retried_then_succeeds(self):
        db = AsyncMock()
        db.delete_document_chunk = AsyncMock(
            side_effect=[RuntimeError("db blip"), MagicMockChunk()]
        )

        ok = await delete_chunk_with_retry(
            db,
            "doc-1",
            "ws-1",
            3,
            operation="chunk_create_vector_rollback",
            backoff_seconds=0,
        )

        assert ok is True
        assert db.delete_document_chunk.await_count == 2

    async def test_exhaustion_returns_false_and_bumps_metric(self):
        db = AsyncMock()
        db.delete_document_chunk = AsyncMock(side_effect=RuntimeError("db degraded"))
        before = _exhausted_count("chunk_create_vector_rollback")

        ok = await delete_chunk_with_retry(
            db,
            "doc-1",
            "ws-1",
            3,
            operation="chunk_create_vector_rollback",
            attempts=3,
            backoff_seconds=0,
        )

        assert ok is False
        assert db.delete_document_chunk.await_count == 3
        assert _exhausted_count("chunk_create_vector_rollback") == before + 1


class TestCreatePathVectorFailRollback:
    """Sprint 1 pin: after PG append, vector failure → compensate deletes the row."""

    async def test_rollback_calls_delete_with_retry_semantics(self):
        db = AsyncMock()
        appended = MagicMockChunk(chunk_index=4)
        db.append_document_chunk = AsyncMock(return_value=appended)
        db.delete_document_chunk = AsyncMock(return_value=appended)

        # Simulate create orchestration without Weaviate (Sprint 2 adds real upsert).
        chunk = await db.append_document_chunk("doc-1", "ws-1", "new text")
        assert chunk is not None
        vector_ok = False
        if not vector_ok:
            compensated = await delete_chunk_with_retry(
                db,
                "doc-1",
                "ws-1",
                chunk.chunk_index,
                operation="chunk_create_vector_rollback",
                backoff_seconds=0,
            )

        assert compensated is True
        db.delete_document_chunk.assert_awaited_once_with("doc-1", "ws-1", 4)


class MagicMockChunk:
    def __init__(self, chunk_index: int = 0):
        self.chunk_index = chunk_index
