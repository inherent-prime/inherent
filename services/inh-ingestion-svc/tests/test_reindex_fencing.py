"""Fencing-token regression tests for #110 blocker 1.

Terminating a Temporal workflow (id_conflict_policy=TERMINATE_EXISTING, see
src/temporal/trigger.py) stops the WORKFLOW but not an ACTIVITY it already
dispatched -- there is no activity heartbeat/cancellation wired anywhere in
this service (`grep -rn heartbeat src/` is empty), so a terminated
(superseded) run's in-flight store_in_postgresql/store_in_weaviate keeps
running to completion, unaware, and its write can land AFTER a newer run
already committed -- silently reverting the document to stale content while
still reporting status='processed'.

These tests exercise the REAL DatabaseService/Postgres fencing check (the
conditional UPSERT in store_processed_document + the claim in
create_pending_document) against a live database, proving the mechanism
that stops that from happening: a stale run's write is rejected once a
newer run has claimed the document, not just documented as a risk.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src.models.document import DocumentChunk, DocumentUploadMessage
from src.services.database import DatabaseService


def _message(document_id: str, filename: str = "f.txt") -> DocumentUploadMessage:
    return DocumentUploadMessage(
        event_type="document.uploaded",
        document_id=document_id,
        workspace_id="ws_fencing",
        user_id="user_fencing",
        filename=filename,
        original_filename=filename,
        content_type="text/plain",
        size_bytes=10,
        storage_backend="local",
        storage_path="workspaces/ws_fencing/f.txt",
        timestamp=datetime.now(UTC).isoformat(),
    )


def _chunk(document_id: str, text: str) -> DocumentChunk:
    return DocumentChunk(
        document_id=document_id,
        content=text,
        chunk_index=0,
        start_char=0,
        end_char=len(text),
    )


class TestStoreProcessedDocumentFencing:
    """store_processed_document must refuse to commit once a NEWER run has
    claimed the document -- this is the mechanism, not the documentation, of
    the #110 blocker 1 fix."""

    @pytest.mark.asyncio
    async def test_stale_run_write_is_rejected_after_newer_run_claims(
        self, db_service: DatabaseService
    ):
        """The exact scenario from the review: run A is slow and still
        in flight when run B (a fresh re-index) claims the document and
        commits its own, newer content. When A's late write finally arrives,
        it must be rejected -- B's content must survive untouched."""
        document_id = "doc_fencing_race_1"
        t_a = datetime(2026, 1, 1, tzinfo=UTC)
        t_b = t_a + timedelta(milliseconds=50)  # B genuinely started after A

        # Run A claims the document first (this is what create_pending_document
        # does at the top of every real workflow run).
        await db_service.create_pending_document(
            document_id=document_id,
            workspace_id="ws_fencing",
            user_id="user_fencing",
            filename="f.txt",
            original_filename="f.txt",
            content_type="text/plain",
            size_bytes=10,
            storage_backend="local",
            storage_path="workspaces/ws_fencing/f.txt",
            workflow_run_id="run-A",
            workflow_start_time=t_a,
        )

        # Run B (a fresh re-index) supersedes A: TERMINATE_EXISTING kills A's
        # workflow, B starts and claims the SAME document immediately.
        await db_service.create_pending_document(
            document_id=document_id,
            workspace_id="ws_fencing",
            user_id="user_fencing",
            filename="f.txt",
            original_filename="f.txt",
            content_type="text/plain",
            size_bytes=10,
            storage_backend="local",
            storage_path="workspaces/ws_fencing/f.txt",
            workflow_run_id="run-B",
            workflow_start_time=t_b,
        )

        # B is fast: its store step commits before A's ever does.
        doc_pk_b = await db_service.store_processed_document(
            message=_message(document_id),
            chunks=[_chunk(document_id, "VERSION-B (newer, must win)")],
            text_length=30,
            processing_time_ms=10,
            workflow_run_id="run-B",
        )
        assert doc_pk_b is not None

        # A's abandoned activity finally completes and tries to commit ITS
        # (stale) content. Must be rejected -- this is the regression #110
        # blocker 1 flagged: without fencing this call succeeds and silently
        # reverts the document to run A's stale content.
        doc_pk_a = await db_service.store_processed_document(
            message=_message(document_id),
            chunks=[_chunk(document_id, "VERSION-A (stale, must NOT win)")],
            text_length=32,
            processing_time_ms=999,
            workflow_run_id="run-A",
        )
        assert doc_pk_a is None, "stale run A's write must be fenced out, not applied"

        # The document must still reflect B's content -- chunk_count/text
        # from A's write must never have landed.
        chunks = await db_service.get_document_chunks(document_id)
        assert len(chunks) == 1
        assert "VERSION-B" in chunks[0]["content"]
        assert "VERSION-A" not in chunks[0]["content"]

        doc = await db_service.get_document_status(document_id)
        assert doc is not None
        assert doc["chunk_count"] == 1
        assert doc["processing_time_ms"] == 10  # B's value, not A's 999

    @pytest.mark.asyncio
    async def test_write_succeeds_when_unclaimed(self, db_service: DatabaseService):
        """A document with no prior claim (active_run_id IS NULL -- predates
        migration 016, or the claim step never ran) must not be permanently
        unwritable; the fencing check treats NULL as unclaimed/permitted."""
        document_id = "doc_fencing_unclaimed"

        doc_pk = await db_service.store_processed_document(
            message=_message(document_id),
            chunks=[_chunk(document_id, "first write, no prior claim")],
            text_length=10,
            processing_time_ms=5,
            workflow_run_id="run-only",
        )
        assert doc_pk is not None

    @pytest.mark.asyncio
    async def test_same_run_can_write_more_than_once(self, db_service: DatabaseService):
        """A retry of the SAME run's store activity (Temporal RetryPolicy)
        must not be fenced out by its own earlier claim/write."""
        document_id = "doc_fencing_retry"

        await db_service.create_pending_document(
            document_id=document_id,
            workspace_id="ws_fencing",
            user_id="user_fencing",
            filename="f.txt",
            original_filename="f.txt",
            content_type="text/plain",
            size_bytes=10,
            storage_backend="local",
            storage_path="workspaces/ws_fencing/f.txt",
            workflow_run_id="run-retry",
            workflow_start_time=datetime.now(UTC),
        )

        first = await db_service.store_processed_document(
            message=_message(document_id),
            chunks=[_chunk(document_id, "attempt 1")],
            text_length=9,
            processing_time_ms=5,
            workflow_run_id="run-retry",
        )
        second = await db_service.store_processed_document(
            message=_message(document_id),
            chunks=[_chunk(document_id, "attempt 2 (retry)")],
            text_length=17,
            processing_time_ms=6,
            workflow_run_id="run-retry",
        )
        assert first is not None
        assert second is not None


