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
        self.activity_kwargs: list[tuple[str, dict]] = []
        self.logger = MagicMock()

    def now(self):
        return datetime.now(UTC)

    def info(self):
        # start_time (#110 follow-up): the real workflow.info().start_time is
        # used to make the fencing claim monotonic (see
        # DocumentIngestionWorkflow.run / DatabaseService.create_pending_document).
        return SimpleNamespace(run_id="run-1", start_time=datetime.now(UTC))

    def execute_activity(self, activity_fn, arg, **kwargs):
        name = getattr(activity_fn, "__name__", str(activity_fn))
        self.calls.append((name, arg))
        self.activity_kwargs.append((name, kwargs))

        async def _run():
            if name in self.raising:
                raise self.raising[name]
            return self.outputs.get(name)

        return _run()

    def calls_for(self, name: str) -> list[object]:
        return [arg for n, arg in self.calls if n == name]

    def kwargs_for(self, name: str) -> list[dict]:
        return [kw for n, kw in self.activity_kwargs if n == name]


HAPPY_OUTPUTS = {
    "ensure_tenant_ready": EnsureTenantOutput(tenant_id=1, workspace_ready=True),
    "extract_text": ExtractTextOutput(text_length=100),
    "chunk_text": ChunkTextOutput(chunk_count=3),
    "store_in_postgresql": StoreDocumentOutput(success=True, chunks_stored=3),
    "store_in_weaviate": StoreDocumentOutput(success=True, chunks_stored=3),
    "publish_completion": True,
}


