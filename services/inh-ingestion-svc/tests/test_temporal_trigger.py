"""Unit tests for TemporalWorkflowTrigger and get_workflow_trigger factory."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import src.temporal.trigger as trigger_mod
from src.models.document import DocumentUploadMessage
from src.temporal.trigger import (
    TemporalWorkflowTrigger,
    build_ingestion_source_memo,
    get_workflow_trigger,
)


# Override the package-level DB-dependent autouse fixture (tests/conftest.py)
# with a no-op. This module's tests are pure/mocked (no real DatabaseService
# interaction), so they must not skip when PostgreSQL is unavailable -- same
# pattern as tests/test_contracts.py and tests/test_temporal_activities.py.
@pytest.fixture(autouse=True)
def cleanup_test_data():
    """No-op override so this module's tests run without a live database."""
    yield


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_settings():
    """Return a minimal mock Settings object."""
    settings = MagicMock()
    settings.temporal_host = "localhost:7233"
    settings.temporal_namespace = "default"
    settings.temporal_task_queue = "ingestion"
    return settings


def _make_upload_message(**overrides) -> DocumentUploadMessage:
    """Build a minimal valid DocumentUploadMessage, with overrides for the
    ingestion-source fields under test (inherent-systems/prime#187)."""
    base = {
        "event_type": "document.uploaded",
        "document_id": "doc-1",
        "workspace_id": "ws-1",
        "user_id": "user-1",
        "filename": "stored.txt",
        "original_filename": "original.txt",
        "content_type": "text/plain",
        "size_bytes": 10,
        "storage_backend": "local",
        "storage_path": "workspaces/ws-1/stored.txt",
        "timestamp": "2024-01-15T10:30:00Z",
    }
    base.update(overrides)
    return DocumentUploadMessage(**base)


# ---------------------------------------------------------------------------
# _classify_error tests
# ---------------------------------------------------------------------------


class TestClassifyError:
    """Tests for TemporalWorkflowTrigger._classify_error (static method)."""

    def test_extract_keyword_returns_extraction_failed(self):
        result = TemporalWorkflowTrigger._classify_error("Failed to extract text from PDF")
        assert result == "extraction_failed"

    def test_storage_keyword_returns_storage_failed(self):
        result = TemporalWorkflowTrigger._classify_error("Storage write failed")
        assert result == "storage_failed"

    def test_timeout_keyword_returns_timeout(self):
        result = TemporalWorkflowTrigger._classify_error("Connection timeout")
        assert result == "timeout"

    def test_timed_out_keyword_returns_timeout(self):
        result = TemporalWorkflowTrigger._classify_error("Request timed out after 30s")
        assert result == "timeout"

    def test_validation_keyword_returns_validation_failed(self):
        result = TemporalWorkflowTrigger._classify_error("Validation error in schema")
        assert result == "validation_failed"

    def test_invalid_keyword_returns_validation_failed(self):
        result = TemporalWorkflowTrigger._classify_error("Invalid document format")
        assert result == "validation_failed"

    def test_fetch_keyword_returns_fetch_failed(self):
        result = TemporalWorkflowTrigger._classify_error("Could not fetch document from bucket")
        assert result == "fetch_failed"

    def test_unknown_string_returns_unknown(self):
        result = TemporalWorkflowTrigger._classify_error("Something completely unexpected happened")
        assert result == "unknown"


# ---------------------------------------------------------------------------
# Initial state tests
# ---------------------------------------------------------------------------


class TestInitialState:
    """Tests for TemporalWorkflowTrigger constructor and initial internal state."""

    def test_client_is_none_initially(self):
        settings = _make_settings()
        trigger = TemporalWorkflowTrigger(settings)
        assert trigger._client is None

    def test_initialized_is_false_initially(self):
        settings = _make_settings()
        trigger = TemporalWorkflowTrigger(settings)
        assert trigger._initialized is False


# ---------------------------------------------------------------------------
# shutdown() tests
# ---------------------------------------------------------------------------


class TestShutdown:
    """Tests for TemporalWorkflowTrigger.shutdown()."""

    def test_shutdown_resets_client_and_initialized_flag(self):
        settings = _make_settings()
        trigger = TemporalWorkflowTrigger(settings)

        # Simulate an initialized state
        trigger._client = MagicMock()
        trigger._initialized = True

        trigger.shutdown()

        assert trigger._client is None
        assert trigger._initialized is False


