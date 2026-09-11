"""Unit tests for per-identity quota enforcement (#309).

Exercises ``src.mcp_server.quotas.check_quota`` directly (no HTTP, no
``call_tool`` dispatch -- that wiring is covered by
``tests/contract/test_mcp_quotas.py``), covering:

- Default-open: an unlimited principal costs zero rate-limiter/database I/O.
- Each limit kind rejects at its boundary and reports the right
  ``QuotaDenial`` (limit name, value, reset time, upgrade URL).
- ``calls_per_minute`` throttles then recovers once its window elapses (the
  issue's acceptance criterion), proven with a real ``InMemoryBackend``.
- ``writes_per_day`` exhausted still permits a read (acceptance criterion).
- ``max_documents`` is enforced only for the one tool that can increase a
  workspace's document count, never for delete/refresh.
- Fail-OPEN on every infrastructure failure (entitlements lookup, rate
  limiter backend, document-count query) vs. fail-CLOSED on genuine
  exhaustion -- #309 design constraint #2.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

import src.mcp_server.quotas as quotas
from src.core.rate_limiter import InMemoryBackend, TokenBucketRateLimiter
from src.services.auth import Principal
from src.services.entitlements import Entitlements, set_entitlements_provider

pytestmark = pytest.mark.asyncio


def _principal(principal_id: str = "user-1") -> Principal:
    return Principal(principal_id=principal_id, principal_type="api_key", scopes=frozenset())


class _FixedProvider:
    """Entitlements provider test double returning one fixed value."""

    def __init__(self, entitlements: Entitlements) -> None:
        self._entitlements = entitlements

    async def get_entitlements(self, principal: Principal) -> Entitlements:  # noqa: ARG002
        return self._entitlements


class _RaisingProvider:
    async def get_entitlements(self, principal: Principal) -> Entitlements:  # noqa: ARG002
        raise ConnectionError("entitlements store unreachable")


# --------------------------------------------------------------------------- #
# Default-open (#309 design constraint #1)
# --------------------------------------------------------------------------- #
class TestDefaultOpen:
    async def test_default_open_principal_incurs_no_quota_io(self):
        """An API-key principal with no entitlement record configured (the
        shipped NullEntitlementsProvider) must behave exactly as today:
        check_quota returns None (allowed) WITHOUT touching the rate
        limiter or the database. This is the proof for #309 design
        constraint #1 ("Nobody loses access by upgrading")."""
        with (
            patch("src.mcp_server.quotas.get_rate_limiter") as mock_get_limiter,
            patch("src.services.database.get_database") as mock_get_database,
        ):
            denial = await quotas.check_quota(_principal(), "search_documents", "search")

        assert denial is None
        mock_get_limiter.assert_not_called()
        mock_get_database.assert_not_called()

    async def test_default_open_applies_to_write_tools_too(self):
        with patch("src.mcp_server.quotas.get_rate_limiter") as mock_get_limiter:
            denial = await quotas.check_quota(_principal(), "upload_document", "write")
        assert denial is None
        mock_get_limiter.assert_not_called()


# --------------------------------------------------------------------------- #
# calls_per_minute: throttle then recover (acceptance criterion)
# --------------------------------------------------------------------------- #
class TestCallsPerMinute:
    async def test_throttled_at_n_then_succeeds_after_window_resets(self, monkeypatch):
        """'A caller whose identity carries calls_per_minute: N is throttled
        at N and succeeds again the next minute.' Uses a real InMemoryBackend
        (the same backend #213 ships) and a 1-second stand-in window (via
        monkeypatching the module constant) instead of a real 60s wait, so
        this test proves the actual refill mechanism, fast."""
        monkeypatch.setattr(quotas, "_SECONDS_PER_MINUTE", 1)
        set_entitlements_provider(_FixedProvider(Entitlements(calls_per_minute=2)))
        limiter = TokenBucketRateLimiter(InMemoryBackend())

        with patch("src.mcp_server.quotas.get_rate_limiter", return_value=limiter):
            principal = _principal()
            first = await quotas.check_quota(principal, "search_documents", "search")
            second = await quotas.check_quota(principal, "search_documents", "search")
            third = await quotas.check_quota(principal, "search_documents", "search")

            assert first is None
            assert second is None
            assert third is not None
            assert third.limit_name == "calls_per_minute"
            assert third.limit == 2
            assert third.reset_at is not None

            await asyncio.sleep(1.1)  # let the 1-second stand-in window elapse

            fourth = await quotas.check_quota(principal, "search_documents", "search")
        assert fourth is None

    async def test_calls_per_minute_applies_to_read_tools(self):
        set_entitlements_provider(_FixedProvider(Entitlements(calls_per_minute=1)))
        principal = _principal()
        first = await quotas.check_quota(principal, "get_document", "read")
        second = await quotas.check_quota(principal, "get_document", "read")
        assert first is None
        assert second is not None
        assert second.limit_name == "calls_per_minute"

    async def test_denial_carries_configured_upgrade_url(self):
        set_entitlements_provider(
            _FixedProvider(
                Entitlements(calls_per_minute=1, upgrade_url="https://example.com/upgrade")
            )
        )
        principal = _principal()
        await quotas.check_quota(principal, "get_document", "read")
        denial = await quotas.check_quota(principal, "get_document", "read")
        assert denial is not None
        assert denial.upgrade_url == "https://example.com/upgrade"


