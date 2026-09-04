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
            RedactedTurn(turn_id=t.turn_id, text=t.text, role=t.role) for t in input.turns
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
    proves `append=True` was actually threaded through to BOTH calls."""

    def __init__(self) -> None:
        self.pg_calls: list[StoreDocumentInput] = []
        self.wv_calls: list[StoreDocumentInput] = []

    @activity.defn(name="store_in_postgresql")
    async def store_in_postgresql(self, input: StoreDocumentInput) -> StoreDocumentOutput:
        self.pg_calls.append(input)
        return StoreDocumentOutput(success=True, chunks_stored=1)

    @activity.defn(name="store_in_weaviate")
    async def store_in_weaviate(self, input: StoreDocumentInput) -> StoreDocumentOutput:
        self.wv_calls.append(input)
        return StoreDocumentOutput(success=True, chunks_stored=1)


@activity.defn(name="update_workspace_stats")
async def mock_update_workspace_stats(input: UpdateStatsInput) -> None:
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


def _activities(chunker: _RecordingChunkConversation, store: _RecordingStore, publisher):
    return [
        mock_ensure_tenant_ready,
        mock_redact_turns_noop,
        chunker.__call__,
        store.store_in_postgresql,
        store.store_in_weaviate,
        mock_update_workspace_stats,
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