# ---------------------------------------------------------------------------
# get_workflow_trigger singleton tests
# ---------------------------------------------------------------------------


class TestGetWorkflowTriggerSingleton:
    """Tests for the get_workflow_trigger() module-level singleton factory."""

    def setup_method(self):
        """Reset the global singleton before each test."""
        trigger_mod._workflow_trigger = None

    def teardown_method(self):
        """Clean up the global singleton after each test."""
        trigger_mod._workflow_trigger = None

    def test_returns_same_instance_on_repeated_calls(self):
        settings = _make_settings()

        first = get_workflow_trigger(settings)
        second = get_workflow_trigger(settings)

        assert first is second

    def test_returns_temporal_workflow_trigger_instance(self):
        settings = _make_settings()
        result = get_workflow_trigger(settings)
        assert isinstance(result, TemporalWorkflowTrigger)

    def test_backfills_db_service_on_existing_singleton(self):
        """A later caller providing db_service must backfill it onto the
        already-created singleton (worker mode creates the trigger before the
        api layer wires db_service), so dead-letter recording is not a no-op (#6)."""
        settings = _make_settings()
        first = get_workflow_trigger(settings)  # created without db_service
        assert first._db_service is None

        db = MagicMock()
        second = get_workflow_trigger(settings, db_service=db)

        assert second is first
        assert first._db_service is db


# ---------------------------------------------------------------------------
# async poison-message handling tests (Fix #6)
# ---------------------------------------------------------------------------


class TestTriggerFailurePathRobustness:
    """A non-validation error before upload_message is bound must not raise an
    UnboundLocalError in the failure path that masks the real error (#39)."""

    @pytest.mark.asyncio
    async def test_non_validation_error_does_not_mask_with_nameerror(self):
        trigger = TemporalWorkflowTrigger(_make_settings())
        trigger._initialized = True
        trigger._mq_service = AsyncMock()

        with patch("src.temporal.trigger.DocumentUploadMessage", side_effect=TypeError("boom")):
            result = await trigger.trigger_workflow({"document_id": "d1"})

        # Clean failure result carrying the real error, not an UnboundLocalError.
        assert result.success is False
        assert "boom" in (result.error or "")
        # No completion publish attempted with an unbound message.
        trigger._mq_service.publish_completion.assert_not_awaited()


class TestAsyncTriggerPoisonHandling:
    """``trigger_workflow_async`` must dead-letter a malformed (poison) message
    and return normally so the MQ consumer ACKs it — never re-raise into an
    infinite redelivery loop. Transient Temporal errors must still raise so the
    message is redelivered (#6)."""

    def _ready_trigger(self, db_service):
        settings = _make_settings()
        trigger = TemporalWorkflowTrigger(settings, db_service=db_service)
        trigger._initialized = True
        trigger._client = MagicMock()
        trigger._client.start_workflow = AsyncMock(return_value=MagicMock())
        return trigger

    @pytest.mark.asyncio
    async def test_poison_message_is_dead_lettered_and_not_raised(self):
        db = MagicMock()
        db.add_dead_letter_job = AsyncMock()
        trigger = self._ready_trigger(db)

        # Malformed message: missing required fields -> validation error.
        result = await trigger.trigger_workflow_async({"document_id": "d1"})

        # Returns normally (no raise) so the consumer ACKs and stops redelivering.
        assert result == ""
        db.add_dead_letter_job.assert_awaited_once()
        # No workflow is started for a poison message.
        trigger._client.start_workflow.assert_not_called()

    @pytest.mark.asyncio
    async def test_transient_temporal_error_still_raises(self, sample_upload_message):
        db = MagicMock()
        db.add_dead_letter_job = AsyncMock()
        trigger = self._ready_trigger(db)
        # Valid message, but Temporal is transiently unavailable.
        trigger._client.start_workflow = AsyncMock(side_effect=RuntimeError("temporal unavailable"))

        with pytest.raises(RuntimeError, match="temporal unavailable"):
            await trigger.trigger_workflow_async(sample_upload_message)

        # Transient errors must NOT be dead-lettered — the message must redeliver.
        db.add_dead_letter_job.assert_not_awaited()


