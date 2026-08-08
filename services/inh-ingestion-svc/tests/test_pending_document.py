"""A document must be observable in the status API before the store step (#10).

No processed_documents row existed until the store step, so an early
'processing'/'failed' status write hit 0 rows and a document that failed during
fetch/extract/chunk showed 'not found'. The workflow now creates a minimal
'processing' row up front.

Also (#110): this activity is where a workflow run claims the document's
fencing token (active_run_id) -- see store_processed_document /
TestCreatePendingDocumentClaimsFencingToken below. And (#110 follow-up): the
claim itself must be ORDERED by each run's start time, not by which claim
write happens to commit last -- see TestClaimOrderingIsMonotonic.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.services.database import DatabaseService, DocumentStatus
from src.temporal.activities.status import create_pending_document
from src.temporal.models import CreatePendingDocumentInput

_START_TIME = datetime(2026, 1, 1, tzinfo=UTC)


def _input() -> CreatePendingDocumentInput:
    return CreatePendingDocumentInput(
        document_id="doc-1",
        workspace_id="ws",
        user_id="u",
        filename="f.txt",
        original_filename="orig.txt",
        content_type="text/plain",
        size_bytes=10,
        storage_backend="local",
        storage_path="p",
        workflow_run_id="run-abc",
        workflow_start_time=_START_TIME,
    )


@pytest.mark.asyncio
async def test_activity_delegates_to_db():
    db = MagicMock()
    db.create_pending_document = AsyncMock(return_value=True)
    with patch("src.temporal.shared_services.get_db_service", return_value=db):
        result = await create_pending_document(_input())
    assert result is True
    kwargs = db.create_pending_document.await_args.kwargs
    assert kwargs["document_id"] == "doc-1"
    assert kwargs["workspace_id"] == "ws"
    # (#110) the activity must forward workflow_run_id so the DB layer can
    # claim the fencing token -- without this, create_pending_document could
    # not tell WHICH run is claiming the document.
    assert kwargs["workflow_run_id"] == "run-abc"
    # (#110 follow-up) and workflow_start_time, so the claim can be ordered
    # by when the run actually STARTED, not by commit order.
    assert kwargs["workflow_start_time"] == _START_TIME


@pytest.mark.asyncio
async def test_db_create_pending_returns_true_on_insert():
    session = MagicMock()
    session.execute.return_value = MagicMock(rowcount=1)
    # Use a fully-initialised service so self.processed_documents exists.
    from src.config.settings import Settings

    db = DatabaseService.__new__(DatabaseService)
    DatabaseService.__init__(db, Settings.model_construct())
    db.engine = MagicMock()

    @contextmanager
    def _gs():
        yield session

    db.get_session = _gs

    created = await db.create_pending_document(
        document_id="doc-1",
        workspace_id="ws",
        user_id="u",
        filename="f.txt",
        original_filename="orig.txt",
        content_type="text/plain",
        size_bytes=10,
        storage_backend="local",
        storage_path="p",
        workflow_run_id="run-abc",
        workflow_start_time=_START_TIME,
    )
    assert created is True
    # (#110) two statements now: the INSERT ... ON CONFLICT DO NOTHING (row
    # create, unchanged from #10) AND a monotonically-guarded UPDATE that
    # claims active_run_id/active_run_claimed_at -- see
    # TestCreatePendingDocumentClaimsFencingToken and
    # TestClaimOrderingIsMonotonic for what that second statement asserts.
    assert session.execute.call_count == 2
    assert DocumentStatus.PROCESSING.value == "processing"


# ---------------------------------------------------------------------------
# Fencing-token claim tests (#110 blocker 1)
# ---------------------------------------------------------------------------


class TestCreatePendingDocumentClaimsFencingToken:
    """create_pending_document must claim active_run_id for THIS run on
    every call -- insert (brand-new document) AND conflict (re-index of an
    existing document) -- since the conflict case is exactly the re-index
    scenario #110 is about. Without the claim, a later store commit from a
    stale, superseded run can't be told apart from a legitimate one."""

    def _db_with_recording_session(self):
        """A DatabaseService whose session.execute() records every statement
        passed to it, so tests can inspect what was actually sent to
        Postgres (values on the INSERT, the WHERE/values on the UPDATE)."""
        from src.config.settings import Settings

        db = DatabaseService.__new__(DatabaseService)
        DatabaseService.__init__(db, Settings.model_construct())
        db.engine = MagicMock()

        executed = []
        session = MagicMock()

        def _execute(stmt, *a, **kw):
            executed.append(stmt)
            return MagicMock(rowcount=1)

        session.execute.side_effect = _execute

        @contextmanager
        def _gs():
            yield session

        db.get_session = _gs
        return db, executed

    @pytest.mark.asyncio
    async def test_insert_branch_stamps_active_run_id(self):
        """A brand-new document (no prior row) claims active_run_id AND
        active_run_claimed_at as part of the INSERT's own values."""
        db, executed = self._db_with_recording_session()

        await db.create_pending_document(
            document_id="doc-new",
            workspace_id="ws",
            user_id="u",
            filename="f.txt",
            original_filename="orig.txt",
            content_type="text/plain",
            size_bytes=10,
            storage_backend="local",
            storage_path="p",
            workflow_run_id="run-A",
            workflow_start_time=_START_TIME,
        )

        insert_stmt = executed[0]
        # SQLAlchemy Insert exposes its VALUES as .select_names / compiled
        # params; the simplest robust check is compiling and inspecting the
        # bound parameters.
        compiled = insert_stmt.compile()
        assert compiled.params.get("active_run_id") == "run-A"
        assert compiled.params.get("active_run_claimed_at") == _START_TIME

    @pytest.mark.asyncio
    async def test_conflict_branch_claims_via_monotonically_guarded_update(self):
        """The re-index case (row already exists, INSERT ON CONFLICT DO
        NOTHING is a no-op): the SECOND statement is an UPDATE that sets
        active_run_id/active_run_claimed_at to the new run -- this is what
        makes a re-index's run supersede a stale run's claim. (The update is
        guarded, not unconditional -- see TestClaimOrderingIsMonotonic for
        what the guard itself asserts.)"""
        db, executed = self._db_with_recording_session()

        await db.create_pending_document(
            document_id="doc-existing",
            workspace_id="ws",
            user_id="u",
            filename="f.txt",
            original_filename="orig.txt",
            content_type="text/plain",
            size_bytes=10,
            storage_backend="local",
            storage_path="p",
            workflow_run_id="run-B",
            workflow_start_time=_START_TIME,
        )

        assert len(executed) == 2
        update_stmt = executed[1]
        compiled = update_stmt.compile()
        assert compiled.params.get("active_run_id") == "run-B"
        assert compiled.params.get("active_run_claimed_at") == _START_TIME