class TestWeaviateStoreBudgetWiring:
    """#228: store_in_weaviate StartToClose must scale with chunk_count."""

    @pytest.mark.asyncio
    async def test_store_in_weaviate_timeout_scales_with_chunk_count(self):
        from datetime import timedelta

        from src.temporal.weaviate_store_budget import weaviate_store_timeout
        from src.temporal.workflows import document_ingestion

        outputs = dict(HAPPY_OUTPUTS)
        # 44 chunks → 2 serial batches under the budget formula (2*100+30=230).
        outputs["chunk_text"] = ChunkTextOutput(chunk_count=44)
        outputs["store_in_postgresql"] = StoreDocumentOutput(success=True, chunks_stored=44)
        outputs["store_in_weaviate"] = StoreDocumentOutput(success=True, chunks_stored=44)
        fake = FakeWorkflowModule(outputs)
        wf = document_ingestion.DocumentIngestionWorkflow()
        with patch.object(document_ingestion, "workflow", fake):
            result = await wf.run(make_workflow_input())

        assert result.success is True
        wv_kwargs = fake.kwargs_for("store_in_weaviate")
        assert len(wv_kwargs) == 1
        assert wv_kwargs[0]["start_to_close_timeout"] == weaviate_store_timeout(44)
        # Serial worst-case (retries×timeout + sleep budget + overhead per batch).
        assert wv_kwargs[0]["start_to_close_timeout"] == timedelta(seconds=230)
        # #229: longer initial retry interval than the old 2s lockstep default.
        assert wv_kwargs[0]["retry_policy"].initial_interval == timedelta(seconds=5)


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
    async def test_postgresql_failure_publishes_failed_event_and_raises(self):
        """#230: document failure must raise after side-effects so Temporal
        close status is Failed, not Completed with success=False."""
        from temporalio.exceptions import ApplicationError

        from src.temporal.workflows import document_ingestion

        outputs = dict(HAPPY_OUTPUTS)
        outputs["store_in_postgresql"] = StoreDocumentOutput(
            success=False, chunks_stored=0, error="pg down"
        )
        fake = FakeWorkflowModule(outputs)
        wf = document_ingestion.DocumentIngestionWorkflow()
        with patch.object(document_ingestion, "workflow", fake):
            with pytest.raises(ApplicationError) as ei:
                await wf.run(make_workflow_input())

        from src.temporal.document_failure import DOCUMENT_INGESTION_FAILED_TYPE

        assert ei.value.type == DOCUMENT_INGESTION_FAILED_TYPE
        assert "pg down" in (ei.value.message or "")
        publishes = fake.calls_for("publish_completion")
        assert len(publishes) == 1
        assert publishes[0].success is False
        assert "pg down" in (publishes[0].error or "")
        # Cleanup still runs (finally) before the raise.
        assert fake.calls_for("cleanup_staging")

    @pytest.mark.asyncio
    async def test_weaviate_failure_publishes_failed_event_and_raises(self):
        from temporalio.exceptions import ApplicationError

        from src.temporal.document_failure import DOCUMENT_INGESTION_FAILED_TYPE
        from src.temporal.workflows import document_ingestion

        outputs = dict(HAPPY_OUTPUTS)
        outputs["store_in_weaviate"] = StoreDocumentOutput(
            success=False, chunks_stored=0, error="weaviate down"
        )
        fake = FakeWorkflowModule(outputs)
        wf = document_ingestion.DocumentIngestionWorkflow()
        with patch.object(document_ingestion, "workflow", fake):
            with pytest.raises(ApplicationError) as ei:
                await wf.run(make_workflow_input())

        assert ei.value.type == DOCUMENT_INGESTION_FAILED_TYPE
        assert "weaviate down" in (ei.value.message or "")
        publishes = fake.calls_for("publish_completion")
        assert len(publishes) == 1
        assert publishes[0].success is False
        assert fake.calls_for("cleanup_staging")

    @pytest.mark.asyncio
    async def test_store_in_weaviate_activity_raise_cleanup_then_document_failure(self):
        """store_in_weaviate re-raises on TEI/Weaviate errors so gather fails
        into the outer except — same terminal raise as success=False (#230)."""
        from temporalio.exceptions import ApplicationError

        from src.temporal.document_failure import DOCUMENT_INGESTION_FAILED_TYPE
        from src.temporal.workflows import document_ingestion

        fake = FakeWorkflowModule(
            dict(HAPPY_OUTPUTS),
            raising={"store_in_weaviate": RuntimeError("activity StartToClose timeout")},
        )
        wf = document_ingestion.DocumentIngestionWorkflow()
        with patch.object(document_ingestion, "workflow", fake):
            with pytest.raises(ApplicationError) as ei:
                await wf.run(make_workflow_input())

        assert ei.value.type == DOCUMENT_INGESTION_FAILED_TYPE
        assert "StartToClose timeout" in (ei.value.message or "")
        assert fake.calls_for("cleanup_staging")
        publishes = fake.calls_for("publish_completion")
        assert len(publishes) == 1
        assert publishes[0].success is False

    @pytest.mark.asyncio
    async def test_unexpected_activity_error_publishes_failed_event_and_raises(self):
        from temporalio.exceptions import ApplicationError

        from src.temporal.document_failure import DOCUMENT_INGESTION_FAILED_TYPE
        from src.temporal.workflows import document_ingestion

        fake = FakeWorkflowModule(
            dict(HAPPY_OUTPUTS), raising={"extract_text": RuntimeError("boom")}
        )
        wf = document_ingestion.DocumentIngestionWorkflow()
        with patch.object(document_ingestion, "workflow", fake):
            with pytest.raises(ApplicationError) as ei:
                await wf.run(make_workflow_input())

        assert ei.value.type == DOCUMENT_INGESTION_FAILED_TYPE
        assert "boom" in (ei.value.message or "")
        publishes = fake.calls_for("publish_completion")
        assert len(publishes) == 1
        assert publishes[0].success is False
        assert "boom" in (publishes[0].error or "")

    @pytest.mark.asyncio
    async def test_activity_error_cause_reaches_error_message_not_generic_wrapper_text(self):
        """Review follow-up (the exact trap CLAUDE.md/learnings.md warns
        about): `workflow.execute_activity` wraps the activity's real
        exception in a Temporal `ActivityError` whose OWN `str()` is always
        the generic, hardcoded "Activity task failed" -- the actual cause
        (here, `_extract_pdf_text`'s non-retryable `ApplicationError`) lives
        on `.cause`. Before this fix, `run()`'s `except Exception as e:`
        interpolated `str(e)` directly at every site (document status,
        dead-letter row, completion event, workflow result) -- so EVERY
        extraction failure read "Activity task failed" instead of the real
        cause, and `_classify_error("Activity task failed")` matches none of
        its keywords, so it classified as "unknown" and never surfaced via
        `GET /dead-letter?error_type=extraction_failed`.

        This constructs a REAL `temporalio.exceptions.ActivityError` (not a
        hand-fed string to `_classify_error` -- that only proves the
        classifier's own keyword matching works, not that the real error
        ever reaches it) with `.cause` set to the ApplicationError
        `_extract_pdf_text` actually raises, and asserts the cause -- not
        the wrapper's generic text -- reaches every one of the sites
        `run()`'s except block writes to, AND that dead-letter recording
        classifies it as "extraction_failed" through the real
        `_record_dead_letter_best_effort` -> `_classify_error` path.
        #230: the terminal raise also carries the cause message."""
        from temporalio.exceptions import ActivityError, ApplicationError, RetryState

        from src.temporal.workflows import document_ingestion

        # The real, non-retryable ApplicationError _extract_pdf_text raises
        # (#195) for a corrupt PDF -- this is what ends up on `.cause`.
        real_cause = ApplicationError(
            "PDF extraction failed: could not read the document "
            "(PdfStreamError: Stream has ended unexpectedly). The file may "
            "be corrupt, truncated, password-protected, or not actually a "
            "PDF despite its declared type.",
            type="PdfOpenFailed",
            non_retryable=True,
        )
        # A real ActivityError, constructed the way the Temporal SDK
        # constructs one -- its own message is ALWAYS this generic string,
        # regardless of what the activity actually raised.
        activity_error = ActivityError(
            "Activity task failed",
            scheduled_event_id=1,
            started_event_id=2,
            identity="worker-1",
            activity_type="extract_text",
            activity_id="1",
            retry_state=RetryState.RETRY_POLICY_NOT_SET,
        )
        activity_error.__cause__ = real_cause
        assert str(activity_error) == "Activity task failed"  # sanity: the trap is real

        fake = FakeWorkflowModule(dict(HAPPY_OUTPUTS), raising={"extract_text": activity_error})
        wf = document_ingestion.DocumentIngestionWorkflow()
        with patch.object(document_ingestion, "workflow", fake):
            with pytest.raises(ApplicationError) as ei:
                await wf.run(make_workflow_input())

        from src.temporal.document_failure import DOCUMENT_INGESTION_FAILED_TYPE

        # Terminal ApplicationError (#230) carries the CAUSE, not the wrapper.
        assert ei.value.type == DOCUMENT_INGESTION_FAILED_TYPE
        assert "Activity task failed" not in (ei.value.message or "")
        assert "PDF extraction failed" in (ei.value.message or "")
        assert "PdfStreamError" in (ei.value.message or "")

        # set_document_status: same cause, not the generic wrapper text.
        # (An earlier 'processing' status write with no error_message also
        # happens on the happy path before extraction runs -- the FAILURE
        # write, the last call, is the one this pins.)
        status_calls = fake.calls_for("set_document_status")
        failed_status_calls = [c for c in status_calls if c.status == "failed"]
        assert len(failed_status_calls) == 1
        assert "Activity task failed" not in failed_status_calls[0].error_message
        assert "PDF extraction failed" in failed_status_calls[0].error_message

        # record_dead_letter: same cause AND classified correctly through
        # the real _classify_error call inside
        # _record_dead_letter_best_effort -- not a hand-fed string.
        dead_letter_calls = fake.calls_for("record_dead_letter")
        assert len(dead_letter_calls) == 1
        assert "Activity task failed" not in dead_letter_calls[0].error_message
        assert "PDF extraction failed" in dead_letter_calls[0].error_message
        assert dead_letter_calls[0].error_type == "extraction_failed"

        # publish_completion (document.failed event): same cause.
        publishes = fake.calls_for("publish_completion")
        assert len(publishes) == 1
        assert "Activity task failed" not in (publishes[0].error or "")
        assert "PDF extraction failed" in (publishes[0].error or "")

        assert fake.calls_for("cleanup_staging")

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
