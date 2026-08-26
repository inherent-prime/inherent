"""Temporal worker configuration and execution.

This module configures and runs the Temporal worker that executes
document ingestion workflows and activities.

Supports dual-namespace operation:
- **default** namespace: document ingestion workflows
- **audit** namespace: audit log write workflows
"""

import asyncio
import contextlib
from collections.abc import Callable
from typing import Any

import structlog
from temporalio.client import Client
from temporalio.worker import Worker

from src.config.settings import Settings
from src.temporal.activities import (
    chunk_text,
    cleanup_staging,
    create_pending_document,
    ensure_tenant_ready,
    extract_text,
    fetch_document,
    publish_completion,
    record_chunk_edit_weaviate_failure,
    record_dead_letter,
    resolve_dead_letter_jobs,
    set_document_status,
    store_in_postgresql,
    store_in_weaviate,
    update_chunk_postgresql,
    update_chunk_weaviate,
    update_workspace_stats,
)
from src.temporal.activities.audit_activities import (
    emit_audit_metric,
    validate_audit_event,
    write_audit_log_to_mongo,
)
from src.temporal.workflows import (
    ChunkEditWorkflow,
    DocumentIngestionWorkflow,
)
from src.temporal.workflows.audit_log import WriteAuditLogWorkflow

logger = structlog.get_logger(__name__)

# All activities registered with the ingestion worker
_ALL_ACTIVITIES: list[Callable[..., Any]] = [
    create_pending_document,
    ensure_tenant_ready,
    fetch_document,
    extract_text,
    chunk_text,
    store_in_postgresql,
    store_in_weaviate,
    set_document_status,
    update_workspace_stats,
    cleanup_staging,
    update_chunk_postgresql,
    update_chunk_weaviate,
    record_chunk_edit_weaviate_failure,
    record_dead_letter,
    resolve_dead_letter_jobs,
    publish_completion,
]

# All workflows registered with the ingestion worker
_ALL_WORKFLOWS = [
    DocumentIngestionWorkflow,
    ChunkEditWorkflow,
]

# Audit namespace activities and workflows
_AUDIT_ACTIVITIES: list[Callable[..., Any]] = [
    validate_audit_event,
    write_audit_log_to_mongo,
    emit_audit_metric,
]

_AUDIT_WORKFLOWS = [
    WriteAuditLogWorkflow,
]


async def _cleanup_stale_staging(settings: Settings) -> None:
    """Delete staging rows older than 1 hour. Safety net for crashed workflows.

    Called once at worker startup (below) AND repeatedly by
    _periodic_staging_cleanup -- see that function's docstring for why a
    startup-only sweep is not sufficient (#110 blocker 2).
    """
    try:
        from src.services.staging import StagingService

        staging = StagingService(settings)
        staging.connect()
        try:
            deleted = staging.cleanup_stale(max_age_hours=1)
            if deleted > 0:
                logger.info("Cleaned stale staging rows", deleted=deleted)
        finally:
            staging.disconnect()
    except Exception as e:
        # Non-fatal — table may not exist yet on first run
        logger.debug("Staging cleanup skipped", error=str(e))


# How often the periodic staging sweep (below) runs, in addition to the
# one-shot startup sweep. #110's TERMINATE_EXISTING conflict policy lets a
# fresh re-index terminate a still-open prior run; termination closes a
# workflow execution server-side WITHOUT delivering another workflow task,
# so that run's `finally: cleanup_staging` (document_ingestion.py) never
# executes -- unlike a normal completion, failure, or even a *cancellation*
# (which the workflow's own try/except/finally CAN observe and run). Before
# #110, the only way to orphan a staging row was a hard worker crash, and
# `_cleanup_stale_staging` only ran again on the next startup -- fine for
# that case. After #110, a superseded re-index is a ROUTINE event on an
# otherwise-healthy, long-running worker, so relying on "the worker happens
# to restart" leaves each superseded run's `extracted_text` + `chunks` rows
# (the very payloads staging exists for because they exceed Temporal's 4MB
# limit -- see services/staging.py) stranded indefinitely. This interval
# task makes the sweep actually periodic, independent of worker restarts.
_STAGING_SWEEP_INTERVAL_SECONDS = 900  # 15 minutes


async def _periodic_staging_cleanup(
    settings: Settings, interval_seconds: int = _STAGING_SWEEP_INTERVAL_SECONDS
) -> None:
    """Run _cleanup_stale_staging on a fixed interval until cancelled.

    Callers own the task's lifecycle: create with asyncio.create_task, cancel
    (and await, suppressing CancelledError) on shutdown -- see run_worker's
    `finally` and TemporalWorkerManager.stop().
    """
    while True:
        await asyncio.sleep(interval_seconds)
        await _cleanup_stale_staging(settings)