# ---------------------------------------------------------------------------
# Claim ordering must be monotonic in START time, not commit order
# (#110 follow-up review, "the claim is fenced but non-monotonic")
# ---------------------------------------------------------------------------


class TestClaimOrderingIsMonotonic:
    """The exact scenario the follow-up review caught: run A starts, its
    create_pending_document is dispatched; ~50ms later a fresh run B for the
    same document (an agent's rapid retry) terminates A and starts. A's
    already-dispatched claim UPDATE is NOT stopped by A's termination (same
    premise the store-side fencing already accepts for store activities --
    create_pending_document is an ordinary, un-heartbeated activity). If A's
    claim commits AFTER B's, a bare unconditional UPDATE would let the DEAD
    run A win the claim, fencing the legitimate newest run B out of its OWN
    store step. These tests claim B first, then claim A (reproducing A's
    claim arriving late), and assert B still owns the row -- AND that B's
    store step still commits, which is the actual user-visible guarantee
    (#110's whole point: the newest content wins)."""

    @pytest.mark.asyncio
    async def test_earlier_starting_runs_late_claim_does_not_overwrite_later_run(
        self, db_service: DatabaseService
    ):
        document_id = "doc_claim_order_1"
        t_a = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        t_b = t_a + timedelta(milliseconds=50)  # B started AFTER A

        common_kwargs = dict(
            document_id=document_id,
            workspace_id="ws_fencing",
            user_id="user_fencing",
            filename="f.txt",
            original_filename="f.txt",
            content_type="text/plain",
            size_bytes=10,
            storage_backend="local",
            storage_path="workspaces/ws_fencing/f.txt",
        )

        # B claims first in wall-clock terms (its activity reaches the DB
        # before A's late one does) -- this is the "commit order" that a
        # non-monotonic guard would blindly trust.
        await db_service.create_pending_document(
            workflow_run_id="run-B",
            workflow_start_time=t_b,
            **common_kwargs,
        )

        # A's claim arrives LATE (it started earlier, at t_a, but its
        # already-dispatched activity's write only lands now, after B's).
        await db_service.create_pending_document(
            workflow_run_id="run-A",
            workflow_start_time=t_a,
            **common_kwargs,
        )

        # B must still own the row -- A (the earlier-starting, terminated
        # run) must not have been able to steal the claim back.
        doc = await db_service.get_document_status(document_id)
        assert doc is not None
        assert doc["active_run_id"] == "run-B"

    @pytest.mark.asyncio
    async def test_b_store_still_commits_after_a_claims_late(self, db_service: DatabaseService):
        """The user-visible guarantee, not just the internal column: even
        after A's claim arrives late, B's OWN store step (which claimed
        first, in this reproduction) must still be able to commit -- #110's
        promise that the newest content wins immediately must hold through
        the ordering fix, not just the row-ownership column."""
        from src.models.document import DocumentChunk, DocumentUploadMessage

        document_id = "doc_claim_order_2"
        t_a = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        t_b = t_a + timedelta(milliseconds=50)

        common_kwargs = dict(
            document_id=document_id,
            workspace_id="ws_fencing",
            user_id="user_fencing",
            filename="f.txt",
            original_filename="f.txt",
            content_type="text/plain",
            size_bytes=10,
            storage_backend="local",
            storage_path="workspaces/ws_fencing/f.txt",
        )

        await db_service.create_pending_document(
            workflow_run_id="run-B", workflow_start_time=t_b, **common_kwargs
        )
        # A's claim lands late, after B's.
        await db_service.create_pending_document(
            workflow_run_id="run-A", workflow_start_time=t_a, **common_kwargs
        )

        message = DocumentUploadMessage(
            event_type="document.uploaded",
            document_id=document_id,
            workspace_id="ws_fencing",
            user_id="user_fencing",
            filename="f.txt",
            original_filename="f.txt",
            content_type="text/plain",
            size_bytes=10,
            storage_backend="local",
            storage_path="workspaces/ws_fencing/f.txt",
            timestamp=datetime.now(UTC).isoformat(),
        )
        chunk = DocumentChunk(
            document_id=document_id, content="B content", chunk_index=0, start_char=0, end_char=9
        )

        # B's store commit must succeed -- NOT be fenced out by A's stale,
        # late-arriving (and, pre-fix, claim-stealing) row.
        doc_pk = await db_service.store_processed_document(
            message=message,
            chunks=[chunk],
            text_length=9,
            processing_time_ms=5,
            workflow_run_id="run-B",
        )
        assert doc_pk is not None, (
            "B (the legitimate newest run) was fenced out of its own store "
            "step by A's stale, late-arriving claim -- the exact inversion "
            "the follow-up review flagged"
        )
