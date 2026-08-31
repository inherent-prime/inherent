"""ConversationMemoryWorkflow: signal-driven conversation ingestion (#306).

Why not DocumentIngestionWorkflow / upload_document
-------------------------------------------------------
A conversation is append-only and grows. Re-uploading it whole after every
turn either duplicates documents or forces a full re-chunk/re-embed of the
entire history on every turn; uploading each turn as its own document floods
the embedding pipeline with tiny jobs (#228-#231: TEI saturates at ~83
concurrent documents); and generic chunking splits mid-turn, destroying the
speaker attribution that makes a conversation chunk useful at retrieval
time. This workflow exists to give conversations their own shape instead of
forcing them through the file-shaped path.

Identity
-----------
``workflow_id = conv-{workspace_id}-{external_id}`` (see
``conversation_identity.py``) makes starting the workflow idempotent by
construction: ``conversation_trigger.py`` calls ``signal_with_start`` with
``WorkflowIDConflictPolicy.USE_EXISTING`` (NOT ``TERMINATE_EXISTING`` like
``trigger.py``'s document path) -- a later turn must add itself to the SAME
running conversation, never kill and restart it.

The flush pipeline -- a security property, not just an ordering convenience
-------------------------------------------------------------------------------
::

    buffered turns -> redact_turns -> chunk_conversation -> store(append=True) -> update_stats

``redact_turns`` (#307, ``src/temporal/activities/redact.py``) is the ONLY
place raw, pre-redaction turn text may exist. ``chunk_conversation`` (#306,
``src/temporal/activities/conversation_chunk.py``) reads turn TEXT
exclusively from ``redact_turns``'s output (``RedactTurnsOutput.
redacted_turns``) -- `_flush` below builds ``ChunkConversationInput`` from
THAT output, never from ``self._buffer`` (the workflow's own raw,
pre-redaction turn state). ``self._buffer`` is used ONLY to source `ts`/
`client` (non-text, non-sensitive metadata `RedactedTurn` doesn't carry) by
`turn_id` lookup, and is discarded (reassigned to `[]`) the moment a flush
starts. See ``redact.py``'s module docstring ("THE SHARPEST EDGE") for why
this specific wiring is the whole point of #307 existing.

Debounce (the embedding-pipeline protection)
-------------------------------------------------
Flushes on size OR idle, whichever comes first
(``CONVERSATION_FLUSH_CHAR_THRESHOLD`` / ``CONVERSATION_FLUSH_IDLE_SECONDS``,
both configurable -- resolved from settings by ``conversation_trigger.py``
and carried on ``ConversationMemoryInput``, never read via ``get_settings()``
inside this workflow; see that model's docstring) -- one store batch per
conversation per flush instead of one per turn, which is what keeps 500
turns delivered over an hour from producing more than one embed batch per
flush window.

Turn dedup
-------------
A duplicate ``turn_id`` (MQ at-least-once redelivery, or a client retry) is
a no-op: ``add_turn`` checks a bounded, most-recently-seen ``turn_id`` set
before buffering -- no per-item dedup inside a batch payload is needed
because the MQ layer publishes ONE message per turn (see
``ConversationTurnMessage``'s docstring, ``inh_contracts.events``).

continue_as_new / idle finalize
------------------------------------
``continue_as_new`` fires every ``conversation_continue_as_new_turns`` turns
(default 500) to bound Temporal history size, carrying forward the tenant
id, whether the document row has been created yet, and the bounded seen-
turn-id set (``ConversationMemoryInput``) so the new run stays behaviorally
continuous. ``conversation_idle_finalize_hours`` (default 24) of no new
turns finalizes the conversation: publish
``core.document.processed.v1`` and let the workflow run complete, rather
than waiting forever.

Reuse, not fork
-------------------
``store_in_postgresql``/``store_in_weaviate`` are the SAME activities
``DocumentIngestionWorkflow`` uses, called here with
``StoreDocumentInput.append=True`` (see that field's docstring for why an
unmodified, unconditional full-replace call on every flush would silently
destroy every previously-flushed turn's chunks from the second flush on).
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from src.temporal.activities.cleanup import cleanup_staging
    from src.temporal.activities.completion import publish_completion
    from src.temporal.activities.conversation_chunk import chunk_conversation
    from src.temporal.activities.redact import redact_turns
    from src.temporal.activities.store import store_in_postgresql, store_in_weaviate
    from src.temporal.activities.tenant import ensure_tenant_ready, update_workspace_stats
    from src.temporal.conversation_identity import conversation_document_id
    from src.temporal.models import (
        ChunkConversationInput,
        CleanupStagingInput,
        ConversationMemoryInput,
        ConversationTurnMeta,
        ConversationTurnSignal,
        EnsureTenantInput,
        PublishCompletionInput,
        RedactTurnInput,
        RedactTurnsInput,
        StoreDocumentInput,
        UpdateStatsInput,
    )

# Synthetic-but-real values that satisfy processed_documents' file-shaped NOT
# NULL/CHECK constraints (migration 001) for a document that has no actual
# uploaded file -- see migration 020's comment for why these constraints are
# deliberately NOT relaxed instead.
CONVERSATION_CONTENT_TYPE = "application/x-inherent-conversation"
CONVERSATION_STORAGE_BACKEND = "local"

# Bound on ConversationMemoryWorkflow._seen_turn_ids -- "a bounded set[str]"
# per the issue. 2000 comfortably covers several flush cycles' worth of
# recent turn_ids (default flush threshold 4000 chars, typical turns are a
# few hundred chars) without growing workflow history/state unboundedly.
_SEEN_TURN_IDS_BOUND = 2000


@dataclass
class _BufferedTurn:
    """One turn buffered in workflow state, PRE-redaction (#306).

    Deliberately workflow-local (not a model in models.py, never crosses an
    activity boundary as-is): `text` here is the raw, unredacted turn text --
    see the module docstring's "flush pipeline" section for why this must
    never be read by anything downstream of `redact_turns`.
    """

    turn_id: str
    role: str
    text: str
    ts: str
    client: str | None = None


@workflow.defn
class ConversationMemoryWorkflow:
    """Durable, signal-driven conversation ingestion workflow (#306).

    Started (and re-signaled on every later turn) via
    `signal_with_start`/`conversation_trigger.py`; never started directly by
    a caller expecting a return value the way `DocumentIngestionWorkflow` is
    -- there is no synchronous "wait for this turn to be stored" contract,
    matching `POST /v1/conversations/{external_id}/turns`'s `202 Accepted`.
    """

    def __init__(self) -> None:
        self._workspace_id: str = ""
        self._external_id: str = ""
        self._user_id: str = ""
        self._document_id: str = ""
        self._tenant_id: int | None = None
        self._document_created: bool = False

        self._buffer: list[_BufferedTurn] = []
        self._buffered_chars: int = 0
        self._total_turns_flushed: int = 0
        self._closed: bool = False

        # OrderedDict used as an insertion-ordered SET (values unused) so a
        # bound can be enforced with O(1) eviction of the OLDEST id --
        # `set[str]` alone has no ordering to evict by.
        self._seen_turn_ids: OrderedDict[str, None] = OrderedDict()

        self._last_activity_time: datetime | None = None  # set from workflow.now() in run()

    @workflow.query
    def get_status(self) -> dict:
        """Query current workflow status (mirrors DocumentIngestionWorkflow's)."""
        return {
            "external_id": self._external_id,
            "buffered_turns": len(self._buffer),
            "buffered_chars": self._buffered_chars,
            "total_turns_flushed": self._total_turns_flushed,
            "document_created": self._document_created,
        }

    @workflow.signal
    async def add_turn(self, turn: ConversationTurnSignal) -> None:
        """Buffer one turn (#306). A duplicate `turn_id` is a no-op.

        The first call also carries the initial identity via
        `signal_with_start`'s companion `run()` args -- this handler only
        ever appends to the buffer, it never sets workspace_id/external_id
        (those come from `ConversationMemoryInput`, see `run()`).
        """
        self._last_activity_time = workflow.now()

        if turn.turn_id in self._seen_turn_ids:
            workflow.logger.debug(
                "ConversationMemoryWorkflow: duplicate turn_id ignored (no-op)",
                turn_id=turn.turn_id,
            )
            return

        self._seen_turn_ids[turn.turn_id] = None
        if len(self._seen_turn_ids) > _SEEN_TURN_IDS_BOUND:
            self._seen_turn_ids.popitem(last=False)  # evict oldest

        self._user_id = turn.user_id
        self._buffer.append(
            _BufferedTurn(
                turn_id=turn.turn_id, role=turn.role, text=turn.text, ts=turn.ts, client=turn.client
            )
        )
        self._buffered_chars += len(turn.text)

    @workflow.signal
    async def close(self) -> None:
        """Explicit finalize signal (bonus over the issue's stated triggers:
        size/idle debounce flush + 24h idle finalize). Flushes any buffered
        turns, then completes the workflow instead of waiting for the next
        signal or the 24h idle deadline."""
        self._closed = True

    @workflow.run
    async def run(self, input: ConversationMemoryInput) -> None:
        """Buffer turns; flush on size-or-idle debounce; finalize on 24h
        idle or `close`; continue_as_new every N turns.

        Debounce/lifecycle thresholds come from `input` (resolved by
        `conversation_trigger.py` OUTSIDE the workflow sandbox), never from
        `get_settings()` called directly in here -- see
        `ConversationMemoryInput`'s docstring for why that is a Temporal
        determinism anti-pattern (#38) this workflow must not repeat.
        """
        flush_char_threshold = input.flush_char_threshold
        flush_idle_seconds = input.flush_idle_seconds
        continue_as_new_turns = input.continue_as_new_turns
        idle_finalize_seconds = input.idle_finalize_seconds

        self._workspace_id = input.workspace_id
        self._external_id = input.external_id
        self._user_id = input.user_id
        self._document_id = conversation_document_id(input.workspace_id, input.external_id)
        self._tenant_id = input.tenant_id
        self._document_created = input.document_created
        for turn_id in input.seen_turn_ids:
            self._seen_turn_ids[turn_id] = None
        # workflow.now() is deterministic/replay-safe and continuous across
        # continue_as_new, so activity always resets the idle clock at start
        # of a run -- input.last_activity_iso is carried only for
        # observability (get_status), never used to compute the idle
        # deadline (a run that continue_as_new'd because it hit the TURN
        # count boundary, not because it went idle, must not immediately
        # look close to the 24h deadline).
        self._last_activity_time = workflow.now()

        if self._tenant_id is None:
            tenant_output = await workflow.execute_activity(
                ensure_tenant_ready,
                EnsureTenantInput(
                    workspace_id=self._workspace_id,
                    user_id=self._user_id,
                    workflow_run_id=workflow.info().run_id,
                    document_id=self._document_id,
                ),
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=RetryPolicy(
                    maximum_attempts=3,
                    initial_interval=timedelta(seconds=1),
                    maximum_interval=timedelta(seconds=10),
                    backoff_coefficient=2.0,
                ),
            )
            self._tenant_id = tenant_output.tenant_id

        turns_since_continue = 0

        while True:
            if not self._buffer:
                # Idle: nothing buffered. Wait for the next turn, `close`, or
                # the 24h-idle-since-last-activity deadline -- whichever
                # comes first. A single long wait here (bounded by the
                # REMAINING idle budget) instead of polling every
                # flush_idle_seconds keeps a genuinely idle conversation from
                # accumulating a timer per debounce interval for 24h.
                elapsed = (workflow.now() - self._last_activity_time).total_seconds()
                remaining = idle_finalize_seconds - elapsed
                if remaining <= 0:
                    break  # 24h idle finalize
                try:
                    await workflow.wait_condition(
                        lambda: bool(self._buffer) or self._closed,
                        timeout=timedelta(seconds=remaining),
                    )
                except TimeoutError:
                    break  # 24h idle finalize
                if self._closed and not self._buffer:
                    break
                continue

            # Buffer has content: debounce on size OR idle, whichever first.
            try:
                await workflow.wait_condition(
                    lambda: self._buffered_chars >= flush_char_threshold or self._closed,
                    timeout=timedelta(seconds=flush_idle_seconds),
                )
            except TimeoutError:
                pass  # idle debounce elapsed -- flush whatever is buffered

            flushed_turns = await self._flush()
            turns_since_continue += flushed_turns

            if self._closed:
                break

            if turns_since_continue >= continue_as_new_turns:
                workflow.continue_as_new(
                    ConversationMemoryInput(
                        workspace_id=self._workspace_id,
                        external_id=self._external_id,
                        user_id=self._user_id,
                        tenant_id=self._tenant_id,
                        document_created=self._document_created,
                        seen_turn_ids=list(self._seen_turn_ids.keys()),
                        flush_char_threshold=flush_char_threshold,
                        flush_idle_seconds=flush_idle_seconds,
                        continue_as_new_turns=continue_as_new_turns,
                        idle_finalize_seconds=idle_finalize_seconds,
                    )
                )

        await self._finalize()

    async def _flush(self) -> int:
        """Run the redact -> chunk -> store(append=True) -> update_stats
        pipeline for the currently buffered turns. Returns the number of
        turns that were in this flush (buffered, not necessarily all
        successfully redacted -- a dropped turn per #307 still counts as
        "handled" so continue_as_new's turn count stays meaningful)."""
        turns_to_flush = self._buffer
        self._buffer = []
        self._buffered_chars = 0

        if not turns_to_flush:
            return 0

        run_id = workflow.info().run_id

        # --- redact_turns: the ONLY step allowed to see raw turn text -----
        redact_output = await workflow.execute_activity(
            redact_turns,
            RedactTurnsInput(
                turns=[
                    RedactTurnInput(turn_id=t.turn_id, text=t.text, role=t.role)
                    for t in turns_to_flush
                ],
                workflow_run_id=run_id,
                workspace_id=self._workspace_id,
                document_id=self._document_id,
            ),
            start_to_close_timeout=timedelta(seconds=30),
            # Belt-and-suspenders (redact.py's own module docstring, guard 2
            # of 2): redact_turns is non_retryable=True on any activity-level
            # failure regardless, but pinning maximum_attempts=1 here means a
            # future change to that guard, or a copy-pasted call site, still
            # cannot retry a redaction failure.
            retry_policy=RetryPolicy(maximum_attempts=1),
        )

        if not redact_output.redacted_turns:
            # Every turn in this flush failed redaction and was dropped
            # (audited by redact_turns itself) -- nothing left to chunk or
            # store. Still counts toward continue_as_new's turn budget.
            return len(turns_to_flush)

        # ts/client are non-text, non-sensitive -- sourced from the RAW
        # buffer by turn_id, never turn text (see module docstring).
        buffered_by_id = {t.turn_id: t for t in turns_to_flush}
        turn_meta = [
            ConversationTurnMeta(
                turn_id=rt.turn_id,
                ts=buffered_by_id[rt.turn_id].ts,
                client=buffered_by_id[rt.turn_id].client,
            )
            for rt in redact_output.redacted_turns
            if rt.turn_id in buffered_by_id
        ]

        chunk_output = await workflow.execute_activity(
            chunk_conversation,
            ChunkConversationInput(
                workflow_run_id=run_id,
                document_id=self._document_id,
                workspace_id=self._workspace_id,
                redacted_turns=redact_output.redacted_turns,
                turn_meta=turn_meta,
            ),
            start_to_close_timeout=timedelta(seconds=60),
            retry_policy=RetryPolicy(
                maximum_attempts=2,
                initial_interval=timedelta(seconds=1),
                maximum_interval=timedelta(seconds=10),
                backoff_coefficient=2.0,
            ),
        )

        if chunk_output.chunk_count == 0:
            return len(turns_to_flush)

        flush_text_length = sum(len(rt.text) for rt in redact_output.redacted_turns)
        # size_bytes must stay > 0 (processed_documents' CHECK, migration
        # 001) -- always true here since this branch only runs when at least
        # one turn redacted successfully, i.e. flush_text_length >= 1.
        flush_size_bytes = max(flush_text_length, 1)

        self._total_turns_flushed += len(turns_to_flush)

        store_input = StoreDocumentInput(
            workflow_run_id=run_id,
            document_id=self._document_id,
            workspace_id=self._workspace_id,
            user_id=self._user_id,
            filename=self._document_id,
            original_filename=self._external_id,
            content_type=CONVERSATION_CONTENT_TYPE,
            size_bytes=flush_size_bytes,
            storage_backend=CONVERSATION_STORAGE_BACKEND,
            storage_path=f"conversation://{self._workspace_id}/{self._external_id}",
            text_length=flush_text_length,
            processing_time_ms=0,
            tenant_id=self._tenant_id,
            append=True,
            document_type="conversation",
            external_id=self._external_id,
            metadata={
                "turn_count": self._total_turns_flushed,
                "last_flushed_at": workflow.now().isoformat(),
            },
        )

        # Captured BEFORE the store calls: whether THIS flush is the one
        # that creates the conversation's processed_documents row (workspace
        # stats' document_delta must count it exactly once, ever -- not
        # once per flush, and not again after a continue_as_new resets
        # workflow-local counters but NOT `self._document_created`, which
        # `run()` restores from `ConversationMemoryInput.document_created`).
        was_already_created = self._document_created

        pg_task = workflow.execute_activity(
            store_in_postgresql,
            store_input,
            start_to_close_timeout=timedelta(seconds=60),
            retry_policy=RetryPolicy(
                maximum_attempts=5,
                initial_interval=timedelta(seconds=2),
                maximum_interval=timedelta(seconds=30),
                backoff_coefficient=2.0,
            ),
        )
        wv_task = workflow.execute_activity(
            store_in_weaviate,
            store_input,
            start_to_close_timeout=timedelta(minutes=2),
            heartbeat_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(
                maximum_attempts=5,
                initial_interval=timedelta(seconds=5),
                maximum_interval=timedelta(seconds=60),
                backoff_coefficient=2.0,
            ),
        )
        pg_result, wv_result = await asyncio.gather(pg_task, wv_task)

        if pg_result.success and not self._document_created:
            self._document_created = True

        await workflow.execute_activity(
            update_workspace_stats,
            UpdateStatsInput(
                workspace_id=self._workspace_id,
                # Only the FIRST successful flush ever creates the
                # conversation's one processed_documents row; every later
                # flush grows the SAME row (append=True), so document_delta
                # must not be re-counted on every flush the way
                # DocumentIngestionWorkflow counts it once per whole-document
                # run.
                document_delta=1 if (pg_result.success and not was_already_created) else 0,
                chunk_delta=(
                    chunk_output.chunk_count if (pg_result.success or wv_result.success) else 0
                ),
                size_delta=flush_size_bytes if pg_result.success else 0,
                workflow_run_id=run_id,
                document_id=self._document_id,
            ),
            start_to_close_timeout=timedelta(seconds=15),
            retry_policy=RetryPolicy(
                maximum_attempts=3,
                initial_interval=timedelta(seconds=1),
                maximum_interval=timedelta(seconds=5),
                backoff_coefficient=2.0,
            ),
        )

        # Staging is scoped by workflow_run_id, which stays constant across
        # MULTIPLE flushes within one run (only continue_as_new changes it) --
        # clean up after EVERY flush, not just at the end, or a later flush's
        # write_chunks would overwrite this flush's already-consumed staging
        # row before it was ever cleaned (harmless in practice since it's
        # already been read, but leaves rows to accumulate for staging's
        # periodic sweep to catch instead of being cleaned promptly).
        try:
            await workflow.execute_activity(
                cleanup_staging,
                CleanupStagingInput(workflow_run_id=run_id),
                start_to_close_timeout=timedelta(seconds=15),
                retry_policy=RetryPolicy(maximum_attempts=2),
            )
        except Exception:
            workflow.logger.warning("ConversationMemoryWorkflow: failed to clean up staging")

        return len(turns_to_flush)

    async def _finalize(self) -> None:
        """Idle-24h or `close` finalize: publish completion, let the
        workflow run complete (#306: "Idle 24h -> finalize, publish
        core.document.processed.v1, complete")."""
        try:
            await workflow.execute_activity(
                publish_completion,
                PublishCompletionInput(
                    document_id=self._document_id,
                    workspace_id=self._workspace_id,
                    user_id=self._user_id,
                    filename=self._document_id,
                    original_filename=self._external_id,
                    content_type=CONVERSATION_CONTENT_TYPE,
                    size_bytes=0,
                    storage_backend=CONVERSATION_STORAGE_BACKEND,
                    storage_path=f"conversation://{self._workspace_id}/{self._external_id}",
                    timestamp=workflow.now().isoformat(),
                    success=self._document_created,
                    chunks_created=0,
                    processing_time_ms=0,
                ),
                start_to_close_timeout=timedelta(seconds=15),
                retry_policy=RetryPolicy(
                    maximum_attempts=3,
                    initial_interval=timedelta(seconds=1),
                    maximum_interval=timedelta(seconds=10),
                    backoff_coefficient=2.0,
                ),
            )
        except Exception:
            workflow.logger.warning(
                "ConversationMemoryWorkflow: failed to publish completion event (non-fatal)"
            )
