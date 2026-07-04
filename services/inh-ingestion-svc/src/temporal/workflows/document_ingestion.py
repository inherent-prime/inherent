"""Document ingestion workflow orchestrating the full processing pipeline.

This workflow coordinates the following steps:
1. Ensure tenant infrastructure is ready
2. Validate document exists in storage
3. Extract text from document (fetches from storage, writes to staging)
4. Chunk text (reads from staging, writes to staging)
5. Store in PostgreSQL and Weaviate (reads from staging, parallel)
6. Update workspace statistics
7. Clean up staging data
"""

import asyncio
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

# Import activities using workflow.unsafe.imports_passed_through()
# This is required because activities run in a separate context.
# Import directly from submodules (not through __init__.py) to avoid
# sandbox import issues where nested package imports may fail silently.
with workflow.unsafe.imports_passed_through():
    from src.temporal.activities.chunk import chunk_text
    from src.temporal.activities.cleanup import cleanup_staging
    from src.temporal.activities.dead_letter import record_dead_letter
    from src.temporal.activities.extract import extract_text
    from src.temporal.activities.fetch import fetch_document
    from src.temporal.activities.status import create_pending_document, set_document_status
    from src.temporal.activities.store import store_in_postgresql, store_in_weaviate
    from src.temporal.activities.tenant import ensure_tenant_ready, update_workspace_stats
    from src.temporal.models import (
        ChunkTextInput,
        CleanupStagingInput,
        CreatePendingDocumentInput,
        DocumentIngestionInput,
        EnsureTenantInput,
        ExtractTextInput,
        FetchDocumentInput,
        RecordDeadLetterInput,
        SetDocumentStatusInput,
        StoreDocumentInput,
        UpdateStatsInput,
        WorkflowResult,
    )