class TestIsActiveRun:
    """DatabaseService.is_active_run -- the pre-check store_in_weaviate uses
    since Weaviate has no transactional WHERE-on-write."""

    @pytest.mark.asyncio
    async def test_true_when_unclaimed(self, db_service: DatabaseService):
        assert await db_service.is_active_run("doc_never_seen", "any-run") is True

    @pytest.mark.asyncio
    async def test_true_when_claimed_by_same_run(self, db_service: DatabaseService):
        document_id = "doc_active_run_self"
        await db_service.create_pending_document(
            document_id=document_id,
            workspace_id="ws_fencing",
            user_id="user_fencing",
            filename="f.txt",
            original_filename="f.txt",
            content_type="text/plain",
            size_bytes=10,
            storage_backend="local",
            storage_path="workspaces/ws_fencing/f.txt",
            workflow_run_id="run-self",
            workflow_start_time=datetime.now(UTC),
        )
        assert await db_service.is_active_run(document_id, "run-self") is True

    @pytest.mark.asyncio
    async def test_false_when_claimed_by_a_different_run(self, db_service: DatabaseService):
        document_id = "doc_active_run_other"
        await db_service.create_pending_document(
            document_id=document_id,
            workspace_id="ws_fencing",
            user_id="user_fencing",
            filename="f.txt",
            original_filename="f.txt",
            content_type="text/plain",
            size_bytes=10,
            storage_backend="local",
            storage_path="workspaces/ws_fencing/f.txt",
            workflow_run_id="run-owner",
            workflow_start_time=datetime.now(UTC),
        )
        assert await db_service.is_active_run(document_id, "run-impostor") is False


