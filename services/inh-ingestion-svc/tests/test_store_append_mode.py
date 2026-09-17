"""Tests for `StoreDocumentInput.append` / `store_processed_document`'s
append extension (#306).

The issue's own text contradicts itself: "reuse the store activities, don't
fork them" vs. the store activities being DESTRUCTIVE full-replace (DELETE
all chunks, then re-insert). Calling them unmodified on every ~90s
conversation flush would silently destroy every previously-flushed turn's
chunks from the second flush on. The resolution is `append: bool = False`:
default False keeps DocumentIngestionWorkflow's full-replace behavior
BYTE-IDENTICAL (this file proves that with the exact same assertions
test_reindex_fencing.py / test_database.py already make); True switches to
additive growth -- these tests prove BOTH modes.

Requires a live Postgres (`db_service` fixture) -- skips without Docker per
conftest.py's autouse `cleanup_test_data`, same as every other
store_processed_document test in this package (test_reindex_fencing.py,
test_database.py).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from src.models.document import DocumentChunk, DocumentUploadMessage
from src.services.database import DatabaseService


def _message(document_id: str, workspace_id: str = "ws_append") -> DocumentUploadMessage:
    return DocumentUploadMessage(
        event_type="document.uploaded",
        document_id=document_id,
        workspace_id=workspace_id,
        user_id="user_append",
        filename="f.txt",
        original_filename="f.txt",
        content_type="text/plain",
        size_bytes=10,
        storage_backend="local",
        storage_path=f"workspaces/{workspace_id}/f.txt",
        timestamp=datetime.now(UTC).isoformat(),
    )


def _chunks(document_id: str, texts: list[str], start_index: int = 0) -> list[DocumentChunk]:
    return [
        DocumentChunk(
            document_id=document_id,
            content=text,
            chunk_index=start_index + i,
            start_char=0,
            end_char=len(text),
        )
        for i, text in enumerate(texts)
    ]


class TestAppendFalsePreservesExistingBehavior:
    """append=False (the default) must be byte-identical to pre-#306
    behavior: destructive full-replace."""

    @pytest.mark.asyncio
    async def test_default_append_false_replaces_chunks(self, db_service: DatabaseService):
        document_id = "doc_append_false_1"
        message = _message(document_id)

        doc_pk_1 = await db_service.store_processed_document(
            message=message,
            chunks=_chunks(document_id, ["chunk one", "chunk two"]),
            text_length=18,
            processing_time_ms=5,
            workflow_run_id="run-1",
        )
        assert doc_pk_1 is not None

        # A second store call (re-index) with FEWER chunks must fully
        # replace, not append -- the pre-#306 contract.
        doc_pk_2 = await db_service.store_processed_document(
            message=message,
            chunks=_chunks(document_id, ["only chunk"]),
            text_length=10,
            processing_time_ms=5,
            workflow_run_id="run-1",
        )
        assert doc_pk_2 == doc_pk_1

        with db_service.get_session() as session:
            row = session.execute(
                db_service.processed_documents.select().where(
                    db_service.processed_documents.c.document_id == document_id
                )
            ).first()
            assert row.chunk_count == 1  # overwritten, not 2+1=3
            assert row.document_type == "file"  # default, unchanged
            assert row.external_id is None

            chunk_rows = session.execute(
                db_service.document_chunks.select().where(
                    db_service.document_chunks.c.document_id == document_id
                )
            ).fetchall()
            assert len(chunk_rows) == 1
            assert chunk_rows[0].content == "only chunk"

    @pytest.mark.asyncio
    async def test_omitting_append_kwarg_entirely_still_replaces(self, db_service: DatabaseService):
        """A caller that never learned about `append` (every call site that
        predates #306) must see no behavior change at all."""
        document_id = "doc_append_false_2"
        message = _message(document_id)

        await db_service.store_processed_document(
            message=message,
            chunks=_chunks(document_id, ["a", "b", "c"]),
            text_length=3,
            processing_time_ms=1,
            workflow_run_id="run-1",
        )
        await db_service.store_processed_document(
            message=message,
            chunks=_chunks(document_id, ["z"]),
            text_length=1,
            processing_time_ms=1,
            workflow_run_id="run-1",
        )

        with db_service.get_session() as session:
            row = session.execute(
                db_service.processed_documents.select().where(
                    db_service.processed_documents.c.document_id == document_id
                )
            ).first()
            assert row.chunk_count == 1


class TestAppendTrueGrowsInsteadOfReplacing:
    """append=True must skip the delete and GROW chunk_count/text_length/
    size_bytes instead of overwriting them -- the conversation flush path."""

    @pytest.mark.asyncio
    async def test_second_flush_does_not_delete_first_flushes_chunks(
        self, db_service: DatabaseService
    ):
        document_id = "conv-ws_append-conv_1"
        message = _message(document_id)

        # Flush 1: creates the row.
        doc_pk_1 = await db_service.store_processed_document(
            message=message,
            chunks=_chunks(document_id, ["turn one text"], start_index=0),
            text_length=13,
            processing_time_ms=1,
            workflow_run_id="run-conv",
            append=True,
            document_type="conversation",
            external_id="conv_1",
            metadata={"turn_count": 1, "last_flushed_at": "2026-08-31T00:00:00Z"},
        )
        assert doc_pk_1 is not None

        # Flush 2: chunk_index continues from where flush 1 left off (2 --
        # chunk_conversation's job in the real pipeline; this test drives
        # store_processed_document directly, so it supplies the already-
        # continued index itself).
        doc_pk_2 = await db_service.store_processed_document(
            message=message,
            chunks=_chunks(document_id, ["turn two text"], start_index=1),
            text_length=13,
            processing_time_ms=1,
            workflow_run_id="run-conv",
            append=True,
            document_type="conversation",
            external_id="conv_1",
            metadata={"turn_count": 2, "last_flushed_at": "2026-08-31T00:01:30Z"},
        )
        assert doc_pk_2 == doc_pk_1

        with db_service.get_session() as session:
            row = session.execute(
                db_service.processed_documents.select().where(
                    db_service.processed_documents.c.document_id == document_id
                )
            ).first()
            # GROWN, not overwritten: 1 (flush 1) + 1 (flush 2) = 2.
            assert row.chunk_count == 2
            assert row.text_length == 26
            assert row.document_type == "conversation"
            assert row.external_id == "conv_1"
            assert row.metadata["turn_count"] == 2

            chunk_rows = session.execute(
                db_service.document_chunks.select()
                .where(db_service.document_chunks.c.document_id == document_id)
                .order_by(db_service.document_chunks.c.chunk_index)
            ).fetchall()
            # BOTH flushes' chunks survive -- flush 2 did not delete flush 1's.
            assert len(chunk_rows) == 2
            assert chunk_rows[0].content == "turn one text"
            assert chunk_rows[1].content == "turn two text"

    @pytest.mark.asyncio
    async def test_append_true_first_call_creates_row_normally(self, db_service: DatabaseService):
        """append=True on a document with NO existing row must still work
        (the very first conversation flush) -- INSERT branch, not UPDATE."""
        document_id = "conv-ws_append-conv_2"
        message = _message(document_id)

        doc_pk = await db_service.store_processed_document(
            message=message,
            chunks=_chunks(document_id, ["hello", "world"]),
            text_length=10,
            processing_time_ms=1,
            workflow_run_id="run-conv-2",
            append=True,
            document_type="conversation",
            external_id="conv_2",
        )
        assert doc_pk is not None

        with db_service.get_session() as session:
            row = session.execute(
                db_service.processed_documents.select().where(
                    db_service.processed_documents.c.document_id == document_id
                )
            ).first()
            assert row.chunk_count == 2
            assert row.document_type == "conversation"
            assert row.external_id == "conv_2"


class TestGetDocumentChunkCount:
    @pytest.mark.asyncio
    async def test_returns_zero_for_unknown_document(self, db_service: DatabaseService):
        assert await db_service.get_document_chunk_count("does-not-exist") == 0

    @pytest.mark.asyncio
    async def test_returns_stored_chunk_count(self, db_service: DatabaseService):
        document_id = "conv-ws_append-conv_3"
        message = _message(document_id)
        await db_service.store_processed_document(
            message=message,
            chunks=_chunks(document_id, ["a", "b", "c"]),
            text_length=3,
            processing_time_ms=1,
            workflow_run_id="run-conv-3",
            append=True,
            document_type="conversation",
            external_id="conv_3",
        )
        assert await db_service.get_document_chunk_count(document_id) == 3


class TestAppendRetryIdempotency:
    """Regression tests for the Temporal-retry non-idempotency bug: the
    append path used to IGNORE the `chunk_index` already stamped on each
    chunk (by `chunk_conversation`, which assigns a global, continuing index
    via `get_document_chunk_count` BEFORE any chunk is produced) and instead
    recomputed it from a live `MAX(chunk_index)` query. `store_in_postgresql`
    reads its chunks from staging keyed by `workflow_run_id`, which is
    STABLE across a Temporal activity retry (`retry_policy=RetryPolicy(
    maximum_attempts=5)` on the real activity) -- so a retry after a lost
    ack replayed the SAME staged chunks, and the live MAX() recompute always
    found a fresh, non-colliding offset for them: silent duplication, since
    the `uq_document_chunks_doc_idx` unique constraint never got a chance to
    fire. Fixed by using `chunk.chunk_index` verbatim (deterministic,
    input-derived) plus `ON CONFLICT (processed_document_id, chunk_index)
    DO NOTHING` on the insert, with `chunk_count` grown by the insert's
    ACTUAL rowcount rather than blindly by `len(chunks)`.

    Document ids start with `test_` so the autouse `cleanup_test_data`
    fixture (which matches on that prefix) actually cleans these up --
    unlike this file's other tests, whose `conv-...`/`doc_...` ids are a
    pre-existing, unrelated gap (#358, not fixed here).
    """

    @pytest.mark.asyncio
    async def test_retry_with_identical_chunks_does_not_double_count_or_duplicate(
        self, db_service: DatabaseService
    ):
        """Two calls with the SAME chunks (same chunk_index values, same
        workflow_run_id) -- simulating a Temporal retry replaying the same
        staged chunks after a lost ack -- must leave the document with
        exactly the FIRST call's chunk rows and chunk_count, not doubled."""
        document_id = "test_append_retry_1"
        message = _message(document_id)
        chunks = _chunks(document_id, ["turn one text", "turn two text"], start_index=0)

        doc_pk_1 = await db_service.store_processed_document(
            message=message,
            chunks=chunks,
            text_length=26,
            processing_time_ms=1,
            workflow_run_id="run-retry-1",
            append=True,
            document_type="conversation",
            external_id="conv_retry_1",
        )
        assert doc_pk_1 is not None

        # The "retry": identical chunks (same chunk_index 0/1), same
        # workflow_run_id -- exactly what a redelivered/retried
        # store_in_postgresql activity attempt would replay from staging.
        doc_pk_2 = await db_service.store_processed_document(
            message=message,
            chunks=chunks,
            text_length=26,
            processing_time_ms=1,
            workflow_run_id="run-retry-1",
            append=True,
            document_type="conversation",
            external_id="conv_retry_1",
        )
        assert doc_pk_2 == doc_pk_1

        with db_service.get_session() as session:
            row = session.execute(
                db_service.processed_documents.select().where(
                    db_service.processed_documents.c.document_id == document_id
                )
            ).first()
            # NOT doubled: the retry's chunks all conflict-skip on insert,
            # so the idempotent chunk_count step adds 0 the second time.
            # (text_length/size_bytes get the same treatment -- see
            # TestAppendRetryIdempotency's own text_length/size_bytes test
            # below.)
            assert row.chunk_count == 2

            chunk_rows = session.execute(
                db_service.document_chunks.select()
                .where(db_service.document_chunks.c.document_id == document_id)
                .order_by(db_service.document_chunks.c.chunk_index)
            ).fetchall()
            # NOT duplicated: still exactly the first call's two rows.
            assert len(chunk_rows) == 2
            assert [c.chunk_index for c in chunk_rows] == [0, 1]
            assert [c.content for c in chunk_rows] == ["turn one text", "turn two text"]

    @pytest.mark.asyncio
    async def test_retry_after_a_real_second_flush_still_only_counts_the_new_chunks(
        self, db_service: DatabaseService
    ):
        """A real second flush (genuinely new chunk_index values) followed
        by a retry of THAT SAME second flush must count the new chunks
        exactly once, on top of the first flush's chunks -- not zero (an
        under-count bug) and not doubled."""
        document_id = "test_append_retry_2"
        message = _message(document_id)

        flush_1_chunks = _chunks(document_id, ["turn one"], start_index=0)
        await db_service.store_processed_document(
            message=message,
            chunks=flush_1_chunks,
            text_length=8,
            processing_time_ms=1,
            workflow_run_id="run-retry-2",
            append=True,
            document_type="conversation",
            external_id="conv_retry_2",
        )

        flush_2_chunks = _chunks(document_id, ["turn two", "turn three"], start_index=1)
        for _ in range(2):  # original attempt, then a retry of the SAME flush
            doc_pk = await db_service.store_processed_document(
                message=message,
                chunks=flush_2_chunks,
                text_length=18,
                processing_time_ms=1,
                workflow_run_id="run-retry-2",
                append=True,
                document_type="conversation",
                external_id="conv_retry_2",
            )
            assert doc_pk is not None

        with db_service.get_session() as session:
            row = session.execute(
                db_service.processed_documents.select().where(
                    db_service.processed_documents.c.document_id == document_id
                )
            ).first()
            # 1 (flush 1) + 2 (flush 2, counted ONCE despite its retry) = 3.
            assert row.chunk_count == 3

            chunk_rows = session.execute(
                db_service.document_chunks.select()
                .where(db_service.document_chunks.c.document_id == document_id)
                .order_by(db_service.document_chunks.c.chunk_index)
            ).fetchall()
            assert len(chunk_rows) == 3
            assert [c.chunk_index for c in chunk_rows] == [0, 1, 2]

    @pytest.mark.asyncio
    async def test_retry_with_identical_chunks_does_not_double_count_text_length_or_size_bytes(
        self, db_service: DatabaseService
    ):
        """Same scenario as
        test_retry_with_identical_chunks_does_not_double_count_or_duplicate,
        but for text_length/size_bytes: these were NOT covered by the
        original chunk_count/chunk_index fix and kept growing
        unconditionally in `update_set` on every call, so a retry (whose
        chunks all conflict-skip and rightly contribute 0 to chunk_count)
        still doubled text_length/size_bytes. Both are now grown in the
        same `inserted_chunk_count`-gated post-insert step as chunk_count,
        so a retry must leave them at the first call's values too."""
        document_id = "test_append_retry_text_length_1"
        message = _message(document_id)  # size_bytes=10
        chunks = _chunks(document_id, ["turn one text", "turn two text"], start_index=0)

        doc_pk_1 = await db_service.store_processed_document(
            message=message,
            chunks=chunks,
            text_length=26,
            processing_time_ms=1,
            workflow_run_id="run-retry-text-length-1",
            append=True,
            document_type="conversation",
            external_id="conv_retry_text_length_1",
        )
        assert doc_pk_1 is not None

        # The "retry": identical chunks (same chunk_index 0/1), same
        # text_length/size_bytes, same workflow_run_id -- exactly what a
        # redelivered/retried store_in_postgresql activity attempt would
        # replay from staging.
        doc_pk_2 = await db_service.store_processed_document(
            message=message,
            chunks=chunks,
            text_length=26,
            processing_time_ms=1,
            workflow_run_id="run-retry-text-length-1",
            append=True,
            document_type="conversation",
            external_id="conv_retry_text_length_1",
        )
        assert doc_pk_2 == doc_pk_1

        with db_service.get_session() as session:
            row = session.execute(
                db_service.processed_documents.select().where(
                    db_service.processed_documents.c.document_id == document_id
                )
            ).first()
            # NOT doubled: still the first call's values (26 / 10), not the
            # pre-fix bug's 52 / 20.
            assert row.text_length == 26
            assert row.size_bytes == 10
            assert row.chunk_count == 2

    @pytest.mark.asyncio
    async def test_retry_after_a_real_second_flush_grows_text_length_and_size_bytes_once(
        self, db_service: DatabaseService
    ):
        """A real second flush (genuinely new content) followed by a retry
        of THAT SAME second flush must grow text_length/size_bytes by
        exactly the second flush's contribution, on top of the first
        flush's -- not zero (an under-count, e.g. if growth were wrongly
        gated on the brand-new-row case only) and not doubled (the bug this
        whole fix removes)."""
        document_id = "test_append_retry_text_length_2"
        message = _message(document_id)  # size_bytes=10

        flush_1_chunks = _chunks(document_id, ["turn one"], start_index=0)
        await db_service.store_processed_document(
            message=message,
            chunks=flush_1_chunks,
            text_length=8,
            processing_time_ms=1,
            workflow_run_id="run-retry-text-length-2",
            append=True,
            document_type="conversation",
            external_id="conv_retry_text_length_2",
        )

        flush_2_chunks = _chunks(document_id, ["turn two", "turn three"], start_index=1)
        for _ in range(2):  # original attempt, then a retry of the SAME flush
            doc_pk = await db_service.store_processed_document(
                message=message,
                chunks=flush_2_chunks,
                text_length=18,
                processing_time_ms=1,
                workflow_run_id="run-retry-text-length-2",
                append=True,
                document_type="conversation",
                external_id="conv_retry_text_length_2",
            )
            assert doc_pk is not None

        with db_service.get_session() as session:
            row = session.execute(
                db_service.processed_documents.select().where(
                    db_service.processed_documents.c.document_id == document_id
                )
            ).first()
            # 8 (flush 1) + 18 (flush 2, counted ONCE despite its retry) = 26.
            assert row.text_length == 26
            # message.size_bytes is a constant 10 per call (see _message) --
            # 10 (flush 1) + 10 (flush 2, counted ONCE) = 20.
            assert row.size_bytes == 20
            assert row.chunk_count == 3