async def create_temporal_client(settings: Settings) -> Client:
    """Create and configure a Temporal client.

    Args:
        settings: Application settings with Temporal configuration

    Returns:
        Connected Temporal client
    """
    logger.info(
        "Connecting to Temporal server",
        host=settings.temporal_host,
        namespace=settings.temporal_namespace,
    )

    client = await Client.connect(
        settings.temporal_host,
        namespace=settings.temporal_namespace,
    )

    logger.info("Connected to Temporal server")
    return client


async def create_audit_temporal_client(settings: Settings) -> Client:
    """Create a Temporal client for the audit namespace.

    Args:
        settings: Application settings

    Returns:
        Connected Temporal client for the audit namespace
    """
    logger.info(
        "Connecting to Temporal server (audit namespace)",
        host=settings.temporal_host,
        namespace=settings.temporal_audit_namespace,
    )

    client = await Client.connect(
        settings.temporal_host,
        namespace=settings.temporal_audit_namespace,
    )

    logger.info("Connected to Temporal server (audit namespace)")
    return client


async def run_worker(
    settings: Settings,
    shutdown_event: asyncio.Event | None = None,
    *,
    audit_client: Client | None = None,
) -> None:
    """Run the Temporal workers (ingestion + audit).

    This function:
    1. Initializes shared service registry (long-lived connection pools)
    2. Cleans up stale staging rows from crashed workflows
    3. Connects to Temporal server (default + audit namespaces)
    4. Registers workflows and activities on both workers
    5. Starts processing tasks from both queues concurrently
    6. Handles graceful shutdown and service cleanup

    Args:
        settings: Application settings
        shutdown_event: Optional event to signal shutdown
        audit_client: Pre-created audit Temporal client (used when
            the caller needs access to it before the worker starts)
    """
    from src.temporal import shared_services

    # Initialize shared connection pools for activities
    shared_services.initialize(settings)

    # Clean up stale staging rows on startup, then keep sweeping periodically
    # for the lifetime of this worker (#110 blocker 2) -- a startup-only
    # sweep is not sufficient once a terminated (superseded) workflow can
    # orphan staging rows during normal operation, not only on a crash.
    await _cleanup_stale_staging(settings)
    staging_sweep_task = asyncio.create_task(_periodic_staging_cleanup(settings))

    client = await create_temporal_client(settings)

    # Create ingestion worker.
    # Concurrency limits (#18 backpressure): cap in-flight activities and
    # workflow tasks so a burst of async workflow starts can't overwhelm the
    # worker. This is the real throughput bound now that MQ consumption is
    # non-blocking.
    ingestion_worker = Worker(
        client,
        task_queue=settings.temporal_task_queue,
        workflows=_ALL_WORKFLOWS,
        activities=_ALL_ACTIVITIES,
        max_concurrent_activities=settings.temporal_max_concurrent_activities,
        max_concurrent_workflow_tasks=settings.temporal_max_concurrent_workflow_tasks,
    )

    logger.info(
        "Starting Temporal ingestion worker",
        task_queue=settings.temporal_task_queue,
        workflows=[w.__name__ for w in _ALL_WORKFLOWS],
        activities=[a.__name__ if hasattr(a, "__name__") else str(a) for a in _ALL_ACTIVITIES],
    )

    # Create audit worker (dual namespace).
    # If audit setup fails, clean up already-initialized resources
    # (shared services, plus drop references to any clients we created)
    # before re-raising so the caller doesn't leak connection pools.
    # Temporal Client has no explicit close() — its gRPC channel is
    # released when the object is garbage-collected.
    audit_client_created_here = False
    try:
        if audit_client is None:
            audit_client = await create_audit_temporal_client(settings)
            audit_client_created_here = True

        audit_worker = Worker(
            audit_client,
            task_queue=settings.temporal_audit_task_queue,
            workflows=_AUDIT_WORKFLOWS,
            activities=_AUDIT_ACTIVITIES,
        )
    except Exception:
        logger.error(
            "Failed to start audit worker; cleaning up partial state",
            audit_client_created_here=audit_client_created_here,
            exc_info=True,
        )
        # Await the cancellation (consistent with the other two cancel sites
        # in this file, #110 follow-up review item 7) -- cancel() alone only
        # schedules cancellation, it doesn't wait for the task to actually
        # stop. Harmless either way here (the task just stops sweeping), but
        # inconsistency invites someone to later "fix" one site and miss the
        # others.
        staging_sweep_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await staging_sweep_task
        try:
            shared_services.shutdown()
        except Exception:
            logger.debug("Error shutting down shared_services during cleanup", exc_info=True)
        # Drop references so the gRPC channels can be GC'd
        audit_client = None
        raise

    logger.info(
        "Starting Temporal audit worker",
        task_queue=settings.temporal_audit_task_queue,
        workflows=[w.__name__ for w in _AUDIT_WORKFLOWS],
        activities=[a.__name__ if hasattr(a, "__name__") else str(a) for a in _AUDIT_ACTIVITIES],
    )

    try:
        if shutdown_event:
            async with ingestion_worker, audit_worker:
                await shutdown_event.wait()
                logger.info("Shutdown signal received, stopping workers...")
        else:
            # Run both workers concurrently
            await asyncio.gather(
                ingestion_worker.run(),
                audit_worker.run(),
            )
    finally:
        staging_sweep_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await staging_sweep_task
        shared_services.shutdown()

    logger.info("Temporal workers stopped")