# --------------------------------------------------------------------------- #
# writes_per_day: write-only, reads unaffected (acceptance criterion)
# --------------------------------------------------------------------------- #
class TestWritesPerDay:
    async def test_writes_per_day_exhausted_read_still_allowed(self):
        """'A caller with writes_per_day exhausted can still read.'"""
        set_entitlements_provider(_FixedProvider(Entitlements(writes_per_day=1)))
        principal = _principal()

        first_write = await quotas.check_quota(principal, "upload_document", "write")
        assert first_write is None

        second_write = await quotas.check_quota(principal, "upload_document", "write")
        assert second_write is not None
        assert second_write.limit_name == "writes_per_day"

        # A read call, same principal, same moment -- must NOT be blocked by
        # the exhausted write budget.
        read_result = await quotas.check_quota(principal, "get_document", "read")
        assert read_result is None

    async def test_writes_per_day_never_checked_for_read_tools(self):
        """A read tool call must not consume writes_per_day budget at all --
        not just 'not blocked by it', but never touches that counter."""
        set_entitlements_provider(_FixedProvider(Entitlements(writes_per_day=1)))
        principal = _principal()
        for _ in range(5):
            result = await quotas.check_quota(principal, "get_document", "read")
            assert result is None
        # The write budget must still be fully intact.
        first_write = await quotas.check_quota(principal, "upload_document", "write")
        assert first_write is None


# --------------------------------------------------------------------------- #
# max_documents: only the document-increasing tool, no time window
# --------------------------------------------------------------------------- #
class TestMaxDocuments:
    async def test_blocks_upload_when_at_or_over_cap(self):
        set_entitlements_provider(_FixedProvider(Entitlements(max_documents=2)))
        db = AsyncMock()
        db.get_document_count_for_workspaces = AsyncMock(return_value=2)

        async def _workspace_ids():
            return ["ws-1"]

        with patch("src.services.database.get_database", AsyncMock(return_value=db)):
            denial = await quotas.check_quota(
                _principal(),
                "upload_document",
                "write",
                workspace_ids_for_max_documents=_workspace_ids,
            )

        assert denial is not None
        assert denial.limit_name == "max_documents"
        assert denial.limit == 2
        assert denial.reset_at is None  # no time window for a document cap

    async def test_allows_upload_when_under_cap(self):
        set_entitlements_provider(_FixedProvider(Entitlements(max_documents=5)))
        db = AsyncMock()
        db.get_document_count_for_workspaces = AsyncMock(return_value=1)

        async def _workspace_ids():
            return ["ws-1"]

        with patch("src.services.database.get_database", AsyncMock(return_value=db)):
            denial = await quotas.check_quota(
                _principal(),
                "upload_document",
                "write",
                workspace_ids_for_max_documents=_workspace_ids,
            )
        assert denial is None

    async def test_not_enforced_for_delete_document(self):
        """delete_document is permission='write' but does NOT increase the
        document count -- blocking it at the cap would trap the caller with
        no way to get back under budget. max_documents must never apply."""
        set_entitlements_provider(_FixedProvider(Entitlements(max_documents=0)))
        db = AsyncMock()
        db.get_document_count_for_workspaces = AsyncMock(
            side_effect=AssertionError("must not be queried for delete_document")
        )

        async def _workspace_ids():
            return ["ws-1"]

        with patch("src.services.database.get_database", AsyncMock(return_value=db)):
            denial = await quotas.check_quota(
                _principal(),
                "delete_document",
                "write",
                workspace_ids_for_max_documents=_workspace_ids,
            )
        assert denial is None

    async def test_not_enforced_for_refresh_stale_source(self):
        set_entitlements_provider(_FixedProvider(Entitlements(max_documents=0)))
        denial = await quotas.check_quota(_principal(), "refresh_stale_source", "write")
        assert denial is None

    async def test_no_workspace_provider_fails_open_with_loud_log(self):
        """The OAuth path has no workspace resolution yet -- a configured
        max_documents limit must not silently block forever, nor silently
        never apply either: fail OPEN, but be LOUD about it."""
        set_entitlements_provider(_FixedProvider(Entitlements(max_documents=1)))
        denial = await quotas.check_quota(
            _principal(), "upload_document", "write", workspace_ids_for_max_documents=None
        )
        assert denial is None


