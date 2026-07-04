"""Unit tests for AuditLogConsumer."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from temporalio.exceptions import WorkflowAlreadyStartedError

from src.services.audit_consumer import AuditLogConsumer


@pytest.fixture
def temporal_client() -> AsyncMock:
    client = AsyncMock()
    client.start_workflow = AsyncMock()
    return client


@pytest.fixture
def consumer(temporal_client: AsyncMock) -> AuditLogConsumer:
    return AuditLogConsumer(
        temporal_client=temporal_client,
        task_queue="audit-writer-queue",
    )


class TestAuditLogConsumer:
    """Tests for AuditLogConsumer.handle()."""

    @pytest.mark.asyncio
    async def test_successful_dispatch(
        self, consumer: AuditLogConsumer, temporal_client: AsyncMock
    ) -> None:
        message = {"audit_id": "abc-123", "workspace_id": "ws-1", "query_text": "test"}

        await consumer.handle(message)

        temporal_client.start_workflow.assert_awaited_once()
        call_kwargs = temporal_client.start_workflow.call_args
        assert call_kwargs.kwargs["id"] == "audit-abc-123"
        assert call_kwargs.kwargs["task_queue"] == "audit-writer-queue"

    @pytest.mark.asyncio
    async def test_missing_audit_id_drops_message(
        self, consumer: AuditLogConsumer, temporal_client: AsyncMock
    ) -> None:
        message = {"workspace_id": "ws-1", "query_text": "test"}

        await consumer.handle(message)

        temporal_client.start_workflow.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_drop_increments_observable_metric(
        self, consumer: AuditLogConsumer, temporal_client: AsyncMock
    ) -> None:
        """A dropped audit event must be observable via a metric, not silent (#18)."""
        from src.services.metrics import AUDIT_MESSAGES_DROPPED_TOTAL

        counter = AUDIT_MESSAGES_DROPPED_TOTAL.labels(reason="missing_audit_id")
        before = counter._value.get()

        await consumer.handle({"workspace_id": "ws-1"})  # no audit_id

        assert counter._value.get() == before + 1
        temporal_client.start_workflow.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_none_audit_id_drops_message(
        self, consumer: AuditLogConsumer, temporal_client: AsyncMock
    ) -> None:
        message = {"audit_id": None, "workspace_id": "ws-1"}

        await consumer.handle(message)

        temporal_client.start_workflow.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_non_string_audit_id_drops_message(
        self, consumer: AuditLogConsumer, temporal_client: AsyncMock
    ) -> None:
        message = {"audit_id": 12345, "workspace_id": "ws-1"}

        await consumer.handle(message)

        temporal_client.start_workflow.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_empty_string_audit_id_drops_message(
        self, consumer: AuditLogConsumer, temporal_client: AsyncMock
    ) -> None:
        message = {"audit_id": "   ", "workspace_id": "ws-1"}

        await consumer.handle(message)

        temporal_client.start_workflow.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_whitespace_audit_id_is_stripped(
        self, consumer: AuditLogConsumer, temporal_client: AsyncMock
    ) -> None:
        message = {"audit_id": "  abc-123  ", "workspace_id": "ws-1"}

        await consumer.handle(message)

        temporal_client.start_workflow.assert_awaited_once()
        call_kwargs = temporal_client.start_workflow.call_args
        assert call_kwargs.kwargs["id"] == "audit-abc-123"

    @pytest.mark.asyncio
    async def test_duplicate_workflow_treated_as_idempotent(
        self, consumer: AuditLogConsumer, temporal_client: AsyncMock
    ) -> None:
        temporal_client.start_workflow.side_effect = WorkflowAlreadyStartedError(
            "audit-abc-123", "WriteAuditLogWorkflow"
        )
        message = {"audit_id": "abc-123", "workspace_id": "ws-1"}

        # Should NOT raise
        await consumer.handle(message)

    @pytest.mark.asyncio
    async def test_other_exception_re_raised_for_mq_retry(
        self, consumer: AuditLogConsumer, temporal_client: AsyncMock
    ) -> None:
        temporal_client.start_workflow.side_effect = RuntimeError("connection lost")
        message = {"audit_id": "abc-123", "workspace_id": "ws-1"}

        with pytest.raises(RuntimeError, match="connection lost"):
            await consumer.handle(message)