class TemporalWorkerManager:
    """Manager for Temporal worker lifecycle.

    Provides methods for starting, stopping, and managing the
    Temporal worker in various deployment scenarios.

    Uses a shutdown event instead of hard-cancellation so that
    in-flight activities can drain gracefully before the worker stops.
    """

    def __init__(self, settings: Settings):
        """Initialize worker manager.

        Args:
            settings: Application settings
        """
        self.settings = settings
        self._client: Client | None = None
        self._audit_client: Client | None = None
        self._worker: Worker | None = None
        self._audit_worker: Worker | None = None
        self._worker_task: asyncio.Task | None = None
        self._shutdown_event: asyncio.Event | None = None
        self._staging_sweep_task: asyncio.Task | None = None

    async def start(self) -> None:
        """Start the Temporal workers (ingestion + audit)."""
        if self._worker_task is not None:
            logger.warning("Worker already running")
            return

        from src.temporal import shared_services

        # Initialize shared connection pools for activities
        shared_services.initialize(self.settings)

        # Clean up stale staging rows on startup, then keep sweeping
        # periodically for as long as this manager runs (#110 blocker 2) --
        # see _periodic_staging_cleanup's docstring for why a startup-only
        # sweep no longer bounds the leak once TERMINATE_EXISTING can
        # supersede (and thus orphan the staging of) a workflow run.
        await _cleanup_stale_staging(self.settings)
        self._staging_sweep_task = asyncio.create_task(_periodic_staging_cleanup(self.settings))

        self._client = await create_temporal_client(self.settings)
        self._audit_client = await create_audit_temporal_client(self.settings)

        self._worker = Worker(
            self._client,
            task_queue=self.settings.temporal_task_queue,
            workflows=_ALL_WORKFLOWS,
            activities=_ALL_ACTIVITIES,
            max_concurrent_activities=self.settings.temporal_max_concurrent_activities,
            max_concurrent_workflow_tasks=self.settings.temporal_max_concurrent_workflow_tasks,
        )

        self._audit_worker = Worker(
            self._audit_client,
            task_queue=self.settings.temporal_audit_task_queue,
            workflows=_AUDIT_WORKFLOWS,
            activities=_AUDIT_ACTIVITIES,
        )

        self._shutdown_event = asyncio.Event()
        self._worker_task = asyncio.create_task(
            self._run_with_shutdown(self._worker, self._audit_worker, self._shutdown_event)
        )

        logger.info(
            "Temporal workers started",
            ingestion_queue=self.settings.temporal_task_queue,
            audit_queue=self.settings.temporal_audit_task_queue,
        )

    async def _run_with_shutdown(
        self,
        worker: Worker,
        audit_worker: Worker,
        shutdown_event: asyncio.Event,
    ) -> None:
        """Run both workers with graceful shutdown via event.

        Uses ``async with`` so the Temporal SDK properly drains
        in-flight activities before exiting.
        """
        async with worker, audit_worker:
            await shutdown_event.wait()
            logger.info("Shutdown signal received, draining workers...")

    async def stop(self) -> None:
        """Stop the Temporal worker gracefully.

        Sets the shutdown event to signal the worker to drain in-flight
        activities, then waits for the worker task to complete.
        """
        if self._worker_task is None:
            return

        logger.info("Stopping Temporal worker...")

        # Signal graceful shutdown (worker drains in-flight activities)
        if self._shutdown_event:
            self._shutdown_event.set()

        try:
            await self._worker_task
        except asyncio.CancelledError:
            pass

        if self._staging_sweep_task is not None:
            self._staging_sweep_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._staging_sweep_task
            self._staging_sweep_task = None

        self._worker_task = None
        self._worker = None
        self._audit_worker = None
        self._client = None
        self._audit_client = None
        self._shutdown_event = None

        # Clean up shared connection pools
        from src.temporal import shared_services

        shared_services.shutdown()

        logger.info("Temporal worker stopped")

    async def get_client(self) -> Client:
        """Get or create a Temporal client.

        Returns:
            Temporal client instance
        """
        if self._client is None:
            self._client = await create_temporal_client(self.settings)
        return self._client

    @property
    def is_running(self) -> bool:
        """Check if worker is running."""
        return self._worker_task is not None and not self._worker_task.done()


# Global worker manager instance (for use in main.py)
_worker_manager: TemporalWorkerManager | None = None


def get_worker_manager(settings: Settings) -> TemporalWorkerManager:
    """Get or create the global worker manager.

    Args:
        settings: Application settings

    Returns:
        TemporalWorkerManager instance
    """
    global _worker_manager
    if _worker_manager is None:
        _worker_manager = TemporalWorkerManager(settings)
    return _worker_manager