# ---------------------------------------------------------------------------
# Ingestion-source Temporal memo tests (inherent-systems/prime#187)
# ---------------------------------------------------------------------------


class TestBuildSourceMemo:
    """Unit tests for TemporalWorkflowTrigger._build_source_memo (inherent-systems/prime#187).

    Memo needs no namespace search-attribute registration and surfaces
    directly in the Temporal UI workflow summary panel.
    """

    def test_connector_sourced_message_includes_connection_and_sync_id(self):
        upload_message = _make_upload_message(
            source="connector:notion", connection_id="conn_123", sync_id="sync_456"
        )

        memo = TemporalWorkflowTrigger._build_source_memo(upload_message)

        assert memo == {
            "source": "connector:notion",
            "connection_id": "conn_123",
            "sync_id": "sync_456",
        }

    def test_source_only_message_omits_absent_connector_ids(self):
        upload_message = _make_upload_message(source="public-api")

        memo = TemporalWorkflowTrigger._build_source_memo(upload_message)

        assert memo == {"source": "public-api"}
        assert "connection_id" not in memo
        assert "sync_id" not in memo

    def test_legacy_message_without_source_defaults_to_unknown(self):
        # Legacy/in-flight messages produced before inherent-systems/prime#187
        # have no source field at all, which Pydantic leaves as None.
        upload_message = _make_upload_message()
        assert upload_message.source is None

        memo = TemporalWorkflowTrigger._build_source_memo(upload_message)

        assert memo == {"source": "unknown"}

    # Oversized source/connection_id/sync_id handling (#141 adversarial pass)
    # is NOT tested here: it is enforced by `max_length=500` on
    # DocumentUploadMessage itself (services/inh-contracts, see
    # test_oversized_source_fields_are_rejected in
    # services/inh-contracts/tests/test_events.py), so an oversized value can
    # never reach a valid `upload_message` for this method to build a memo
    # from in the first place -- there is nothing for _build_source_memo to
    # guard against.


class TestBuildIngestionSourceMemo:
    """Unit tests for the module-level build_ingestion_source_memo (#178).

    Extracted out of TemporalWorkflowTrigger._build_source_memo so
    POST /ingest (src/api/app.py) can build the identical memo shape for its
    hardcoded source="api-direct" without duplicating (and risking drift
    from) the MQ-path memo logic. TestBuildSourceMemo above pins that the
    staticmethod still produces identical output by delegating here; these
    tests exercise the shared function directly, including the app.py-style
    call shape (source only, no connection_id/sync_id).
    """

    def test_source_only_call_omits_connector_fields(self):
        """The exact call shape src/api/app.py uses: a direct/manual trigger
        has no connector to attribute a connection_id/sync_id to."""
        memo = build_ingestion_source_memo(source="api-direct")
        assert memo == {"source": "api-direct"}

    def test_connector_sourced_call_includes_connection_and_sync_id(self):
        memo = build_ingestion_source_memo(
            source="connector:notion", connection_id="conn_123", sync_id="sync_456"
        )
        assert memo == {
            "source": "connector:notion",
            "connection_id": "conn_123",
            "sync_id": "sync_456",
        }

    def test_none_source_defaults_to_unknown(self):
        memo = build_ingestion_source_memo(source=None)
        assert memo == {"source": "unknown"}

    def test_falsy_connector_ids_are_omitted_not_included_as_empty(self):
        memo = build_ingestion_source_memo(source="api-direct", connection_id="", sync_id="")
        assert memo == {"source": "api-direct"}
        assert "connection_id" not in memo
        assert "sync_id" not in memo

    def test_static_method_delegates_to_shared_function(self):
        """_build_source_memo must produce byte-identical output to calling
        build_ingestion_source_memo directly -- pins the #178 refactor
        didn't change the MQ path's existing (#141) behavior."""
        upload_message = _make_upload_message(
            source="connector:notion", connection_id="conn_123", sync_id="sync_456"
        )
        via_staticmethod = TemporalWorkflowTrigger._build_source_memo(upload_message)
        via_shared_function = build_ingestion_source_memo(
            source=upload_message.source,
            connection_id=upload_message.connection_id,
            sync_id=upload_message.sync_id,
        )
        assert via_staticmethod == via_shared_function


