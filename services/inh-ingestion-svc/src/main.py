"""Main entry point for the ingestion service.

Service modes:
    worker     — Temporal worker + MQ subscriber (production default)
    standalone — HTTP API + Temporal worker (manual triggers, health checks)
    migrate    — Apply pending SQL migrations, then exit (DB init container)

Configure via SERVICE_MODE environment variable.
"""

from __future__ import annotations

import asyncio
import signal
import sys
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

import structlog

from src.config.settings import Settings, get_settings
from src.services.mq import BaseMQService, create_mq_service
from src.utils.logger import setup_logging

logger = structlog.get_logger(__name__)

try:
    # Single source of truth: the installed package version (pyproject.toml),
    # so the logged version can't drift from the published image tag.
    __version__ = _pkg_version("inh-ingestion-svc")
except PackageNotFoundError:  # not installed (e.g. running from a raw checkout)
    __version__ = "0.0.0+local"

# Global instances
mq_service: BaseMQService | None = None
_shutdown_event: asyncio.Event | None = None


def setup_signal_handlers() -> None:
    """Setup signal handlers for graceful shutdown."""

    def signal_handler(sig, frame):
        logger.info("Received shutdown signal", signal=sig)
        if _shutdown_event:
            _shutdown_event.set()
        else:
            sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)


# =============================================================================
# Worker Mode: Temporal worker + MQ subscriber
# =============================================================================


async def run_worker(settings: Settings) -> None:
    """Run Temporal worker + MQ subscriber + HTTP API.

    This is the recommended mode for all environments (dev, staging, prod).
    1. Starts a Prometheus metrics server on METRICS_PORT
    2. Starts the HTTP API on API_PORT (for chunk edits, health checks)
    3. Starts a Temporal worker to execute ingestion workflows
    4. Subscribes to the MQ upload topic to receive document events
    5. For each event, triggers a Temporal workflow
    6. After workflow completes, publishes completion to the MQ completion topic
    """
    global mq_service

    import uvicorn
    from prometheus_client import start_http_server

    from src.api import create_app
    from src.temporal.trigger import get_workflow_trigger
    from src.temporal.worker import run_worker as run_temporal_worker

    logger.info(
        "Starting worker mode (Temporal worker + MQ subscriber + HTTP API)",
        mq_backend=settings.mq_backend,
        temporal_host=settings.temporal_host,
        upload_topic=settings.mq_upload_topic,
        completion_topic=settings.mq_completion_topic,
    )

    # 0. Start Prometheus metrics server (non-blocking, runs in background thread)
    start_http_server(settings.metrics_port)
    logger.info("Prometheus metrics server started", port=settings.metrics_port)

    # 1. Connect to MQ
    mq_service = create_mq_service(settings)
    await mq_service.connect()
    logger.info("MQ connected", backend=mq_service.backend)

    # Register the connection with the shared registry so the workflow's
    # publish_completion activity (#88) reuses it instead of opening its own.
    from src.temporal import shared_services

    shared_services.set_mq_service(mq_service)

    # 2. Initialize workflow trigger (bridges MQ → Temporal → MQ). Pass the
    # shared db_service so poison messages can be dead-lettered (#6) — without
    # it, _record_dead_letter is a silent no-op.
    from src.temporal.shared_services import get_db_service

    trigger = get_workflow_trigger(settings, mq_service=mq_service, db_service=get_db_service())
    await trigger.initialize()
    logger.info("Temporal trigger initialized")

    # 3. Subscribe to upload topic.
    # Use the NON-BLOCKING async start (#18 backpressure): the handler returns
    # once Temporal accepts the workflow start, freeing the consumer instead of
    # blocking until the workflow finishes. Processing concurrency is bounded by
    # the Temporal worker; the consume loop is bounded by mq_max_concurrent.
    await mq_service.subscribe(
        topic=settings.mq_upload_topic,
        handler=trigger.trigger_workflow_async,  # type: ignore[arg-type]
        group_id=settings.mq_consumer_group,
    )
    logger.info(
        "Subscribed to upload topic",
        topic=settings.mq_upload_topic,
        group=settings.mq_consumer_group,
    )

    # 3b. Create audit Temporal client and subscribe audit consumer
    from src.services.audit_consumer import AuditLogConsumer
    from src.temporal.worker import create_audit_temporal_client

    audit_client = await create_audit_temporal_client(settings)
    audit_consumer = AuditLogConsumer(
        temporal_client=audit_client,
        task_queue=settings.temporal_audit_task_queue,
    )
    await mq_service.subscribe(
        topic=settings.audit_log_topic,
        handler=audit_consumer.handle,
        group_id=settings.audit_consumer_group,
    )
    logger.info(
        "Subscribed to audit log topic",
        topic=settings.audit_log_topic,
        group=settings.audit_consumer_group,
    )

    # 4. Start HTTP API (chunk edits, health, ingest triggers)
    api_task = None
    if settings.ingestion_api_key:
        app = create_app(settings)
        config = uvicorn.Config(
            app,
            host=settings.api_host,
            port=settings.api_port,
            log_level=settings.log_level.lower(),
        )
        server = uvicorn.Server(config)
        api_task = asyncio.create_task(server.serve())
        logger.info("HTTP API started", port=settings.api_port)
    else:
        logger.info("INGESTION_API_KEY not set, HTTP API disabled")

    # 5. Start Temporal worker (processes workflows — ingestion + audit)
    worker_task = asyncio.create_task(
        run_temporal_worker(settings, _shutdown_event, audit_client=audit_client)
    )

    # Wait for shutdown
    try:
        await worker_task
    except asyncio.CancelledError:
        logger.info("Worker task cancelled during shutdown")
    finally:
        if api_task:
            api_task.cancel()
        await mq_service.disconnect()
        trigger.shutdown()


