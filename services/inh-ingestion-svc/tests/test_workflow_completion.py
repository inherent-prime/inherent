"""Tests for the workflow-owned completion event (#88).

Worker mode starts workflows via the fire-and-forget trigger, so the
document.processed / document.failed contract must be fulfilled from INSIDE
DocumentIngestionWorkflow as a final activity — not from the (dead in worker
mode) synchronous trigger path.

Covers:
- the publish_completion activity: message shape, topic gating, error
  propagation (so Temporal retries a failed publish)
- DocumentIngestionWorkflow wiring: exactly one completion event per run, on
  success and on every failure path, and best-effort semantics (a broken MQ
  must not fail an otherwise-complete ingestion)
- worker registration: the activity ships with the worker
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.temporal.activities.completion import publish_completion
from src.temporal.models import (
    ChunkTextOutput,
    DocumentIngestionInput,
    EnsureTenantOutput,
    ExtractTextOutput,
    PublishCompletionInput,
    StoreDocumentOutput,
)


@pytest.fixture(autouse=True)
def cleanup_test_data():
    """No-op override of the package-level DB-dependent autouse fixture.

    Everything here runs against mocks — no PostgreSQL required.
    """
    yield


def make_completion_input(**overrides: object) -> PublishCompletionInput:
    base: dict = {
        "document_id": "doc-1",
        "workspace_id": "ws-1",
        "user_id": "user-1",
        "filename": "stored.txt",
        "original_filename": "original.txt",
        "content_type": "text/plain",
        "size_bytes": 1024,
        "storage_backend": "s3",
        "storage_path": "workspaces/ws-1/stored.txt",
        "storage_bucket": "docs",
        "storage_url": "https://s3.example.com/docs/stored.txt",
        "success": True,
        "chunks_created": 3,
        "error": None,
        "processing_time_ms": 500,
        "timestamp": "2026-01-01T00:00:00Z",
    }
    base.update(overrides)
    return PublishCompletionInput(**base)


def make_mock_mq(topic: str | None = "core.document.processed.v1") -> MagicMock:
    mq = MagicMock()
    mq.settings.mq_completion_topic = topic
    mq.publish = AsyncMock()
    return mq


# ---------------------------------------------------------------------------
# publish_completion activity
# ---------------------------------------------------------------------------


class TestPublishCompletionActivity:
    @pytest.mark.asyncio
    async def test_success_publishes_document_processed(self):
        mq = make_mock_mq()
        with patch("src.temporal.shared_services.get_mq_service", new=AsyncMock(return_value=mq)):
            published = await publish_completion(make_completion_input())

        assert published is True
        mq.publish.assert_awaited_once()
        topic, message = mq.publish.call_args[0]
        assert topic == "core.document.processed.v1"
        assert message["event_type"] == "document.processed"
        assert message["status"] == "ready"
        assert message["success"] is True
        assert message["document_id"] == "doc-1"
        assert message["workspace_id"] == "ws-1"
        assert message["user_id"] == "user-1"
        assert message["original_filename"] == "original.txt"
        assert message["chunks_created"] == 3
        assert message["processing_time_ms"] == 500
        # Storage metadata for downstream document creation (intg-svc).
        assert message["storage_backend"] == "s3"
        assert message["storage_path"] == "workspaces/ws-1/stored.txt"
        assert message["storage_bucket"] == "docs"
        assert message["content_type"] == "text/plain"
        assert message["size_bytes"] == 1024
        assert "timestamp" in message

    @pytest.mark.asyncio
    async def test_failure_publishes_document_failed(self):
        mq = make_mock_mq()
        with patch("src.temporal.shared_services.get_mq_service", new=AsyncMock(return_value=mq)):
            published = await publish_completion(
                make_completion_input(success=False, chunks_created=0, error="Weaviate exploded")
            )

        assert published is True
        _topic, message = mq.publish.call_args[0]
        assert message["event_type"] == "document.failed"
        assert message["status"] == "failed"
        assert message["success"] is False
        assert message["error"] == "Weaviate exploded"

    @pytest.mark.asyncio
    async def test_no_topic_configured_skips_quietly(self):
        mq = make_mock_mq(topic=None)
        with patch("src.temporal.shared_services.get_mq_service", new=AsyncMock(return_value=mq)):
            published = await publish_completion(make_completion_input())

        assert published is False
        mq.publish.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_publish_error_propagates_for_temporal_retry(self):
        """Unlike the old best-effort trigger path, a publish failure must raise
        so Temporal's retry policy re-attempts it."""
        mq = make_mock_mq()
        mq.publish = AsyncMock(side_effect=RuntimeError("redis down"))
        with patch("src.temporal.shared_services.get_mq_service", new=AsyncMock(return_value=mq)):
            with pytest.raises(RuntimeError, match="redis down"):
                await publish_completion(make_completion_input())

    def test_activity_registered_with_worker(self):
        """The activity must ship with the ingestion worker, or the workflow
        call fails at runtime with 'activity not registered'."""
        from src.temporal.worker import _ALL_ACTIVITIES

        assert publish_completion in _ALL_ACTIVITIES


# ---------------------------------------------------------------------------
# DocumentIngestionWorkflow wiring
# ---------------------------------------------------------------------------