class TestTriggerWorkflowAsyncMemoIntegration:
    """Verify trigger_workflow_async threads the memo through to the actual
    Temporal client.start_workflow call (not just the helper in isolation)."""

    def _ready_trigger(self) -> TemporalWorkflowTrigger:
        trigger = TemporalWorkflowTrigger(_make_settings())
        trigger._initialized = True
        trigger._client = MagicMock()
        trigger._client.start_workflow = AsyncMock(return_value=MagicMock())
        return trigger

    @pytest.mark.asyncio
    async def test_connector_sourced_message_passes_full_memo(
        self, sample_upload_message_connector_sourced
    ):
        trigger = self._ready_trigger()

        await trigger.trigger_workflow_async(sample_upload_message_connector_sourced)

        _, kwargs = trigger._client.start_workflow.call_args
        assert kwargs["memo"] == {
            "source": "connector:notion",
            "connection_id": "conn_123",
            "sync_id": "sync_456",
        }

    @pytest.mark.asyncio
    async def test_public_api_message_passes_source_only_memo(
        self, sample_upload_message_public_api
    ):
        trigger = self._ready_trigger()

        await trigger.trigger_workflow_async(sample_upload_message_public_api)

        _, kwargs = trigger._client.start_workflow.call_args
        assert kwargs["memo"] == {"source": "public-api"}

    @pytest.mark.asyncio
    async def test_legacy_message_without_source_passes_unknown_memo(self, sample_upload_message):
        # sample_upload_message has no "source" key at all — simulates an
        # in-flight message produced before inherent-systems/prime#187 shipped on the intg-svc side.
        trigger = self._ready_trigger()

        await trigger.trigger_workflow_async(sample_upload_message)

        _, kwargs = trigger._client.start_workflow.call_args
        assert kwargs["memo"] == {"source": "unknown"}


class TestSyncTriggerWorkflowMemoIntegration:
    """Verify trigger_workflow (the synchronous, wait-for-result path) also
    threads the memo through to client.start_workflow (#141 follow-up: the
    async-path test class above does not exercise this method, and deleting
    only the async-path memo= line left this one uncovered).

    trigger_workflow has no production caller today (grep confirms nothing
    outside this test file and its own definition calls it) -- the MQ
    consumer uses trigger_workflow_async exclusively. This test exists so the
    memo stays correct here too if/when it grows a caller, not because it is
    presently load-bearing.
    """

    def _ready_trigger(self) -> TemporalWorkflowTrigger:
        from src.temporal.models import WorkflowResult

        trigger = TemporalWorkflowTrigger(_make_settings())
        trigger._initialized = True
        trigger._mq_service = AsyncMock()
        trigger._client = MagicMock()
        handle = MagicMock()
        handle.result = AsyncMock(return_value=WorkflowResult(document_id="doc-1", success=True))
        trigger._client.start_workflow = AsyncMock(return_value=handle)
        return trigger

    @pytest.mark.asyncio
    async def test_connector_sourced_message_passes_full_memo(
        self, sample_upload_message_connector_sourced
    ):
        trigger = self._ready_trigger()

        await trigger.trigger_workflow(sample_upload_message_connector_sourced)

        _, kwargs = trigger._client.start_workflow.call_args
        assert kwargs["memo"] == {
            "source": "connector:notion",
            "connection_id": "conn_123",
            "sync_id": "sync_456",
        }


# Workflow-id-collision supersession tests (#110)
# ---------------------------------------------------------------------------
#
# Both start_workflow call sites use a *fixed* id (f"ingest-{document_id}").
# Before this fix, a re-index enqueued while the prior run for that
# document_id was still open raised WorkflowAlreadyStartedError with the
# default id_conflict_policy (UNSPECIFIED). On the async (MQ) path that
# exception propagates out of trigger_workflow_async, so the message is never
# ACKed; RedisMQService only retries it once XAUTOCLAIM reclaims it (idle >=
# 30s, see src/services/mq/redis_mq.py:46) — and only after a *new* message
# also arrives, since the reclaim call is skipped whenever a poll iteration
# reads no fresh entries (redis_mq.py:216-233). Every retry collides with the
# same still-open run and fails again, so the caller effectively waits out
# however long the stale run takes to close on its own — the ~10min observed
# in CI run 29222060795, not a fixed timeout constant.
#
# Fix: pass id_conflict_policy=TERMINATE_EXISTING so Temporal supersedes the
# stale run with the fresh one at start time instead of raising. This is the
# right call for an AI-agent caller that will retry: re-index/refresh always
# means "the current content should win", so terminating a stale run in
# favor of a fresh one is correct, not a race to avoid — and it turns the
# ~10 minute stall into a normal, fast run.


