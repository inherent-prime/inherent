"""Audit log MQ consumer.

Receives audit events from the ``audit.log.write`` Redis Stream and
dispatches each one as a Temporal ``WriteAuditLogWorkflow``.

If the Temporal dispatch fails the handler re-raises so that the MQ
does NOT acknowledge the message, allowing automatic retry.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog
from temporalio.exceptions import WorkflowAlreadyStartedError

if TYPE_CHECKING:
    from temporalio.client import Client

logger = structlog.get_logger(__name__)


class AuditLogConsumer:
    """Bridges the MQ audit stream to Temporal workflows."""

    def __init__(self, *, temporal_client: Client, task_queue: str) -> None:
        self._client = temporal_client
        self._task_queue = task_queue

    async def handle(self, message: dict[str, Any]) -> None:
        """Handle an incoming audit event message.

        Args:
            message: Parsed message dict from the MQ stream.

        Raises:
            Exception: Re-raises on Temporal dispatch failure so the
                message is not acknowledged and can be retried.
        """
        audit_id = message.get("audit_id")
        if not isinstance(audit_id, str) or not audit_id.strip():
            # Permanently invalid (retry can't fix it) so we drop — but make the
            # drop observable via a metric, not just a warning log (#18).
            from src.services.metrics import AUDIT_MESSAGES_DROPPED_TOTAL

            AUDIT_MESSAGES_DROPPED_TOTAL.labels(reason="missing_audit_id").inc()
            logger.warning(
                "Dropping audit message without valid audit_id", message_keys=list(message)
            )
            return
        audit_id = audit_id.strip()

        logger.info("Dispatching audit workflow", audit_id=audit_id)

        try:
            from src.temporal.workflows.audit_log import WriteAuditLogWorkflow

            await self._client.start_workflow(
                WriteAuditLogWorkflow.run,
                message,
                id=f"audit-{audit_id}",
                task_queue=self._task_queue,
            )
            logger.info("Audit workflow started", audit_id=audit_id)
        except WorkflowAlreadyStartedError:
            logger.info(
                "Audit workflow already started; treating as idempotent",
                audit_id=audit_id,
            )
            return
        except Exception:
            logger.error(
                "Failed to start audit workflow — re-raising for MQ retry",
                audit_id=audit_id,
                exc_info=True,
            )
            raise
