"""Base message queue service interface.

All MQ backends must implement this abstract class. The interface is intentionally
kept close to the intg-svc IMQService TypeScript interface for consistency.
"""

from __future__ import annotations

import abc
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from src.config.settings import Settings
    from src.models.document import DocumentUploadMessage, ProcessingResult

logger = structlog.get_logger(__name__)

# Handler receives the parsed message dict
MessageHandler = Callable[[dict], Awaitable[None]]


def build_completion_message(
    result: ProcessingResult,
    upload_message: DocumentUploadMessage,
) -> dict:
    """Build the DocumentCompletionMessage payload from a processing outcome.

    Single owner of the completion-event shape (#88): used by
    BaseMQService.publish_completion (legacy sync paths) and by the
    publish_completion Temporal activity (worker mode).
    """
    return {
        "event_type": "document.processed" if result.success else "document.failed",
        "document_id": result.document_id,
        "workspace_id": upload_message.workspace_id,
        "user_id": upload_message.user_id,
        "original_filename": upload_message.original_filename,
        "success": result.success,
        "status": "ready" if result.success else "failed",
        "chunks_created": result.chunks_created,
        "processing_time_ms": result.processing_time_ms,
        "error": result.error,
        "timestamp": datetime.now(UTC).isoformat(),
        # Storage metadata for intg-svc document creation
        "content_type": upload_message.content_type,
        "size_bytes": upload_message.size_bytes,
        "storage_backend": upload_message.storage_backend,
        "storage_path": upload_message.storage_path or upload_message.filename,
        "storage_bucket": upload_message.storage_bucket,
        "storage_url": upload_message.storage_url,
    }


class BaseMQService(abc.ABC):
    """Abstract base class for message queue backends.

    Provides a consistent interface across Valkey/Redis, Google Pub/Sub,
    and in-memory (test) implementations.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @property
    @abc.abstractmethod
    def backend(self) -> str:
        """Return the backend identifier (e.g. 'redis', 'pubsub', 'memory')."""

    @abc.abstractmethod
    async def connect(self) -> None:
        """Establish connection to the message broker."""

    @abc.abstractmethod
    async def disconnect(self) -> None:
        """Gracefully close the connection."""

    @abc.abstractmethod
    async def publish(self, topic: str, message: dict) -> None:
        """Publish a message to a topic/stream."""

    @abc.abstractmethod
    async def subscribe(
        self,
        topic: str,
        handler: MessageHandler,
        *,
        group_id: str = "default",
    ) -> None:
        """Subscribe to a topic with a consumer group.

        The handler is called for each message. If the handler raises, the
        message should NOT be acknowledged (allowing redelivery).

        Args:
            topic: Stream/topic name (e.g. 'core.document.uploaded.v1')
            handler: Async callback receiving the parsed message dict
            group_id: Consumer group name for load balancing and tracking
        """

    @abc.abstractmethod
    async def unsubscribe(self, topic: str) -> None:
        """Stop consuming from a topic."""

    @abc.abstractmethod
    def is_connected(self) -> bool:
        """Check if the connection is alive."""

    # --------------------------------------------------------------------------
    # Convenience: completion notification (shared logic, calls self.publish)
    # --------------------------------------------------------------------------

    async def publish_completion(
        self,
        result: ProcessingResult,
        upload_message: DocumentUploadMessage,
    ) -> None:
        """Publish document processing completion notification.

        Called after a document has been fully processed (success or failure)
        so that intg-svc can update MongoDB status.

        Failures are logged but never re-raised — a completion notification
        failure must not fail the ingestion itself.
        """
        topic = self.settings.mq_completion_topic
        if not topic:
            logger.warning(
                "MQ completion topic not configured, skipping notification",
                document_id=result.document_id,
            )
            return

        completion_message = build_completion_message(result, upload_message)

        try:
            await self.publish(topic, completion_message)
            logger.info(
                "Published completion notification",
                document_id=result.document_id,
                success=result.success,
                topic=topic,
            )
        except Exception as e:
            # Best-effort, but make the drop observable (not a silent loss that
            # leaves the doc stuck "processing" downstream) (#37).
            from src.services.metrics import COMPLETION_PUBLISH_FAILURES_TOTAL

            COMPLETION_PUBLISH_FAILURES_TOTAL.inc()
            logger.error(
                "Failed to publish completion notification",
                document_id=result.document_id,
                error=str(e),
            )
