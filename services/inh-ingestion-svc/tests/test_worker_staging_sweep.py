"""Tests for the periodic staging-row sweep (#110 blocker 2).

Before this fix, `_cleanup_stale_staging` ran exactly once, at worker
startup (see `worker.py`'s docstrings on `_cleanup_stale_staging` and
`_periodic_staging_cleanup` for the full mechanism). That was correctly
described as a "1-hour safety net" for a CRASHED worker (crash -> restart ->
sweep runs again) but was NOT actually periodic -- there was no scheduler,
no loop, just the one startup call. #110's TERMINATE_EXISTING conflict
policy means a workflow can now be terminated (superseded) as a routine
event on an otherwise-healthy, long-running worker; termination skips the
workflow's `finally: cleanup_staging` entirely (Temporal closes the
execution without delivering another workflow task), so every superseded
re-index now orphans one `extracted_text` row and one `chunks` row in
`ingestion_staging` with no compensating cleanup until the worker happens to
restart. This file pins the periodic sweep that closes that gap.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.temporal.worker import TemporalWorkerManager, _periodic_staging_cleanup, run_worker


@pytest.fixture(autouse=True)
async def cleanup_test_data():
    """Override the global autouse DB fixture — these tests never touch Postgres."""
    yield


@pytest.fixture()
def db_service():
    yield None


# ---------------------------------------------------------------------------
# _periodic_staging_cleanup: the actual periodic-loop logic
# ---------------------------------------------------------------------------


class TestPeriodicStagingCleanup:
    """_periodic_staging_cleanup must call _cleanup_stale_staging repeatedly
    on the given interval, not just once, until cancelled."""

    @pytest.mark.asyncio
    async def test_sweeps_repeatedly_until_cancelled(self):
        settings = MagicMock()
        calls = 0

        async def fake_cleanup(_settings):
            nonlocal calls
            calls += 1

        # A tiny interval so the test runs fast; real production uses
        # _STAGING_SWEEP_INTERVAL_SECONDS (15 minutes).
        with patch("src.temporal.worker._cleanup_stale_staging", side_effect=fake_cleanup):
            task = asyncio.create_task(_periodic_staging_cleanup(settings, interval_seconds=0.01))
            # Give the loop enough wall-clock time for several sweeps.
            await asyncio.sleep(0.06)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        # At least 2 sweeps in ~60ms at a 10ms interval -- proves this is a
        # LOOP, not the pre-fix one-shot call (which would leave calls == 0,
        # since the very first sweep only fires after the first interval
        # elapses, and a one-shot implementation wouldn't loop back at all).
        assert calls >= 2, f"expected repeated sweeps, got {calls}"

    @pytest.mark.asyncio
    async def test_first_sweep_does_not_fire_immediately(self):
        """The startup sweep (_cleanup_stale_staging, called directly by
        run_worker/TemporalWorkerManager.start before this loop starts)
        already covers t=0 -- this loop must wait a full interval before its
        first sweep, not double up immediately."""
        settings = MagicMock()
        calls = 0

        async def fake_cleanup(_settings):
            nonlocal calls
            calls += 1

        with patch("src.temporal.worker._cleanup_stale_staging", side_effect=fake_cleanup):
            task = asyncio.create_task(_periodic_staging_cleanup(settings, interval_seconds=10))
            await asyncio.sleep(0)  # let the task start and hit its first sleep
            assert calls == 0
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task


# ---------------------------------------------------------------------------
# TemporalWorkerManager: wires the sweep into start()/stop()
# ---------------------------------------------------------------------------


class TestTemporalWorkerManagerStagingSweepLifecycle:
    """start() must create the sweep task; stop() must cancel it -- a leaked
    background task would keep sweeping (harmless) but also keep a
    DB-connecting StagingService alive past shutdown."""

    def _settings(self) -> MagicMock:
        settings = MagicMock()
        settings.temporal_task_queue = "document-ingestion"
        settings.temporal_audit_task_queue = "audit"
        settings.temporal_max_concurrent_activities = 10
        settings.temporal_max_concurrent_workflow_tasks = 10
        return settings

    @staticmethod
    def _mock_worker_class() -> MagicMock:
        """A Worker() replacement usable as `async with worker:` -- a bare
        MagicMock's __aenter__/__aexit__ are not awaitable, so
        _run_with_shutdown's `async with self._worker, self._audit_worker:`
        would raise TypeError without this."""
        worker_cls = MagicMock()
        instance = worker_cls.return_value
        instance.__aenter__ = AsyncMock(return_value=instance)
        instance.__aexit__ = AsyncMock(return_value=False)
        return worker_cls

    @pytest.mark.asyncio
    async def test_start_creates_staging_sweep_task(self):
        manager = TemporalWorkerManager(self._settings())

        with (
            patch("src.temporal.shared_services.initialize", new=MagicMock()),
            patch("src.temporal.shared_services.shutdown", new=MagicMock()),
            patch("src.temporal.worker._cleanup_stale_staging", new=AsyncMock()),
            patch("src.temporal.worker.create_temporal_client", new=AsyncMock()),
            patch("src.temporal.worker.create_audit_temporal_client", new=AsyncMock()),
            patch("src.temporal.worker.Worker", new=self._mock_worker_class()),
        ):
            await manager.start()

            assert manager._staging_sweep_task is not None
            assert not manager._staging_sweep_task.done()

            await manager.stop()

    @pytest.mark.asyncio
    async def test_stop_cancels_staging_sweep_task(self):
        manager = TemporalWorkerManager(self._settings())

        with (
            patch("src.temporal.shared_services.initialize", new=MagicMock()),
            patch("src.temporal.shared_services.shutdown", new=MagicMock()),
            patch("src.temporal.worker._cleanup_stale_staging", new=AsyncMock()),
            patch("src.temporal.worker.create_temporal_client", new=AsyncMock()),
            patch("src.temporal.worker.create_audit_temporal_client", new=AsyncMock()),
            patch("src.temporal.worker.Worker", new=self._mock_worker_class()),
        ):
            await manager.start()
            sweep_task = manager._staging_sweep_task
            assert sweep_task is not None

            await manager.stop()

            assert sweep_task.done()
            assert manager._staging_sweep_task is None


# ---------------------------------------------------------------------------
# run_worker: the audit-worker-setup failure path must also await the
# cancellation, not just request it (#110 follow-up review item 7)
# ---------------------------------------------------------------------------


class TestRunWorkerAuditSetupFailureAwaitsSweepCancellation:
    """If audit worker construction fails, run_worker cleans up and
    re-raises. All THREE places this file cancels the sweep task must await
    the cancellation the same way -- this one previously didn't."""

    @pytest.mark.asyncio
    async def test_sweep_task_is_done_after_audit_setup_failure(self):
        settings = MagicMock()
        settings.temporal_task_queue = "document-ingestion"
        settings.temporal_audit_task_queue = "audit"
        settings.temporal_max_concurrent_activities = 10
        settings.temporal_max_concurrent_workflow_tasks = 10

        captured: dict[str, asyncio.Task] = {}
        real_create_task = asyncio.create_task

        def _capturing_create_task(coro, *a, **kw):
            task = real_create_task(coro, *a, **kw)
            captured["sweep_task"] = task
            return task

        with (
            patch("src.temporal.shared_services.initialize", new=MagicMock()),
            patch("src.temporal.shared_services.shutdown", new=MagicMock()),
            patch("src.temporal.worker._cleanup_stale_staging", new=AsyncMock()),
            patch("src.temporal.worker.create_temporal_client", new=AsyncMock()),
            # Audit client construction fails -> the except branch this test
            # targets.
            patch(
                "src.temporal.worker.create_audit_temporal_client",
                new=AsyncMock(side_effect=RuntimeError("audit connect failed")),
            ),
            patch("src.temporal.worker.Worker", new=MagicMock()),
            patch("asyncio.create_task", side_effect=_capturing_create_task),
        ):
            with pytest.raises(RuntimeError, match="audit connect failed"):
                await run_worker(settings)

        sweep_task = captured["sweep_task"]
        # Awaited (not just cancel()-requested): by the time run_worker has
        # re-raised, the task must already be fully done, not merely
        # scheduled for cancellation.
        assert sweep_task.done()
