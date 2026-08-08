"""Temporal workflow trigger for bridging MQ events to Temporal workflows.

This module bridges the message queue (Valkey/Redis, Pub/Sub, etc.) and
Temporal workflow execution. It receives document upload notifications
via MQ and starts corresponding Temporal workflows.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog
from pydantic import ValidationError as PydanticValidationError
from temporalio.client import Client
from temporalio.common import WorkflowIDConflictPolicy

from src.config.settings import Settings
from src.models.document import DocumentUploadMessage, ProcessingResult
from src.temporal.models import DocumentIngestionInput, WorkflowResult
from src.temporal.workflows import DocumentIngestionWorkflow

# Both start_workflow call sites below use a fixed, deterministic workflow id
# (f"ingest-{document_id}") so status queries and dedup can address a run by
# document_id. That determinism means a re-index/refresh enqueued while the
# prior run for the same document_id is still open collides on the id.
#
# Temporal's default id_conflict_policy (UNSPECIFIED) treats a same-id
# collision against a RUNNING execution as an error: start_workflow raises
# WorkflowAlreadyStartedError. On the MQ-driven path (trigger_workflow_async)
# that exception propagates out of the handler, so the message is never
# ACKed and RedisMQService leaves it pending for redelivery (see
# src/services/mq/redis_mq.py) -- but every redelivery collides with the
# same still-open run and fails again, so the caller is stuck waiting for
# however long that stale run takes to close on its own (#110; ~10min was
# observed in CI, not a fixed timeout -- see docs/developer/learnings.md).
#
# Fix: supersede instead of collide, BUT only for callers whose fresh event
# genuinely represents newer content. TERMINATE_EXISTING tells Temporal to
# terminate the same-id running execution and start the new one atomically
# instead of raising. That is correct for the MQ upload/refresh path (the
# default here) -- but WRONG for a dead-letter retry of a *stale* payload
# racing a healthy, newer run for the same document: superseding there would
# silently discard the newer content in favor of the old dead-letter one
# (#110 follow-up review, blocker 3). So this is a per-call parameter, not a
# module constant -- callers must decide, not inherit a default silently.
# See DocumentIngestionWorkflow's store activities (store.py) for the second
# half of the fix this alone is not sufficient for: terminating a workflow
# does not stop an activity it already dispatched (no heartbeat/cancellation
# is wired), so a fencing check at commit time is what actually prevents a
# superseded run's late write from clobbering the newer one.
_SUPERSEDE_CONFLICT_POLICY = WorkflowIDConflictPolicy.TERMINATE_EXISTING
_REJECT_CONFLICT_POLICY = WorkflowIDConflictPolicy.UNSPECIFIED  # SDK default: raise on collision

if TYPE_CHECKING:
    from src.services.database import DatabaseService
    from src.services.mq import BaseMQService

logger = structlog.get_logger(__name__)


def build_ingestion_source_memo(
    source: str | None,
    connection_id: str | None = None,
    sync_id: str | None = None,
) -> dict[str, str]:
    """Build the Temporal memo every ``DocumentIngestionWorkflow.run`` start
    site attaches, describing where an ingestion came from
    (inherent-systems/prime#187, and its #178 extension below).

    Extracted out of ``TemporalWorkflowTrigger._build_source_memo`` (#178) so
    the memo SHAPE lives in exactly one place. #141 added this memo to the
    two MQ-driven start sites in this module; ``POST /ingest``
    (``src/api/app.py``) was a third start site #141 didn't cover, because
    its request model (``IngestRequest``) carries no ``source``/
    ``connection_id``/``sync_id`` fields to build one from -- there was
    nothing to port #141's fix onto without inventing something. Rather than
    have the REST route hand-roll its own ``{"source": ...}`` dict (drifting
    from this shape the moment either one changes independently), it calls
    this same function directly with a hardcoded ``source="api-direct"``
    (see ``app.py``'s ``_DIRECT_API_INGESTION_SOURCE``) -- a manual/API-direct
    trigger has no connector to attribute the memo to, so
    ``connection_id``/``sync_id`` are always omitted on that path.

    Args:
        source: The ingestion source label, e.g. ``"connector:notion"`` or
            ``"api-direct"``. ``None``/empty maps to ``"unknown"`` rather
            than an empty memo value, so every started workflow always shows
            SOME label in the Temporal UI summary panel.
        connection_id: Connector connection id, if this ingestion came from
            a connector sync. Omitted from the memo entirely when absent.
        sync_id: Connector sync-run id, same omission rule as
            ``connection_id``.

    Returns:
        A ``dict[str, str]`` suitable for ``Client.start_workflow(...,
        memo=...)``. Always has a ``"source"`` key; ``connection_id``/
        ``sync_id`` keys are present only when their argument was truthy.
    """
    memo: dict[str, str] = {"source": source or "unknown"}
    if connection_id:
        memo["connection_id"] = connection_id
    if sync_id:
        memo["sync_id"] = sync_id
    return memo


class TemporalWorkflowTrigger:
    """Triggers Temporal workflows from MQ messages.

    This class acts as a bridge between the message queue and
    the Temporal workflow system.
    """

    def __init__(
        self,
        settings: Settings,
        mq_service: BaseMQService | None = None,
        db_service: DatabaseService | None = None,
    ):
        """Initialize workflow trigger.

        Args:
            settings: Application settings with Temporal configuration
            mq_service: Optional MQ service for publishing completion notifications
            db_service: Optional database service for dead-letter recording
        """
        self.settings = settings
        self._mq_service = mq_service
        self._db_service = db_service
        self._client: Client | None = None
        self._initialized = False

    async def initialize(self) -> None:
        """Initialize Temporal client connection."""
        if self._initialized:
            return

        logger.info(
            "Connecting to Temporal server for workflow triggering",
            host=self.settings.temporal_host,
            namespace=self.settings.temporal_namespace,
        )

        self._client = await Client.connect(
            self.settings.temporal_host,
            namespace=self.settings.temporal_namespace,
        )

        self._initialized = True
        logger.info("Temporal client connected for workflow triggering")

    @staticmethod
    def _classify_error(error_message: str) -> str:
        """Classify an error message into an error type for dead-letter tracking.

        Args:
            error_message: The error string from the workflow

        Returns:
            A short error type classification
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
    def _build_source_memo(upload_message: DocumentUploadMessage) -> dict[str, str]:
        """Build the Temporal memo describing where an ingestion came from
        (inherent-systems/prime#187).

        Memo needs no namespace search-attribute registration and renders
        directly in the Temporal UI workflow summary panel, so operators can
        tell a connector sync apart from a manual or public-API upload.

        Backward compatible: messages produced before the ``source`` field
        existed have it as None, which maps to "unknown" rather than crashing
        the consumer. ``connection_id``/``sync_id`` are omitted entirely when
        absent (only set for connector-sourced uploads).

        Oversized-value handling (#141 adversarial pass): source/connection_id/
        sync_id carry ``max_length=500`` on ``DocumentUploadMessage`` itself
        (services/inh-contracts/src/inh_contracts/events.py), so a
        pathologically large value never reaches this method at all --
        ``DocumentUploadMessage(**message)`` raises ``PydanticValidationError``
        first, in both trigger paths, before ``_build_source_memo`` is ever
        called. ``trigger_workflow_async`` treats that exception as poison: it
        dead-letters via ``_record_dead_letter`` and returns normally so the MQ
        consumer ACKs the message (the redelivery loop in
        services/inh-ingestion-svc/src/services/mq/redis_mq.py never sees this
        failure mode). ``trigger_workflow`` (the synchronous, wait-for-result
        path) returns a failed ``ProcessingResult`` for the same exception
        instead of dead-lettering -- it has no production caller today (see
        ``trigger_workflow_async`` for the path the MQ consumer actually
        uses). Either way, this keeps the memo a value that is always correct
        or entirely absent -- never a truncated, misleadingly-plausible one.

        Delegates to the module-level ``build_ingestion_source_memo`` (#178)
        so this MQ-message-shaped extraction and ``POST /ingest``'s
        hardcoded-source call share one memo-shape implementation instead of
        drifting independently.
        """
        return build_ingestion_source_memo(
            source=upload_message.source,
            connection_id=upload_message.connection_id,
            sync_id=upload_message.sync_id,
        )

    async def _record_dead_letter(
        self,
        document_id: str,
        workspace_id: str,
        user_id: str,
        workflow_run_id: str | None,
        original_message: dict,
        error_message: str,
    ) -> None:
        """Record a failed job in the dead-letter table (non-blocking).

        If the DB insert fails, logs a warning but never raises.
        """
        if not self._db_service:
            logger.debug(
                "No db_service configured, skipping dead-letter recording",
                document_id=document_id,
            )
            return

        try:
            error_type = self._classify_error(error_message)
            await self._db_service.add_dead_letter_job(
                document_id=document_id,
                workspace_id=workspace_id,
                user_id=user_id,
                workflow_run_id=workflow_run_id,
                original_message=original_message,
                error_message=error_message,
                error_type=error_type,
            )
        except Exception as e:
            logger.warning(
                "Failed to record dead-letter job (non-blocking)",
                document_id=document_id,
                error=str(e),
            )

    async def trigger_workflow(
        self, message: dict, *, supersede_running: bool = True
    ) -> ProcessingResult:
        """Trigger a document ingestion workflow from an MQ message.

        This method:
        1. Validates the incoming message
        2. Converts it to workflow input
        3. Starts a Temporal workflow
        4. Waits for completion and returns the result

        Note: the completion event (document.processed / document.failed) is
        published by the WORKFLOW itself as a final activity (#88) — the
        contract has one owner, so this path must not publish it too.

        Args:
            message: Raw message dictionary from MQ
            supersede_running: When True (default), a same-id run already open
                for this document_id is terminated and superseded (#110) --
                correct when `message` is fresh content that should win. Pass
                False when replaying a message that may be *stale* relative to
                a run already in flight (e.g. a dead-letter retry, #110 blocker
                3): a collision then raises WorkflowAlreadyStartedError instead
                of silently discarding the newer run's work.

        Returns:
            ProcessingResult with success status
        """
        if not self._initialized:
            await self.initialize()

        document_id = message.get("document_id", "unknown")

        try:
            # Validate message schema
            try:
                upload_message = DocumentUploadMessage(**message)
                document_id = upload_message.document_id
            except PydanticValidationError as e:
                logger.error(
                    "Message validation failed",
                    error=str(e),
                    message=message,
                    validation_errors=e.errors(),
                )
                return ProcessingResult(
                    document_id=document_id,
                    success=False,
                    error=f"Invalid message format: {e}",
                )

            logger.info(
                "Triggering Temporal workflow",
                document_id=upload_message.document_id,
                workspace_id=upload_message.workspace_id,
                user_id=upload_message.user_id,
                filename=upload_message.original_filename,
            )

            # Create workflow input
            workflow_input = DocumentIngestionInput(
                document_id=upload_message.document_id,
                workspace_id=upload_message.workspace_id,
                user_id=upload_message.user_id,
                filename=upload_message.filename,
                original_filename=upload_message.original_filename,
                content_type=upload_message.content_type,
                size_bytes=upload_message.size_bytes,
                storage_backend=upload_message.storage_backend,
                storage_path=upload_message.storage_path,
                storage_bucket=upload_message.storage_bucket,
                storage_url=upload_message.storage_url,
                timestamp=upload_message.timestamp,
            )

            # Start the workflow
            if self._client is None:
                raise RuntimeError("Temporal client not initialized")

            workflow_id = f"ingest-{upload_message.document_id}"

            # Supersede a still-open prior run for this document_id instead of
            # colliding with it (#110) -- see module comment above. The caller
            # decides via supersede_running whether this message's content
            # should win a collision (default) or lose one (dead-letter retry).
            handle = await self._client.start_workflow(
                DocumentIngestionWorkflow.run,
                workflow_input,
                id=workflow_id,
                task_queue=self.settings.temporal_task_queue,
                memo=self._build_source_memo(upload_message),
                id_conflict_policy=(
                    _SUPERSEDE_CONFLICT_POLICY if supersede_running else _REJECT_CONFLICT_POLICY
                ),
            )

            logger.info(
                "Temporal workflow started",
                workflow_id=workflow_id,
                document_id=upload_message.document_id,
                task_queue=self.settings.temporal_task_queue,
            )

            # Wait for workflow completion
            result: WorkflowResult = await handle.result()

            logger.info(
                "Temporal workflow completed",
                workflow_id=workflow_id,
                document_id=result.document_id,
                success=result.success,
                chunks_created=result.chunks_created,
                processing_time_ms=result.processing_time_ms,
            )

            # Completion event is published by the workflow itself (#88).
            return ProcessingResult(
                document_id=result.document_id,
                success=result.success,
                chunks_created=result.chunks_created,
                error=result.error,
                processing_time_ms=result.processing_time_ms,
            )

        except Exception as e:
            logger.error(
                "Failed to trigger workflow",
                document_id=document_id,
                error=str(e),
                exc_info=True,
            )

            # No completion publish here: for pre-workflow failures there is no
            # workflow outcome to report (a poison message is dead-lettered by
            # the async path), and workflow failures publish document.failed
            # from inside the workflow (#88).
            return ProcessingResult(
                document_id=document_id,
                success=False,
                error=str(e),
            )

    async def trigger_workflow_async(self, message: dict, *, supersede_running: bool = True) -> str:
        """Trigger a workflow without waiting for completion.

        This method starts a workflow and returns immediately with the
        workflow ID. Useful for fire-and-forget scenarios.

        Args:
            message: Raw message dictionary from Pub/Sub
            supersede_running: When True (default), a same-id run already open
                for this document_id is terminated and superseded (#110) --
                correct for the MQ upload/refresh path this method normally
                serves, where `message` is always fresh content that should
                win. Pass False when `message` may be *stale* relative to a
                run already in flight -- e.g. dead-letter retry
                (`POST /dead-letter/{id}/retry`, src/api/app.py): replaying an
                old failed payload must never silently clobber a healthy,
                newer run for the same document (#110 blocker 3). With False,
                a collision raises WorkflowAlreadyStartedError as before this
                fix, so the caller's existing handling (reset to pending,
                surface an error) still applies unchanged.

        Returns:
            Workflow ID for tracking
        """
        import time

        from src.services.metrics import WORKFLOW_START_LATENCY

        receive_time = time.perf_counter()

        if not self._initialized:
            await self.initialize()

        # Validate message. A malformed (poison) message can never succeed on
        # retry, so we dead-letter it and return normally → the MQ consumer ACKs
        # it and stops redelivering. A *transient* failure below (e.g. Temporal
        # unavailable) still raises so the message is left pending → redelivered (#6).
        try:
            upload_message = DocumentUploadMessage(**message)
        except PydanticValidationError as e:
            logger.error(
                "Poison upload message; dead-lettering instead of redelivering",
                error=str(e),
                message=message,
                validation_errors=e.errors(),
            )
            await self._record_dead_letter(
                document_id=message.get("document_id", "unknown"),
                workspace_id=message.get("workspace_id", "unknown"),
                user_id=message.get("user_id", "unknown"),
                workflow_run_id=None,
                original_message=message,
                error_message=f"Invalid message format: {e}",
            )
            return ""

        # Create workflow input
        workflow_input = DocumentIngestionInput(
            document_id=upload_message.document_id,
            workspace_id=upload_message.workspace_id,
            user_id=upload_message.user_id,
            filename=upload_message.filename,
            original_filename=upload_message.original_filename,
            content_type=upload_message.content_type,
            size_bytes=upload_message.size_bytes,
            storage_backend=upload_message.storage_backend,
            storage_path=upload_message.storage_path,
            storage_bucket=upload_message.storage_bucket,
            storage_url=upload_message.storage_url,
            timestamp=upload_message.timestamp,
        )

        if self._client is None:
            raise RuntimeError("Temporal client not initialized")

        workflow_id = f"ingest-{upload_message.document_id}"

        # Start the workflow without awaiting its result. If Temporal is
        # transiently unavailable this raises and propagates so the MQ
        # consumer does NOT ack the message (it stays pending → redelivered).
        #
        # Supersede a still-open prior run for this document_id instead of
        # colliding with it (#110) -- see module comment above. Without this,
        # a re-index/refresh enqueued while the previous run is still open
        # raises WorkflowAlreadyStartedError here, which (like any other
        # exception in this method) also propagates for MQ redelivery -- but
        # every redelivery hits the same collision until the stale run closes
        # on its own, stalling the caller for however long that takes.
        #
        # supersede_running=False (dead-letter retry) keeps the original
        # raise-on-collision behavior so a stale replay can't silently
        # terminate a healthy newer run (#110 blocker 3).
        await self._client.start_workflow(
            DocumentIngestionWorkflow.run,
            workflow_input,
            id=workflow_id,
            task_queue=self.settings.temporal_task_queue,
            memo=self._build_source_memo(upload_message),
            id_conflict_policy=(
                _SUPERSEDE_CONFLICT_POLICY if supersede_running else _REJECT_CONFLICT_POLICY
            ),
        )

        # Record admission latency: MQ-receive → Temporal-accepted.
        WORKFLOW_START_LATENCY.observe(time.perf_counter() - receive_time)

        logger.info(
            "Temporal workflow started (async)",
            workflow_id=workflow_id,
            document_id=upload_message.document_id,
        )

        return workflow_id

    async def get_workflow_status(self, workflow_id: str) -> dict | None:
        """Get the status of a running workflow.

        Args:
            workflow_id: The workflow ID to query

        Returns:
            Status dict with step, progress, and chunks_created
        """
        if self._client is None:
            await self.initialize()

        try:
            if self._client is None:
                raise RuntimeError("Temporal client not initialized")

            handle = self._client.get_workflow_handle(workflow_id)
            status = await handle.query(DocumentIngestionWorkflow.get_status)
            return status
        except Exception as e:
            logger.error(
                "Failed to get workflow status",
                workflow_id=workflow_id,
                error=str(e),
            )
            return None

    def shutdown(self) -> None:
        """Shutdown the trigger (cleanup resources)."""
        # Temporal client doesn't need explicit cleanup
        self._client = None
        self._initialized = False
        logger.info("Temporal workflow trigger shut down")


# Global trigger instance
_workflow_trigger: TemporalWorkflowTrigger | None = None


def get_workflow_trigger(
    settings: Settings,
    mq_service: BaseMQService | None = None,
    db_service: DatabaseService | None = None,
) -> TemporalWorkflowTrigger:
    """Get or create the global workflow trigger.

    Args:
        settings: Application settings
        mq_service: Optional MQ service for publishing completion notifications
        db_service: Optional database service for dead-letter recording

    Returns:
        TemporalWorkflowTrigger instance
    """
    global _workflow_trigger
    if _workflow_trigger is None:
        _workflow_trigger = TemporalWorkflowTrigger(
            settings, mq_service=mq_service, db_service=db_service
        )
    else:
        # Backfill dependencies a later caller provides (e.g. api-only mode
        # constructs the trigger before db_service is wired) so dead-letter
        # recording is not a permanent no-op (#6). Never downgrade to None.
        if db_service is not None and _workflow_trigger._db_service is None:
            _workflow_trigger._db_service = db_service
        if mq_service is not None and _workflow_trigger._mq_service is None:
            _workflow_trigger._mq_service = mq_service
    return _workflow_trigger
