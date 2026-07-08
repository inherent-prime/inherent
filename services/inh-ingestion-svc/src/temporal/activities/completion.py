"""Activity publishing the document completion event (#88).

Worker mode starts workflows via the fire-and-forget trigger, so the MQ
consumer never observes the workflow outcome — the `document.processed` /
`document.failed` contract (DocumentCompletionMessage, consumed by intg-svc)
must be fulfilled from INSIDE the workflow as its final activity. Publishing
here (instead of from the trigger) composes with fire-and-forget admission,
survives worker restarts, and gets Temporal's retry semantics.

Unlike BaseMQService.publish_completion (which swallows errors), a publish
failure RAISES so Temporal retries it; the workflow wraps the call so an
exhausted retry policy still can't fail an otherwise-complete ingestion.
"""

import structlog
from temporalio import activity

from src.temporal.models import PublishCompletionInput

logger = structlog.get_logger(__name__)


@activity.defn
async def publish_completion(input: PublishCompletionInput) -> bool:
    """Publish exactly one DocumentCompletionMessage for a finished workflow.

    Rebuilds the upload-event context from the workflow input and the outcome
    fields, then XADDs the canonical completion payload to the configured
    completion topic (core.document.processed.v1).

    Returns:
        True if the event was published, False if publishing is disabled
        (no completion topic configured).

    Raises:
        Any error from the MQ publish — propagated so Temporal retries.
    """
    from src.models.document import DocumentUploadMessage, ProcessingResult
    from src.services.mq.base import build_completion_message
    from src.temporal import shared_services

    mq_service = await shared_services.get_mq_service()

    topic = mq_service.settings.mq_completion_topic
    if not topic:
        logger.warning(
            "MQ completion topic not configured, skipping completion event",
            document_id=input.document_id,
        )
        return False

    result = ProcessingResult(
        document_id=input.document_id,
        success=input.success,
        chunks_created=input.chunks_created,
        error=input.error,
        processing_time_ms=input.processing_time_ms,
    )
    upload_message = DocumentUploadMessage(
        event_type="document.uploaded",
        document_id=input.document_id,
        workspace_id=input.workspace_id,
        user_id=input.user_id,
        filename=input.filename,
        original_filename=input.original_filename,
        content_type=input.content_type,
        size_bytes=input.size_bytes,
        storage_backend=input.storage_backend,  # type: ignore[arg-type]
        storage_path=input.storage_path,
        storage_bucket=input.storage_bucket,
        storage_url=input.storage_url,
        timestamp=input.timestamp or "1970-01-01T00:00:00Z",
    )

    message = build_completion_message(result, upload_message)
    await mq_service.publish(topic, message)

    logger.info(
        "Published completion event",
        document_id=input.document_id,
        event_type=message["event_type"],
        success=input.success,
        topic=topic,
    )
    return True