@workflow.defn
class DocumentIngestionWorkflow:
    """Durable workflow for document ingestion processing.

    This workflow provides:
    - Per-step retry policies with configurable timeouts
    - Parallel execution of PostgreSQL and Weaviate storage
    - Progress tracking via queries
    - Fault tolerance with automatic recovery
    - Staging cleanup in finally block
    """

    def __init__(self) -> None:
        """Initialize workflow state."""
        self._current_step = "initialized"
        self._progress_percent = 0
        self._chunks_created = 0
        self._tenant_id: int | None = None

    @workflow.query
    def get_status(self) -> dict:
        """Query current workflow status.

        Returns:
            Dict with current step, progress percentage, and chunks created
        """
        return {
            "step": self._current_step,
            "progress": self._progress_percent,
            "chunks_created": self._chunks_created,
            "tenant_id": self._tenant_id,
        }

    async def _set_status_best_effort(
        self,
        document_id: str,
        workspace_id: str,
        status: str,
        error_message: str | None = None,
    ) -> None:
        """Write a document status transition without failing the workflow.

        Status writes ('processing'/'failed') are observability signals, not
        the source of truth, so a failure here is swallowed and logged.
        """
        try:
            await workflow.execute_activity(
                set_document_status,
                SetDocumentStatusInput(
                    document_id=document_id,
                    workspace_id=workspace_id,
                    status=status,
                    error_message=error_message,
                ),
                start_to_close_timeout=timedelta(seconds=10),
                retry_policy=RetryPolicy(
                    maximum_attempts=2,
                    initial_interval=timedelta(seconds=1),
                    maximum_interval=timedelta(seconds=3),
                    backoff_coefficient=2.0,
                ),
            )
        except Exception:
            workflow.logger.warning(f"Failed to set document status to '{status}' (non-fatal)")

    @staticmethod
    def _classify_error(error_message: str) -> str:
        """Classify an error message into an error type for dead-letter tracking.

        Mirrors TemporalWorkflowTrigger._classify_error so dead-letter rows have
        a consistent error_type regardless of where they were recorded.
        """
        lower = error_message.lower()
        if "extract" in lower or "parse" in lower:
            return "extraction_failed"
        if "storage" in lower or "postgresql" in lower or "weaviate" in lower:
            return "storage_failed"
        if "timeout" in lower or "timed out" in lower:
            return "timeout"
        if "validation" in lower or "invalid" in lower:
            return "validation_failed"
        if "fetch" in lower or "not found" in lower or "download" in lower:
            return "fetch_failed"
        return "unknown"

    @staticmethod
    def _reconstruct_original_message(input: DocumentIngestionInput) -> dict:
        """Rebuild the original upload-event message from the workflow input.

        The dead-letter retry API re-publishes ``original_message`` to start a
        fresh workflow, so the shape must match DocumentUploadMessage (the
        upload-event contract).
        """
        return {
            "event_type": "document.uploaded",
            "document_id": input.document_id,
            "workspace_id": input.workspace_id,
            "user_id": input.user_id,
            "filename": input.filename,
            "original_filename": input.original_filename,
            "content_type": input.content_type,
            "size_bytes": input.size_bytes,
            "storage_backend": input.storage_backend,
            "storage_path": input.storage_path,
            "storage_bucket": input.storage_bucket,
            "storage_url": input.storage_url,
            "timestamp": input.timestamp,
        }

    async def _record_dead_letter_best_effort(
        self,
        input: DocumentIngestionInput,
        workflow_run_id: str,
        error_message: str,
    ) -> None:
        """Record a terminal failure in the dead-letter table (best-effort).

        Must never raise: a failure to record the dead-letter row must not mask
        the original workflow error (#8). Swallows and logs any exception.
        """
        try:
            await workflow.execute_activity(
                record_dead_letter,
                RecordDeadLetterInput(
                    document_id=input.document_id,
                    workspace_id=input.workspace_id,
                    user_id=input.user_id,
                    workflow_run_id=workflow_run_id,
                    original_message=self._reconstruct_original_message(input),
                    error_message=error_message,
                    error_type=self._classify_error(error_message),
                ),
                start_to_close_timeout=timedelta(seconds=15),
                retry_policy=RetryPolicy(
                    maximum_attempts=2,
                    initial_interval=timedelta(seconds=1),
                    maximum_interval=timedelta(seconds=5),
                    backoff_coefficient=2.0,
                ),
            )
        except Exception:
            workflow.logger.warning("Failed to record dead-letter job (non-fatal)")

    @workflow.run
    async def run(self, input: DocumentIngestionInput) -> WorkflowResult:
        """Execute the document ingestion workflow.

        Args:
            input: DocumentIngestionInput with all document metadata

        Returns:
            WorkflowResult with success status and processing details
        """
        start_time = workflow.now()
        workflow_run_id = workflow.info().run_id

        try:
            # Create a minimal 'processing' row up front so the document is
            # observable via the status API before the store step; a failure in
            # fetch/extract/chunk then shows as 'failed', not 'not found' (#10).
            # Best-effort: a create failure must not fail the workflow.
            try:
                await workflow.execute_activity(
                    create_pending_document,
                    CreatePendingDocumentInput(
                        document_id=input.document_id,
                        workspace_id=input.workspace_id,
                        user_id=input.user_id,
                        filename=input.filename,
                        original_filename=input.original_filename,
                        content_type=input.content_type,
                        size_bytes=input.size_bytes,
                        storage_backend=input.storage_backend,
                        storage_path=input.storage_path,
                        storage_bucket=input.storage_bucket,
                        storage_url=input.storage_url,
                    ),
                    start_to_close_timeout=timedelta(seconds=15),
                    retry_policy=RetryPolicy(
                        maximum_attempts=2,
                        initial_interval=timedelta(seconds=1),
                        maximum_interval=timedelta(seconds=5),
                        backoff_coefficient=2.0,
                    ),
                )
            except Exception:
                workflow.logger.warning("Failed to create pending document row (non-fatal)")

            # Mark the document as 'processing' before heavy work begins.
            # Best-effort: a status-write failure must not fail the workflow.
            await self._set_status_best_effort(
                document_id=input.document_id,
                workspace_id=input.workspace_id,
                status="processing",
            )

            # Step 1: Ensure tenant infrastructure is ready (10%)
            self._current_step = "ensuring_tenant_ready"
            self._progress_percent = 5

            tenant_output = await workflow.execute_activity(
                ensure_tenant_ready,
                EnsureTenantInput(
                    workspace_id=input.workspace_id,
                    user_id=input.user_id,
                    workflow_run_id=workflow_run_id,
                    document_id=input.document_id,
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
            self._progress_percent = 10

            # Step 2: Validate document in storage (30%)
            self._current_step = "fetching_document"

            await workflow.execute_activity(
                fetch_document,
                FetchDocumentInput(
                    document_id=input.document_id,
                    storage_backend=input.storage_backend,
                    storage_path=input.storage_path,
                    storage_bucket=input.storage_bucket,
                    storage_url=input.storage_url,
                    workflow_run_id=workflow_run_id,
                    workspace_id=input.workspace_id,
                ),
                start_to_close_timeout=timedelta(minutes=2),
                retry_policy=RetryPolicy(
                    maximum_attempts=3,
                    initial_interval=timedelta(seconds=2),
                    maximum_interval=timedelta(seconds=30),
                    backoff_coefficient=2.0,
                ),
            )

            self._progress_percent = 30

            # Step 3: Extract text from document (50%)
            self._current_step = "extracting_text"

            extract_output = await workflow.execute_activity(
                extract_text,
                ExtractTextInput(
                    workflow_run_id=workflow_run_id,
                    storage_backend=input.storage_backend,
                    storage_path=input.storage_path,
                    content_type=input.content_type,
                    original_filename=input.original_filename,
                    storage_bucket=input.storage_bucket,
                    storage_url=input.storage_url,
                    document_id=input.document_id,
                    workspace_id=input.workspace_id,
                ),
                start_to_close_timeout=timedelta(minutes=5),
                retry_policy=RetryPolicy(
                    maximum_attempts=3,
                    initial_interval=timedelta(seconds=2),
                    maximum_interval=timedelta(seconds=30),
                    backoff_coefficient=2.0,
                ),
            )

            self._progress_percent = 50

            # Step 4: Chunk text (60%)
            self._current_step = "chunking_text"

            # Pass per-document overrides through as-is (they may be None). The
            # chunk_text activity resolves None from settings — reading config in
            # @workflow.run is a Temporal determinism anti-pattern (#38).
            chunk_output = await workflow.execute_activity(
                chunk_text,
                ChunkTextInput(
                    workflow_run_id=workflow_run_id,
                    document_id=input.document_id,
                    strategy=input.chunking_strategy,
                    max_chunk_size=input.max_chunk_size,
                    chunk_overlap=input.chunk_overlap,
                    workspace_id=input.workspace_id,
                ),
                start_to_close_timeout=timedelta(minutes=2),
                retry_policy=RetryPolicy(
                    maximum_attempts=2,
                    initial_interval=timedelta(seconds=1),
                    maximum_interval=timedelta(seconds=10),
                    backoff_coefficient=2.0,
                ),
            )

            self._chunks_created = chunk_output.chunk_count
            self._progress_percent = 60

            # Calculate processing time up to this point
            processing_time_ms = int((workflow.now() - start_time).total_seconds() * 1000)

            # Step 5: Store in PostgreSQL and Weaviate (parallel) (90%)
            self._current_step = "storing_document"

            store_input = StoreDocumentInput(
                workflow_run_id=workflow_run_id,
                document_id=input.document_id,
                workspace_id=input.workspace_id,
                user_id=input.user_id,
                filename=input.filename,
                original_filename=input.original_filename,
                content_type=input.content_type,
                size_bytes=input.size_bytes,
                storage_backend=input.storage_backend,
                storage_path=input.storage_path,
                text_length=extract_output.text_length,
                processing_time_ms=processing_time_ms,
                tenant_id=self._tenant_id,
            )

            # Execute PostgreSQL and Weaviate storage in parallel
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
                start_to_close_timeout=timedelta(seconds=60),
                retry_policy=RetryPolicy(
                    maximum_attempts=5,
                    initial_interval=timedelta(seconds=2),
                    maximum_interval=timedelta(seconds=30),
                    backoff_coefficient=2.0,
                ),
            )

            pg_result, wv_result = await asyncio.gather(pg_task, wv_task)

            self._progress_percent = 90

            # Check storage results
            if not pg_result.success:
                # PostgreSQL storage is critical
                pg_error = f"PostgreSQL storage failed: {pg_result.error}"
                await self._set_status_best_effort(
                    document_id=input.document_id,
                    workspace_id=input.workspace_id,
                    status="failed",
                    error_message=pg_error,
                )
                await self._record_dead_letter_best_effort(
                    input=input,
                    workflow_run_id=workflow_run_id,
                    error_message=pg_error,
                )
                return WorkflowResult(
                    document_id=input.document_id,
                    success=False,
                    error=pg_error,
                    processing_time_ms=processing_time_ms,
                )

            # Weaviate stores the embeddings that semantic/hybrid search reads.
            # PG is the truth layer (chunk text is durable), but a doc with no
            # vectors in Weaviate is invisible to the search API — the customer
            # sees status=ready and gets zero results.
            #
            # Decision: fail the workflow on Weaviate failure so the doc is
            # marked failed in PG (chunk_count stays the new value but status
            # transitions to "failed"). Customers can re-upload; ops can see
            # the problem in the dashboard. PG-only "ghost" docs are worse
            # than a clear failure.
            if not wv_result.success:
                wv_error = f"Weaviate storage failed: {wv_result.error}"
                workflow.logger.error(wv_error)
                await self._set_status_best_effort(
                    document_id=input.document_id,
                    workspace_id=input.workspace_id,
                    status="failed",
                    error_message=wv_error,
                )
                await self._record_dead_letter_best_effort(
                    input=input,
                    workflow_run_id=workflow_run_id,
                    error_message=wv_error,
                )
                return WorkflowResult(
                    document_id=input.document_id,
                    success=False,
                    error=wv_error,
                    processing_time_ms=processing_time_ms,
                )

            # Step 6: Update workspace statistics (100%)
            self._current_step = "updating_stats"

            await workflow.execute_activity(
                update_workspace_stats,
                UpdateStatsInput(
                    workspace_id=input.workspace_id,
                    document_delta=1,
                    chunk_delta=chunk_output.chunk_count,
                    size_delta=input.size_bytes,
                    workflow_run_id=workflow_run_id,
                    document_id=input.document_id,
                ),
                start_to_close_timeout=timedelta(seconds=15),
                retry_policy=RetryPolicy(
                    maximum_attempts=3,
                    initial_interval=timedelta(seconds=1),
                    maximum_interval=timedelta(seconds=5),
                    backoff_coefficient=2.0,
                ),
            )

            self._current_step = "completed"
            self._progress_percent = 100

            # Calculate final processing time
            final_processing_time_ms = int((workflow.now() - start_time).total_seconds() * 1000)

            return WorkflowResult(
                document_id=input.document_id,
                success=True,
                chunks_created=chunk_output.chunk_count,
                processing_time_ms=final_processing_time_ms,
            )

        except Exception as e:
            self._current_step = "failed"
            processing_time_ms = int((workflow.now() - start_time).total_seconds() * 1000)

            workflow.logger.error(f"Workflow failed: {str(e)}")

            # Best-effort: mark the document as failed so it isn't stuck
            # in 'processing'. A status-write failure must not mask the
            # original error.
            await self._set_status_best_effort(
                document_id=input.document_id,
                workspace_id=input.workspace_id,
                status="failed",
                error_message=str(e),
            )

            # Best-effort: record the terminal failure in the dead-letter table
            # so it can be retried via the dead-letter API (#8). Must not mask
            # the original error.
            await self._record_dead_letter_best_effort(
                input=input,
                workflow_run_id=workflow_run_id,
                error_message=str(e),
            )

            return WorkflowResult(
                document_id=input.document_id,
                success=False,
                error=str(e),
                processing_time_ms=processing_time_ms,
            )

        finally:
            # Always clean up staging data
            try:
                await workflow.execute_activity(
                    cleanup_staging,
                    CleanupStagingInput(workflow_run_id=workflow_run_id),
                    start_to_close_timeout=timedelta(seconds=15),
                    retry_policy=RetryPolicy(
                        maximum_attempts=2,
                        initial_interval=timedelta(seconds=1),
                        maximum_interval=timedelta(seconds=5),
                        backoff_coefficient=2.0,
                    ),
                )
            except Exception:
                workflow.logger.warning("Failed to clean up staging data")