# =============================================================================
# Standalone Mode: HTTP API + Temporal worker
# =============================================================================


async def run_standalone(settings: Settings) -> None:
    """Run HTTP API with embedded Temporal worker.

    Exposes POST /ingest for manual document ingestion triggers.
    Also runs a Temporal worker in the same process.
    """
    import uvicorn

    from src.api import create_app

    if not settings.ingestion_api_key:
        logger.error(
            "INGESTION_API_KEY must be set for standalone mode. "
            "Set it in .env or as an environment variable."
        )
        sys.exit(1)

    logger.info(
        "Starting standalone mode (HTTP API + Temporal worker)",
        host=settings.api_host,
        port=settings.api_port,
        temporal_host=settings.temporal_host,
    )

    app = create_app(settings)

    config = uvicorn.Config(
        app,
        host=settings.api_host,
        port=settings.api_port,
        log_level=settings.log_level.lower(),
    )
    server = uvicorn.Server(config)
    await server.serve()


# =============================================================================
# Entry Point
# =============================================================================

# Map legacy mode names to current modes
_MODE_ALIASES: dict[str, str] = {
    "pubsub": "worker",
    "temporal_worker": "worker",
    "temporal_trigger": "worker",
    "temporal_all": "worker",
}


async def main() -> None:
    """Main application entry point."""
    global _shutdown_event

    settings = get_settings()
    setup_logging(settings.log_level)

    # Resolve mode (with backward compatibility)
    raw_mode = settings.service_mode.lower()
    mode = _MODE_ALIASES.get(raw_mode, raw_mode)

    if raw_mode != mode:
        logger.warning(
            f"SERVICE_MODE='{raw_mode}' is deprecated, mapped to '{mode}'",
            old_mode=raw_mode,
            new_mode=mode,
        )

    logger.info(
        "Starting ingestion service",
        version=__version__,
        mode=mode,
        mq_backend=settings.mq_backend,
    )

    _shutdown_event = asyncio.Event()

    try:
        if mode == "migrate":
            # One-shot DB init container: apply migrations, then exit. Used by
            # docker-compose.release.yml in place of the host-bind-mounted
            # postgres-init step, so the stack is self-contained from images.
            from src.services.migrations import run_migrations

            run_migrations(settings)
            return
        if mode == "standalone":
            await run_standalone(settings)
        else:
            await run_worker(settings)
    except Exception as e:
        logger.error("Fatal error in main", error=str(e), exc_info=True)
        raise
    finally:
        if mq_service and mq_service.is_connected():
            await mq_service.disconnect()


if __name__ == "__main__":
    setup_signal_handlers()
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Service interrupted by user")