def make_workflow_input(**overrides: object) -> DocumentIngestionInput:
    base: dict = {
        "document_id": "doc-1",
        "workspace_id": "ws-1",
        "user_id": "user-1",
        "filename": "stored.txt",
        "original_filename": "original.txt",
        "content_type": "text/plain",
        "size_bytes": 1024,
        "storage_backend": "s3",
        "storage_path": "workspaces/ws-1/stored.txt",
        "storage_bucket": "docs",
        "storage_url": "https://s3.example.com/docs/stored.txt",
        "timestamp": "2026-01-01T00:00:00Z",
    }
    base.update(overrides)
    return DocumentIngestionInput(**base)


class FakeWorkflowModule:
    """Stand-in for temporalio's `workflow` module inside run().

    execute_activity returns a coroutine resolving to a canned per-activity
    output (so it works both awaited directly and via asyncio.gather), and
    records every call for assertions.
    """

    def __init__(self, outputs: dict, raising: dict | None = None):
        self.outputs = outputs
        self.raising = raising or {}
        self.calls: list[tuple[str, object]] = []
        self.logger = MagicMock()

    def now(self):
        return datetime.now(UTC)

    def info(self):
        return SimpleNamespace(run_id="run-1")

    def execute_activity(self, activity_fn, arg, **kwargs):
        name = getattr(activity_fn, "__name__", str(activity_fn))
        self.calls.append((name, arg))

        async def _run():
            if name in self.raising:
                raise self.raising[name]
            return self.outputs.get(name)

        return _run()

    def calls_for(self, name: str) -> list[object]:
        return [arg for n, arg in self.calls if n == name]


HAPPY_OUTPUTS = {
    "ensure_tenant_ready": EnsureTenantOutput(tenant_id=1, workspace_ready=True),
    "extract_text": ExtractTextOutput(text_length=100),
    "chunk_text": ChunkTextOutput(chunk_count=3),
    "store_in_postgresql": StoreDocumentOutput(success=True, chunks_stored=3),
    "store_in_weaviate": StoreDocumentOutput(success=True, chunks_stored=3),
    "publish_completion": True,
}


class TestWorkflowPublishesCompletion:
    @pytest.mark.asyncio
    async def test_success_publishes_exactly_one_processed_event(self):
        from src.temporal.workflows import document_ingestion

        fake = FakeWorkflowModule(dict(HAPPY_OUTPUTS))
        wf = document_ingestion.DocumentIngestionWorkflow()
        with patch.object(document_ingestion, "workflow", fake):
            result = await wf.run(make_workflow_input())

        assert result.success is True
        publishes = fake.calls_for("publish_completion")
        assert len(publishes) == 1
        completion = publishes[0]
        assert completion.success is True
        assert completion.chunks_created == 3
        assert completion.document_id == "doc-1"
        assert completion.workspace_id == "ws-1"
        # Storage metadata must flow through for downstream consumers.
        assert completion.storage_backend == "s3"
        assert completion.original_filename == "original.txt"

    @pytest.mark.asyncio
    async def test_postgresql_failure_publishes_failed_event(self):
        from src.temporal.workflows import document_ingestion

        outputs = dict(HAPPY_OUTPUTS)
        outputs["store_in_postgresql"] = StoreDocumentOutput(
            success=False, chunks_stored=0, error="pg down"
        )
        fake = FakeWorkflowModule(outputs)
        wf = document_ingestion.DocumentIngestionWorkflow()
        with patch.object(document_ingestion, "workflow", fake):
            result = await wf.run(make_workflow_input())

        assert result.success is False
        publishes = fake.calls_for("publish_completion")
        assert len(publishes) == 1
        assert publishes[0].success is False
        assert "pg down" in (publishes[0].error or "")

    @pytest.mark.asyncio
    async def test_weaviate_failure_publishes_failed_event(self):
        from src.temporal.workflows import document_ingestion

        outputs = dict(HAPPY_OUTPUTS)
        outputs["store_in_weaviate"] = StoreDocumentOutput(
            success=False, chunks_stored=0, error="weaviate down"
        )
        fake = FakeWorkflowModule(outputs)
        wf = document_ingestion.DocumentIngestionWorkflow()
        with patch.object(document_ingestion, "workflow", fake):
            result = await wf.run(make_workflow_input())

        assert result.success is False
        publishes = fake.calls_for("publish_completion")
        assert len(publishes) == 1
        assert publishes[0].success is False

    @pytest.mark.asyncio
    async def test_unexpected_activity_error_publishes_failed_event(self):
        from src.temporal.workflows import document_ingestion

        fake = FakeWorkflowModule(
            dict(HAPPY_OUTPUTS), raising={"extract_text": RuntimeError("boom")}
        )
        wf = document_ingestion.DocumentIngestionWorkflow()
        with patch.object(document_ingestion, "workflow", fake):
            result = await wf.run(make_workflow_input())

        assert result.success is False
        publishes = fake.calls_for("publish_completion")
        assert len(publishes) == 1
        assert publishes[0].success is False
        assert "boom" in (publishes[0].error or "")

    @pytest.mark.asyncio
    async def test_publish_failure_does_not_fail_successful_ingestion(self):
        """Completion publishing is best-effort at the workflow level: after
        Temporal's retries are exhausted, ingestion must still return success."""
        from src.temporal.workflows import document_ingestion

        fake = FakeWorkflowModule(
            dict(HAPPY_OUTPUTS), raising={"publish_completion": RuntimeError("mq gone")}
        )
        wf = document_ingestion.DocumentIngestionWorkflow()
        with patch.object(document_ingestion, "workflow", fake):
            result = await wf.run(make_workflow_input())

        assert result.success is True
        assert result.chunks_created == 3