class TestWorkflowIdConflictPolicySupersedesStaleRun:
    """A re-index/refresh under an open prior run must supersede it, not
    collide and stall (#110)."""

    def _ready_trigger(self):
        """Trigger with a mocked Temporal client whose start_workflow succeeds
        and whose handle resolves to a completed WorkflowResult."""
        settings = _make_settings()
        trigger = TemporalWorkflowTrigger(settings)
        trigger._initialized = True
        trigger._client = MagicMock()

        handle = MagicMock()
        handle.result = AsyncMock(
            return_value=MagicMock(
                document_id="test_doc_12345",
                success=True,
                chunks_created=1,
                error=None,
                processing_time_ms=1,
            )
        )
        trigger._client.start_workflow = AsyncMock(return_value=handle)
        return trigger

    @pytest.mark.asyncio
    async def test_async_trigger_passes_terminate_existing_conflict_policy(
        self, sample_upload_message
    ):
        """trigger_workflow_async (the MQ-driven re-index/refresh path) must
        ask Temporal to terminate-and-supersede a same-id run in flight,
        instead of relying on the default (raise) behavior."""
        from temporalio.common import WorkflowIDConflictPolicy

        trigger = self._ready_trigger()

        await trigger.trigger_workflow_async(sample_upload_message)

        _, kwargs = trigger._client.start_workflow.call_args
        assert kwargs.get("id_conflict_policy") == WorkflowIDConflictPolicy.TERMINATE_EXISTING

    @pytest.mark.asyncio
    async def test_sync_trigger_passes_terminate_existing_conflict_policy(
        self, sample_upload_message
    ):
        """trigger_workflow (the blocking twin) carries the same fixed-id
        exposure and must use the same supersession policy."""
        from temporalio.common import WorkflowIDConflictPolicy

        trigger = self._ready_trigger()

        await trigger.trigger_workflow(sample_upload_message)

        _, kwargs = trigger._client.start_workflow.call_args
        assert kwargs.get("id_conflict_policy") == WorkflowIDConflictPolicy.TERMINATE_EXISTING

    @pytest.mark.asyncio
    async def test_async_trigger_supersedes_a_still_open_prior_run(self, sample_upload_message):
        """Behavioral regression guard for the actual stall (#110).

        Fakes the real Temporal server contract: a start_workflow call for an
        id with an open run raises WorkflowAlreadyStartedError *unless* the
        caller opted into conflict resolution via
        id_conflict_policy=TERMINATE_EXISTING, in which case the server
        terminates the stale run and starts the new one instead of raising.
        This fails against the pre-fix code (no id_conflict_policy kwarg ->
        the fake raises, same as the real server) and passes once the fix
        sets it -- unlike a mock that always succeeds, this actually exercises
        the collision branch that caused the MQ redelivery stall."""
        from temporalio.common import WorkflowIDConflictPolicy
        from temporalio.exceptions import WorkflowAlreadyStartedError

        trigger = self._ready_trigger()

        async def fake_start_workflow(*args, **kwargs):
            if kwargs.get("id_conflict_policy") != WorkflowIDConflictPolicy.TERMINATE_EXISTING:
                raise WorkflowAlreadyStartedError(kwargs["id"], "test-run-id")
            return MagicMock()

        trigger._client.start_workflow = AsyncMock(side_effect=fake_start_workflow)

        # Must resolve fast with the new run's id, not raise and leave the
        # MQ message unacked for redelivery.
        workflow_id = await trigger.trigger_workflow_async(sample_upload_message)

        assert workflow_id == "ingest-test_doc_12345"