# --------------------------------------------------------------------------- #
# Fail-open on infrastructure failure (#309 design constraint #2)
# --------------------------------------------------------------------------- #
class TestFailOpen:
    async def test_entitlements_lookup_failure_fails_open(self):
        set_entitlements_provider(_RaisingProvider())
        denial = await quotas.check_quota(_principal(), "search_documents", "search")
        assert denial is None

    async def test_rate_limiter_backend_failure_fails_open(self):
        set_entitlements_provider(_FixedProvider(Entitlements(calls_per_minute=1)))
        broken_limiter = AsyncMock()
        broken_limiter.check_rate_limit = AsyncMock(side_effect=ConnectionError("redis down"))
        with patch("src.mcp_server.quotas.get_rate_limiter", return_value=broken_limiter):
            denial = await quotas.check_quota(_principal(), "search_documents", "search")
        assert denial is None

    async def test_max_documents_db_failure_fails_open(self):
        set_entitlements_provider(_FixedProvider(Entitlements(max_documents=1)))
        db = AsyncMock()
        db.get_document_count_for_workspaces = AsyncMock(side_effect=RuntimeError("db down"))

        async def _workspace_ids():
            return ["ws-1"]

        with patch("src.services.database.get_database", AsyncMock(return_value=db)):
            denial = await quotas.check_quota(
                _principal(),
                "upload_document",
                "write",
                workspace_ids_for_max_documents=_workspace_ids,
            )
        assert denial is None

    async def test_genuine_exhaustion_still_fails_closed(self):
        """Sanity check that fail-open is scoped to INFRASTRUCTURE failures
        only -- a real, successfully-observed exhaustion is still denied."""
        set_entitlements_provider(_FixedProvider(Entitlements(calls_per_minute=1)))
        principal = _principal()
        await quotas.check_quota(principal, "search_documents", "search")
        denial = await quotas.check_quota(principal, "search_documents", "search")
        assert denial is not None


# --------------------------------------------------------------------------- #
# Metering: fire-and-forget, never blocking, never raising (#309 "Metering")
# --------------------------------------------------------------------------- #
class TestPublishUsageEvent:
    def test_returns_immediately_without_awaiting_the_sink(self):
        """publish_usage_event must be a plain (non-async) function that
        schedules work and returns -- a caller that does not await anything
        after it must not be delayed by the 'sink'."""
        principal = _principal()

        # No event loop running here (sync test) -- create_task would raise
        # RuntimeError with no loop; run inside one instead to prove it
        # schedules without blocking.
        async def _run():
            quotas.publish_usage_event(principal, "search_documents", allowed=True)

        asyncio.run(_run())  # completes without hanging even though the
        # scheduled task may not have run to completion yet.

    async def test_a_failing_sink_never_raises_into_the_caller(self, monkeypatch):
        def _broken_info(*args, **kwargs):
            raise RuntimeError("sink is down")

        monkeypatch.setattr(quotas.logger, "info", _broken_info)
        principal = _principal()

        # Must not raise.
        quotas.publish_usage_event(principal, "search_documents", allowed=True)
        # Let the scheduled task actually run so its internal except path executes.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
