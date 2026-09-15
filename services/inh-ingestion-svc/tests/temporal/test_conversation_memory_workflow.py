"""ConversationMemoryWorkflow end-to-end tests via Temporal's time-skipping
test env (#306).

Deliberately does NOT override conftest's autouse `cleanup_test_data`/
`db_service` fixtures, matching the existing
tests/temporal/test_chunk_edit_workflow.py / test_audit_workflow.py
convention in this repo -- these tests skip wherever Postgres isn't
reachable, standing in for the WorkflowEnvironment's own separate
requirement (an ephemeral Temporal test-server binary download from
temporal.download, blocked in this sandbox's proxy per
test_chunk_edit_workflow.py's own docstring). "Believed correct, unverified
in this sandbox" -- same standing caveat as that file: proven by CI, not by
this run.

What these tests avoid depending on: real Temporal `sleep`/time-skipping
semantics for the 90s size-or-idle debounce. Every scenario below drives the
flush deterministically instead -- either via the SIZE threshold (set low
enough that the very first turn crosses it) or the explicit `close` signal
(which sets `self._closed = True`, making `wait_condition` return
immediately regardless of the idle timer) -- so these tests exercise the
REAL flush pipeline ordering and REAL signal_with_start wiring without being
sensitive to timer-skipping edge cases.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

import pytest
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from src.temporal.models import (
    ChunkConversationInput,
    ChunkConversationOutput,
    CleanupStagingInput,
    ConversationMemoryInput,
    ConversationTurnSignal,
    EnsureTenantInput,
    EnsureTenantOutput,
    PublishCompletionInput,
    RedactTurnsInput,
    RedactTurnsOutput,
    StoreDocumentInput,
    StoreDocumentOutput,
    UpdateStatsInput,
)
from src.temporal.workflows.conversation_memory import ConversationMemoryWorkflow

TASK_QUEUE = "conversation-memory-test-queue"


def _input(**overrides) -> ConversationMemoryInput:
    defaults: dict[str, Any] = {
        "workspace_id": "ws1",
        "external_id": "conv1",
        "user_id": "user1",
        # Effectively "never idle-flush, never idle-finalize" unless a test
        # opts in -- every scenario below drives the flush via size or
        # `close` instead of waiting on a real timer.
        "flush_char_threshold": 4000,
        "flush_idle_seconds": 100_000,
        "continue_as_new_turns": 500,
        "idle_finalize_seconds": 100_000,
    }
    defaults.update(overrides)
    return ConversationMemoryInput(**defaults)


def _turn(**overrides) -> ConversationTurnSignal:
    defaults: dict[str, Any] = {
        "turn_id": "t1",
        "role": "user",
        "text": "hello there",
        "ts": "2026-08-31T00:00:00Z",
        "user_id": "user1",
        "client": "test-client",
    }
    defaults.update(overrides)
    return ConversationTurnSignal(**defaults)


# --- Mock activities --------------------------------------------------------


@activity.defn(name="ensure_tenant_ready")
async def mock_ensure_tenant_ready(input: EnsureTenantInput) -> EnsureTenantOutput:
    return EnsureTenantOutput(tenant_id=42, workspace_ready=True)


@activity.defn(name="redact_turns")
async def mock_redact_turns_noop(input: RedactTurnsInput) -> RedactTurnsOutput:
    """No redactions found -- turns pass through with role/turn_id intact,
    same output shape the real activity produces for benign text."""
    from src.temporal.models import RedactedTurn

    return RedactTurnsOutput(
        redacted_turns=[
            RedactedTurn(turn_id=t.turn_id, text=t.text, role=t.role, original_index=i)
            for i, t in enumerate(input.turns)
        ],
        dropped_turn_ids=[],
        redaction_counts={},
    )


class _RecordingChunkConversation:
    def __init__(self) -> None:
        self.calls: list[ChunkConversationInput] = []

    @activity.defn(name="chunk_conversation")
    async def __call__(self, input: ChunkConversationInput) -> ChunkConversationOutput:
        self.calls.append(input)
        return ChunkConversationOutput(chunk_count=len(input.redacted_turns))


class _RecordingStore:
    """Shared recorder for store_in_postgresql/store_in_weaviate mocks --
    proves `append=True` was actually threaded through to BOTH calls.

    `document_row_inserted_sequence` (#364): lets a test control, per flush,
    what the DB-verified insert/update signal (StoreDocumentOutput.
    document_row_inserted) reports -- e.g. `[True, True]` simulates the
    processed_documents row being deleted and re-created between two
    flushes of the SAME still-running workflow (the #364 scenario), vs.
    `[True, False]` for the ordinary case of a second flush growing the
    SAME row. Defaults to always-True (every flush looks like a fresh
    insert) for tests that don't care about this signal at all.
    """

    def __init__(self, document_row_inserted_sequence: list[bool] | None = None) -> None:
        self.pg_calls: list[StoreDocumentInput] = []
        self.wv_calls: list[StoreDocumentInput] = []
        self._document_row_inserted_sequence = list(
            document_row_inserted_sequence if document_row_inserted_sequence is not None else []
        )

    @activity.defn(name="store_in_postgresql")
    async def store_in_postgresql(self, input: StoreDocumentInput) -> StoreDocumentOutput:
        self.pg_calls.append(input)
        row_inserted = (
            self._document_row_inserted_sequence.pop(0)
            if self._document_row_inserted_sequence
            else True
        )
        return StoreDocumentOutput(
            success=True, chunks_stored=1, document_row_inserted=row_inserted
        )

    @activity.defn(name="store_in_weaviate")
    async def store_in_weaviate(self, input: StoreDocumentInput) -> StoreDocumentOutput:
        self.wv_calls.append(input)
        return StoreDocumentOutput(success=True, chunks_stored=1)


@activity.defn(name="update_workspace_stats")
async def mock_update_workspace_stats(input: UpdateStatsInput) -> None:
    return None


class _RecordingStats:
    """Records every UpdateStatsInput the workflow sends to
    update_workspace_stats -- #364's regression tests assert on
    `document_delta` here, which `mock_update_workspace_stats` above
    (used by the pre-existing tests, which don't care about this value)
    discards."""

    def __init__(self) -> None:
        self.calls: list[UpdateStatsInput] = []

    @activity.defn(name="update_workspace_stats")
    async def __call__(self, input: UpdateStatsInput) -> None:
        self.calls.append(input)
        return None


@activity.defn(name="cleanup_staging")
async def mock_cleanup_staging(input: CleanupStagingInput) -> None:
    return None


class _RecordingPublishCompletion:
    def __init__(self) -> None:
        self.calls: list[PublishCompletionInput] = []

    @activity.defn(name="publish_completion")
    async def __call__(self, input: PublishCompletionInput) -> None:
        self.calls.append(input)
        return None


def _activities(
    chunker: _RecordingChunkConversation, store: _RecordingStore, publisher, stats=None
):
    return [
        mock_ensure_tenant_ready,
        mock_redact_turns_noop,
        chunker.__call__,
        store.store_in_postgresql,
        store.store_in_weaviate,
        stats.__call__ if stats is not None else mock_update_workspace_stats,
        mock_cleanup_staging,
        publisher.__call__,
    ]


@pytest.mark.asyncio
async def test_close_signal_flushes_then_finalizes_with_append_true():
    """The full flush pipeline, driven deterministically by `close` instead
    of a real idle timer: redact -> chunk -> store(append=True) -> stats ->
    (loop exits because self._closed) -> finalize (publish_completion)."""
    chunker = _RecordingChunkConversation()
    store = _RecordingStore()
    publisher = _RecordingPublishCompletion()

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue=TASK_QUEUE,
            workflows=[ConversationMemoryWorkflow],
            activities=_activities(chunker, store, publisher),
        ):
            handle = await env.client.start_workflow(
                ConversationMemoryWorkflow.run,
                _input(),
                id="conv-close-test-1",
                task_queue=TASK_QUEUE,
                start_signal="add_turn",
                start_signal_args=[_turn(turn_id="t1", text="hello")],
            )
            await handle.signal(ConversationMemoryWorkflow.close)
            await handle.result()

    assert len(chunker.calls) == 1
    assert [t.turn_id for t in chunker.calls[0].redacted_turns] == ["t1"]

    assert len(store.pg_calls) == 1
    assert store.pg_calls[0].append is True
    assert store.pg_calls[0].document_type == "conversation"
    assert store.pg_calls[0].external_id == "conv1"
    assert len(store.wv_calls) == 1
    assert store.wv_calls[0].append is True

    assert len(publisher.calls) == 1
    assert publisher.calls[0].success is True


@pytest.mark.asyncio
async def test_size_threshold_flush_without_waiting_on_idle_timer():
    """A turn whose text alone crosses flush_char_threshold flushes on the
    NEXT wait_condition check -- no idle timeout needed."""
    chunker = _RecordingChunkConversation()
    store = _RecordingStore()
    publisher = _RecordingPublishCompletion()

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue=TASK_QUEUE,
            workflows=[ConversationMemoryWorkflow],
            activities=_activities(chunker, store, publisher),
        ):
            handle = await env.client.start_workflow(
                ConversationMemoryWorkflow.run,
                _input(flush_char_threshold=10),  # crossed by "hello there" (11 chars)
                id="conv-size-test-1",
                task_queue=TASK_QUEUE,
                start_signal="add_turn",
                start_signal_args=[_turn(turn_id="t1", text="hello there")],
            )

            # Poll get_status until the flush has actually happened -- the
            # workflow task processing the signal and the size-triggered
            # flush are not necessarily observable in the same tick.
            async def _flushed() -> bool:
                status = await handle.query(ConversationMemoryWorkflow.get_status)
                return bool(status["total_turns_flushed"] >= 1)

            for _ in range(50):
                if await _flushed():
                    break
                await asyncio.sleep(0.05)

            await handle.signal(ConversationMemoryWorkflow.close)
            await handle.result()

    assert len(chunker.calls) == 1
    assert len(store.pg_calls) == 1


@pytest.mark.asyncio
async def test_duplicate_turn_id_is_a_no_op():
    """A duplicate turn_id (retry / MQ redelivery) must not double-count
    toward the buffer or be chunked twice."""
    chunker = _RecordingChunkConversation()
    store = _RecordingStore()
    publisher = _RecordingPublishCompletion()

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue=TASK_QUEUE,
            workflows=[ConversationMemoryWorkflow],
            activities=_activities(chunker, store, publisher),
        ):
            handle = await env.client.start_workflow(
                ConversationMemoryWorkflow.run,
                _input(),
                id="conv-dedup-test-1",
                task_queue=TASK_QUEUE,
                start_signal="add_turn",
                start_signal_args=[_turn(turn_id="dup-1", text="first delivery")],
            )
            # Same turn_id delivered again (e.g. MQ at-least-once redelivery).
            await handle.signal(
                ConversationMemoryWorkflow.add_turn, _turn(turn_id="dup-1", text="first delivery")
            )

            status = await handle.query(ConversationMemoryWorkflow.get_status)
            assert status["buffered_turns"] == 1
            assert status["buffered_chars"] == len("first delivery")

            await handle.signal(ConversationMemoryWorkflow.close)
            await handle.result()

    # Only ONE turn ever reached the redact/chunk/store pipeline.
    assert len(chunker.calls[0].redacted_turns) == 1


@pytest.mark.asyncio
async def test_duplicate_turn_id_does_not_extend_idle_finalize_deadline():
    """A duplicate turn_id signal (MQ redelivery / client retry) must NOT
    push out the idle-finalize deadline -- `idle_finalize_seconds` counts
    "no NEW turns" (module docstring), and a rejected duplicate is not a new
    turn.

    Regression test for the bug where `add_turn` stamped
    `_last_activity_time = workflow.now()` BEFORE the duplicate-`turn_id`
    check returned early, so even a no-op redelivery reset the clock the
    idle-finalize wait consumes.

    Timeline (turn1 real at t=0, duplicate at t=15, debounce flush at
    t=30, then idle-wait to finalize):
      - Fixed:  elapsed-at-flush = 30 - 0  = 30 -> remaining = 170 -> finalize 170s after the flush
      - Buggy:  elapsed-at-flush = 30 - 15 = 15 -> remaining = 185 -> finalize 185s after the flush
    Measured via `workflow.now()` timestamps the workflow itself stamps
    onto its own activity inputs (the flush's `last_flushed_at` and the
    finalize's `publish_completion` timestamp) rather than the test
    environment's wall clock -- `env.get_current_time()` does not reliably
    reflect the workflow's simulated time once a run has completed, but
    these workflow-authored timestamps always do.
    """
    chunker = _RecordingChunkConversation()
    store = _RecordingStore()
    publisher = _RecordingPublishCompletion()

    flush_idle_seconds = 30
    idle_finalize_seconds = 200

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue=TASK_QUEUE,
            workflows=[ConversationMemoryWorkflow],
            activities=_activities(chunker, store, publisher),
        ):
            handle = await env.client.start_workflow(
                ConversationMemoryWorkflow.run,
                _input(
                    # Never crossed by "hello there" alone -- turn1 flushes
                    # via the flush_idle_seconds debounce timeout instead of
                    # the size threshold, so it sits buffered for a while,
                    # giving the duplicate a window to (wrongly) bump the
                    # clock before the flush-triggered idle-branch recompute.
                    flush_char_threshold=100_000,
                    flush_idle_seconds=flush_idle_seconds,
                    idle_finalize_seconds=idle_finalize_seconds,
                ),
                id="conv-dup-idle-test-1",
                task_queue=TASK_QUEUE,
                start_signal="add_turn",
                start_signal_args=[_turn(turn_id="t1", text="hello there")],
            )

            # Redelivery of the SAME turn_id partway through the debounce
            # wait -- a genuine duplicate, must be a no-op.
            await env.sleep(15)
            await handle.signal(
                ConversationMemoryWorkflow.add_turn, _turn(turn_id="t1", text="hello there")
            )

            await handle.result()

    flush_ts = datetime.fromisoformat(store.pg_calls[0].metadata["last_flushed_at"])
    finalize_ts = datetime.fromisoformat(publisher.calls[0].timestamp)
    idle_wait_seconds = (finalize_ts - flush_ts).total_seconds()

    # Fixed behavior waits ~170s after the flush; the pre-fix bug stretches
    # it to ~185s. 178s sits squarely between the two predictions.
    assert idle_wait_seconds < 178, (
        f"idle-finalize waited {idle_wait_seconds:.1f}s after the flush -- the duplicate "
        "signal appears to have extended the idle-finalize deadline (expected ~170s, not ~185s)"
    )

    # The duplicate itself must still be a no-op on the actual pipeline.
    assert len(chunker.calls[0].redacted_turns) == 1


async def _run_two_flushes(store: _RecordingStore, stats: _RecordingStats) -> None:
    """Drive TWO deterministic size-triggered flushes of the same running
    workflow (no continue_as_new in between -- matches #364's scenario of a
    delete landing on a conversation whose workflow keeps running), then
    close. Shared by both tests below so only `store`'s
    document_row_inserted_sequence differs between "delete-then-reflush"
    and "ordinary second flush"."""
    chunker = _RecordingChunkConversation()
    publisher = _RecordingPublishCompletion()

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue=TASK_QUEUE,
            workflows=[ConversationMemoryWorkflow],
            activities=_activities(chunker, store, publisher, stats=stats),
        ):
            handle = await env.client.start_workflow(
                ConversationMemoryWorkflow.run,
                # Low threshold: each individual turn's text alone crosses
                # it, so each add_turn triggers its OWN flush deterministically.
                _input(flush_char_threshold=10),
                id="conv-document-delta-test",
                task_queue=TASK_QUEUE,
                start_signal="add_turn",
                start_signal_args=[_turn(turn_id="t1", text="first turn crosses threshold")],
            )

            async def _flushed(n: int) -> bool:
                status = await handle.query(ConversationMemoryWorkflow.get_status)
                return bool(status["total_turns_flushed"] >= n)

            for _ in range(50):
                if await _flushed(1):
                    break
                await asyncio.sleep(0.05)
            assert await _flushed(1), "first flush never happened"

            # A later turn on the SAME still-running workflow -- exactly
            # what a client resumes with after DELETE /v1/conversations/
            # {external_id} removed the processed_documents row without
            # terminating this workflow (that endpoint deliberately does
            # not terminate it -- see conversation_memory.py's #364 comment).
            await handle.signal(
                ConversationMemoryWorkflow.add_turn,
                _turn(turn_id="t2", text="second turn also crosses threshold"),
            )
            for _ in range(50):
                if await _flushed(2):
                    break
                await asyncio.sleep(0.05)
            assert await _flushed(2), "second flush never happened"

            await handle.signal(ConversationMemoryWorkflow.close)
            await handle.result()

    assert len(store.pg_calls) == 2
    assert len(stats.calls) == 2


class TestDocumentDeltaFromDbInsertSignal:
    """#364 regression: document_delta must come from the database's own
    insert/update signal (StoreDocumentOutput.document_row_inserted), not
    from the workflow's own `self._document_created` memory -- which is
    exactly what goes stale across a delete-then-reflush (see
    conversation_memory.py's `_flush` for the full mechanism)."""

    @pytest.mark.asyncio
    async def test_delete_then_reflush_still_counts_the_new_row(self):
        """Both flushes' Postgres upserts report an INSERT (the DB's view:
        flush 1 creates the row, something deletes it out from under the
        still-running workflow, flush 2 creates it again from scratch).

        Under the OLD bug, flush 2's `document_delta` would be computed from
        `not self._document_created`, which is False by flush 2 (it was set
        True after flush 1 and never reset) -- producing document_delta=0
        for a flush that the database says just INSERTed a brand-new row.
        This is the exact under-count issue #364 reports. The fix must
        produce document_delta=1 for BOTH flushes here.
        """
        store = _RecordingStore(document_row_inserted_sequence=[True, True])
        stats = _RecordingStats()

        await _run_two_flushes(store, stats)

        assert stats.calls[0].document_delta == 1, "first flush must count its insert"
        assert stats.calls[1].document_delta == 1, (
            "second flush's INSERT (row recreated after a delete) must also count -- "
            "this is exactly what the stale self._document_created flag used to zero out"
        )

    @pytest.mark.asyncio
    async def test_ordinary_second_flush_on_the_same_row_does_not_double_count(self):
        """The existing idempotency guarantee, preserved under the new
        mechanism: when the row was NOT deleted, flush 2's upsert goes
        through DO UPDATE (document_row_inserted=False), and document_delta
        must stay 0 -- a repeated/ordinary flush of the same conversation
        must not inflate workspace_metadata.document_count."""
        store = _RecordingStore(document_row_inserted_sequence=[True, False])
        stats = _RecordingStats()

        await _run_two_flushes(store, stats)

        assert stats.calls[0].document_delta == 1, "first flush creates the row"
        assert stats.calls[1].document_delta == 0, "second flush only grows the existing row"