class TestUpdateDocumentStatusFencing:
    """(#110 follow-up, ALSO FIX item 4) update_document_status must be
    fenced the same way store_processed_document is: a terminated
    (superseded) run's in-flight status write must not be able to land after
    a newer run finished, leaving status='processing' with no self-heal on a
    document whose actual content (already protected by the store-side
    fence) is correct."""

    @pytest.mark.asyncio
    async def test_stale_run_status_write_is_rejected_after_newer_run_claims(
        self, db_service: DatabaseService
    ):
        from src.services.database import DocumentStatus

        document_id = "doc_status_fencing_1"

        await db_service.create_pending_document(
            document_id=document_id,
            workspace_id="ws_fencing",
            user_id="user_fencing",
            filename="f.txt",
            original_filename="f.txt",
            content_type="text/plain",
            size_bytes=10,
            storage_backend="local",
            storage_path="workspaces/ws_fencing/f.txt",
            workflow_run_id="run-A",
            workflow_start_time=datetime(2026, 1, 1, tzinfo=UTC),
        )
        # B supersedes A and claims the document.
        await db_service.create_pending_document(
            document_id=document_id,
            workspace_id="ws_fencing",
            user_id="user_fencing",
            filename="f.txt",
            original_filename="f.txt",
            content_type="text/plain",
            size_bytes=10,
            storage_backend="local",
            storage_path="workspaces/ws_fencing/f.txt",
            workflow_run_id="run-B",
            workflow_start_time=datetime(2026, 1, 1, 0, 0, 0, 50000, tzinfo=UTC),
        )
        # B finishes successfully.
        updated = await db_service.update_document_status(
            document_id=document_id,
            status=DocumentStatus.PROCESSED,
            workflow_run_id="run-B",
        )
        assert updated is True

        # A's abandoned activity finally tries to mark the document
        # 'processing' (or 'failed') again. Must be rejected -- must NOT
        # revert a correctly-processed document's status.
        stale_updated = await db_service.update_document_status(
            document_id=document_id,
            status=DocumentStatus.PROCESSING,
            workflow_run_id="run-A",
        )
        assert stale_updated is False, "stale run A's status write must be fenced out"

        doc = await db_service.get_document_status(document_id)
        assert doc is not None
        assert doc["status"] == DocumentStatus.PROCESSED.value
        assert doc["processed_at"] is not None

    @pytest.mark.asyncio
    async def test_unfenced_when_workflow_run_id_omitted(self, db_service: DatabaseService):
        """Backward-compatible: a caller without a run context (none exist
        today) gets the pre-#110 unconditional write."""
        from src.services.database import DocumentStatus

        document_id = "doc_status_unfenced"
        await db_service.create_pending_document(
            document_id=document_id,
            workspace_id="ws_fencing",
            user_id="user_fencing",
            filename="f.txt",
            original_filename="f.txt",
            content_type="text/plain",
            size_bytes=10,
            storage_backend="local",
            storage_path="workspaces/ws_fencing/f.txt",
            workflow_run_id="run-only",
            workflow_start_time=datetime.now(UTC),
        )

        updated = await db_service.update_document_status(
            document_id=document_id,
            status=DocumentStatus.FAILED,
            error_message="no run context",
        )
        assert updated is True
