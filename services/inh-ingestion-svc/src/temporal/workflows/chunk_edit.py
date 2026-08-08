"""Temporal workflow for editing a single chunk.

Updates content in PostgreSQL (truth) and re-embeds in Weaviate (memory).
"""

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from src.temporal.models import (
        CHUNK_EDIT_COMPENSATION_MAX_ATTEMPTS,
        ChunkEditInput,
        ChunkEditResult,
        ChunkEditWeaviateFailureInput,
    )


@workflow.defn
class ChunkEditWorkflow:
    """Edit a single chunk's content across all stores."""

    @workflow.run
    async def run(self, input: ChunkEditInput) -> ChunkEditResult:
        # 1. Update PostgreSQL (authoritative)
        try:
            await workflow.execute_activity(
                "update_chunk_postgresql",
                input,
                start_to_close_timeout=timedelta(seconds=30),
            )
        except Exception as e:
            return ChunkEditResult(
                document_id=input.document_id,
                chunk_index=input.chunk_index,
                success=False,
                error=f"PostgreSQL update failed: {e}",
            )

        # 2. Update Weaviate. PG already holds the NEW content at this
        # point, so a Weaviate failure here is a PG/vector divergence
        # (#137), not a cosmetic miss: semantic search would keep matching
        # the OLD text/vector indefinitely with no signal to anyone. Bounded
        # retries (matching store_in_weaviate's policy) give a transient
        # TEI/Weaviate hiccup a chance to clear. update_chunk_weaviate
        # re-raises on failure (it used to swallow into `return False`,
        # which is a *completed* activity to Temporal -- the RetryPolicy
        # below never got to run at all).
        try:
            await workflow.execute_activity(
                "update_chunk_weaviate",
                input,
                start_to_close_timeout=timedelta(seconds=60),
                retry_policy=RetryPolicy(
                    maximum_attempts=3,
                    initial_interval=timedelta(seconds=2),
                    maximum_interval=timedelta(seconds=10),
                    backoff_coefficient=2.0,
                ),
            )
        except Exception as e:
            # workflow.execute_activity wraps the activity's real exception
            # in an ActivityError whose own message is always the generic,
            # hardcoded "Activity task failed" -- the SDK puts the actual
            # cause (e.g. the ConnectionError update_chunk_weaviate raised)
            # on `.cause`. Interpolating `e` directly (as this line
            # originally did) throws away all diagnostic content: the 5xx
            # body and the compensating ingestion_events row below would
            # both read "...failed after retries: Activity task failed" for
            # every failure, indistinguishable from each other.
            cause_message = str(getattr(e, "cause", None) or e)
            error_message = (
                f"PostgreSQL updated but the Weaviate re-embed failed after "
                f"retries: {cause_message}. Search results for this chunk "
                "may return stale content/vector until a retry succeeds."
            )

            # Compensating "mark-failed" (#137): a durable, queryable signal
            # (GET /lineage/{document_id}) that this divergence exists, so
            # it's discoverable even if the caller doesn't act on the 5xx
            # this workflow is about to report. Routed through an explicit
            # bounded RetryPolicy -- never a bare call. The activity itself
            # now RAISES on failure (see its docstring -- #99: a compensation
            # that swallows its own error is the exact defect it exists to
            # prevent, reintroduced one level up) so this RetryPolicy is not
            # dead code. This outer try/except is what makes raising safe:
            # it catches the fully-exhausted failure and logs it without
            # ever masking or replacing the REAL error already captured in
            # `error_message` above.
            try:
                await workflow.execute_activity(
                    "record_chunk_edit_weaviate_failure",
                    ChunkEditWeaviateFailureInput(
                        workflow_id=workflow.info().workflow_id,
                        document_id=input.document_id,
                        workspace_id=input.workspace_id,
                        chunk_index=input.chunk_index,
                        error_message=cause_message,
                    ),
                    start_to_close_timeout=timedelta(seconds=10),
                    retry_policy=RetryPolicy(
                        maximum_attempts=CHUNK_EDIT_COMPENSATION_MAX_ATTEMPTS,
                        initial_interval=timedelta(seconds=1),
                        maximum_interval=timedelta(seconds=3),
                    ),
                )
            except Exception as record_err:
                # Both the Weaviate failure AND its compensating write have
                # now failed -- the PG/vector divergence this workflow
                # detected is recorded nowhere. That is the scenario
                # docs/developer/learnings.md's #99 entry calls "loud
                # exhaustion": CRITICAL, not a plain warning, so it can't
                # blend into routine noise.
                workflow.logger.critical(
                    "Chunk-edit compensation exhausted: PG/vector divergence "
                    f"for document {input.document_id} chunk {input.chunk_index} "
                    f"was never recorded (compensation error: {record_err}; "
                    f"original error: {cause_message})"
                )

            return ChunkEditResult(
                document_id=input.document_id,
                chunk_index=input.chunk_index,
                success=False,
                error=error_message,
            )

        return ChunkEditResult(
            document_id=input.document_id,
            chunk_index=input.chunk_index,
            success=True,
        )
