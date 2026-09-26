"""Workspace stats must be idempotent per workflow run (#7).

update_workspace_stats was a blind additive increment, so a Temporal retry or a
dead-letter reprocess of the same document double-counted document/chunk/size.
A ledger keyed on workflow_run_id makes the increment apply at most once.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from sqlalchemy import text

from src.models.document import DocumentChunk, DocumentUploadMessage
from src.services.database import DatabaseService, StoreProcessedDocumentInfo


def _make_db(session) -> DatabaseService:
    db = object.__new__(DatabaseService)  # bypass connect/table setup
    db.engine = MagicMock()

    @contextmanager
    def _gs():
        yield session

    db.get_session = _gs
    return db


@pytest.mark.asyncio
async def test_stats_skipped_when_run_already_applied():
    session = MagicMock()
    # Ledger insert hits the ON CONFLICT -> rowcount 0 -> already applied.
    session.execute.return_value = MagicMock(rowcount=0)
    db = _make_db(session)

    result = await db.update_workspace_stats(
        "ws", document_delta=1, chunk_delta=2, size_delta=3, workflow_run_id="run-1"
    )

    assert result is False
    # Only the ledger insert ran; the UPDATE was skipped (no double count).
    assert session.execute.call_count == 1


@pytest.mark.asyncio
async def test_stats_applied_once_for_a_new_run():
    session = MagicMock()
    # Ledger insert succeeds (rowcount 1), then the UPDATE runs (rowcount 1).
    session.execute.side_effect = [MagicMock(rowcount=1), MagicMock(rowcount=1)]
    db = _make_db(session)

    result = await db.update_workspace_stats("ws", document_delta=1, workflow_run_id="run-1")

    assert result is True
    assert session.execute.call_count == 2


@pytest.mark.asyncio
async def test_backward_compatible_without_run_id():
    session = MagicMock()
    session.execute.return_value = MagicMock(rowcount=1)
    db = _make_db(session)

    # No workflow_run_id -> old unconditional behaviour (single UPDATE, no ledger).
    result = await db.update_workspace_stats("ws", document_delta=1)

    assert result is True
    assert session.execute.call_count == 1


# ---------------------------------------------------------------------------
# #364: workspace_metadata.document_count must not drift after a conversation
# document is deleted and re-ingested by a still-running workflow.
#
# Requires a live Postgres (`db_service` fixture, tests/conftest.py) -- skips
# without one, same as every other store_processed_document integration test
# in this package (test_store_append_mode.py, test_reindex_fencing.py).
# ---------------------------------------------------------------------------


def _conversation_message(document_id: str, workspace_id: str) -> DocumentUploadMessage:
    return DocumentUploadMessage(
        event_type="document.uploaded",
        document_id=document_id,
        workspace_id=workspace_id,
        user_id="test_user_364",
        filename=document_id,
        original_filename="conv-364",
        content_type="text/plain",
        size_bytes=10,
        storage_backend="local",
        storage_path=f"conversation://{workspace_id}/conv-364",
        timestamp=datetime.now(UTC).isoformat(),
    )


def _turn_chunk(document_id: str, text_content: str, chunk_index: int) -> DocumentChunk:
    return DocumentChunk(
        document_id=document_id,
        content=text_content,
        chunk_index=chunk_index,
        start_char=0,
        end_char=len(text_content),
    )


class TestDocumentCountSurvivesDeleteThenReflush:
    """#364: a conversation's workflow keeps running across `DELETE
    /v1/conversations/{external_id}` (that endpoint deliberately does not
    terminate it, to avoid racing an in-flight flush -- see
    conversation_memory.py's module docstring). The next turn's flush must
    still be counted so `workspace_metadata.document_count` matches the real
    `processed_documents` row count, not permanently under-count it.
    """

    @pytest.mark.asyncio
    async def test_document_count_matches_row_count_after_delete_then_reflush(
        self, db_service: DatabaseService
    ):
        workspace_id = "test_364_ws"
        document_id = "test_364_conv1"
        await db_service.upsert_tenant("test_user_364")  # FK target for workspace_metadata
        await db_service.upsert_workspace_metadata(workspace_id, user_id="test_user_364")

        # --- Flush 1: first turn creates the conversation's document row ---
        info_1 = StoreProcessedDocumentInfo()
        doc_pk_1 = await db_service.store_processed_document(
            message=_conversation_message(document_id, workspace_id),
            chunks=[_turn_chunk(document_id, "turn one", 0)],
            text_length=8,
            processing_time_ms=1,
            workflow_run_id="run-364",
            append=True,
            document_type="conversation",
            external_id="conv1",
            result_info=info_1,
        )
        assert doc_pk_1 is not None
        assert info_1.row_was_inserted is True, "the very first flush must INSERT a new row"

        # document_delta derived from the DB signal (#364's fix), not from
        # any workflow-local "have I created this document" flag.
        #
        # workflow_run_id intentionally omitted here (and below): the #7
        # idempotency ledger is keyed on workflow_run_id alone and is
        # designed for ONE execute_activity call per workflow run
        # (DocumentIngestionWorkflow); ConversationMemoryWorkflow calls
        # update_workspace_stats once per FLUSH, reusing the SAME run_id
        # across flushes within one continue_as_new segment. Exercising
        # that ledger-vs-multi-flush interaction is a separate, pre-existing
        # concern from #364's document_delta bug -- omitting workflow_run_id
        # isolates this test to exactly what #364 changed.
        applied = await db_service.update_workspace_stats(
            workspace_id, document_delta=1 if info_1.row_was_inserted else 0, chunk_delta=1
        )
        assert applied is True

        # --- Simulate DELETE /v1/conversations/{external_id} (inh-public- --
        # --- api-svc's src/services/database.py delete_conversation path, --
        # --- reproduced here since that service isn't importable from this --
        # --- one): removes the processed_documents row (chunks cascade)   --
        # --- and decrements document_count, WITHOUT terminating the       --
        # --- workflow that's still running for this conversation.        ---
        with db_service.get_session() as session:
            session.execute(
                db_service.processed_documents.delete().where(
                    db_service.processed_documents.c.document_id == document_id
                )
            )
            session.execute(
                text(
                    """
                    UPDATE workspace_metadata
                    SET document_count = GREATEST(document_count - 1, 0)
                    WHERE workspace_id = :workspace_id
                    """
                ),
                {"workspace_id": workspace_id},
            )

        with db_service.get_session() as session:
            row_count_after_delete = session.execute(
                text("SELECT COUNT(*) FROM processed_documents WHERE workspace_id = :ws"),
                {"ws": workspace_id},
            ).scalar_one()
        assert row_count_after_delete == 0

        # --- Flush 2: a later turn on the SAME still-running workflow -----
        # finds no row (it was deleted) and INSERTs a fresh one.
        info_2 = StoreProcessedDocumentInfo()
        doc_pk_2 = await db_service.store_processed_document(
            message=_conversation_message(document_id, workspace_id),
            chunks=[_turn_chunk(document_id, "turn two", 0)],
            text_length=8,
            processing_time_ms=1,
            workflow_run_id="run-364",  # SAME run_id as flush 1 -- realistic:
            # the workflow was never terminated, so it is still the same
            # Temporal run (see module docstring on why DELETE does not
            # terminate it).
            append=True,
            document_type="conversation",
            external_id="conv1",
            result_info=info_2,
        )
        assert doc_pk_2 is not None
        # The row was deleted, so this upsert's ON CONFLICT branch has
        # nothing to conflict with -- it INSERTs, exactly like flush 1.
        # This is the signal the OLD code could not see (it only had
        # `self._document_created`, already True and never reset).
        assert info_2.row_was_inserted is True, (
            "a flush after the document row was deleted must be seen as an INSERT, "
            "not silently treated as an update to a since-vanished row"
        )
        assert doc_pk_2 != doc_pk_1, "the re-created row is a brand-new processed_documents row"

        await db_service.update_workspace_stats(
            workspace_id, document_delta=1 if info_2.row_was_inserted else 0, chunk_delta=1
        )

        # --- The invariant #364 is about: the denormalised counter must ---
        # --- match reality, not just "some value that stopped drifting" --
        with db_service.get_session() as session:
            document_count = session.execute(
                text("SELECT document_count FROM workspace_metadata WHERE workspace_id = :ws"),
                {"ws": workspace_id},
            ).scalar_one()
            real_row_count = session.execute(
                text("SELECT COUNT(*) FROM processed_documents WHERE workspace_id = :ws"),
                {"ws": workspace_id},
            ).scalar_one()

        assert document_count == real_row_count == 1, (
            f"workspace_metadata.document_count ({document_count}) must equal the real "
            f"processed_documents row count ({real_row_count}) after a delete-then-reflush"
        )

    @pytest.mark.asyncio
    async def test_repeated_flush_of_the_same_conversation_stays_idempotent(
        self, db_service: DatabaseService
    ):
        """Existing idempotency guarantee, unaffected by #364's fix: flushing
        the SAME still-existing conversation document twice (no delete in
        between) must still yield document_delta=0 for the second flush."""
        workspace_id = "test_364_ws_2"
        document_id = "test_364_conv2"
        await db_service.upsert_tenant("test_user_364")  # FK target for workspace_metadata
        await db_service.upsert_workspace_metadata(workspace_id, user_id="test_user_364")

        info_1 = StoreProcessedDocumentInfo()
        await db_service.store_processed_document(
            message=_conversation_message(document_id, workspace_id),
            chunks=[_turn_chunk(document_id, "turn one", 0)],
            text_length=8,
            processing_time_ms=1,
            workflow_run_id="run-364-b",
            append=True,
            document_type="conversation",
            external_id="conv2",
            result_info=info_1,
        )
        assert info_1.row_was_inserted is True
        await db_service.update_workspace_stats(
            workspace_id, document_delta=1 if info_1.row_was_inserted else 0, chunk_delta=1
        )

        # Second flush of the SAME conversation, row still present -> goes
        # through DO UPDATE, not INSERT.
        info_2 = StoreProcessedDocumentInfo()
        await db_service.store_processed_document(
            message=_conversation_message(document_id, workspace_id),
            chunks=[_turn_chunk(document_id, "turn two", 1)],
            text_length=8,
            processing_time_ms=1,
            workflow_run_id="run-364-b",
            append=True,
            document_type="conversation",
            external_id="conv2",
            result_info=info_2,
        )
        assert info_2.row_was_inserted is False, "the row still exists -- this must be an UPDATE"
        await db_service.update_workspace_stats(
            workspace_id, document_delta=1 if info_2.row_was_inserted else 0, chunk_delta=1
        )

        with db_service.get_session() as session:
            document_count = session.execute(
                text("SELECT document_count FROM workspace_metadata WHERE workspace_id = :ws"),
                {"ws": workspace_id},
            ).scalar_one()
        assert document_count == 1, "a repeated flush of one conversation must count ONE document"
