"""Rate-limiting middleware: unauthenticated traffic is bounded per IP (#5).

Previously the middleware skipped rate limiting entirely whenever
``request.state.api_key_info`` was None — i.e. for every missing/invalid key,
and for *all* traffic during a transient auth-DB outage. Now such requests fall
back to a per-client-IP bucket with a conservative limit.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.responses import Response

from src.core.rate_limiter import RateLimitInfo, RateLimitResult
from src.middleware.rate_limiting import RateLimitingMiddleware

pytestmark = pytest.mark.asyncio


def _request(path: str = "/v1/search", host: str = "1.2.3.4") -> SimpleNamespace:
    # state has NO api_key_info attribute -> getattr(...) returns None (unauth).
    return SimpleNamespace(
        url=SimpleNamespace(path=path),
        client=SimpleNamespace(host=host),
        state=SimpleNamespace(),
    )


def _limiter(allowed: bool):
    info = RateLimitInfo(
        limit=30, remaining=0 if not allowed else 29, reset_at=0, window_seconds=60
    )
    limiter = MagicMock()
    limiter.check_rate_limit = AsyncMock(return_value=RateLimitResult(allowed=allowed, info=info))
    return limiter


async def test_unauthenticated_request_is_limited_by_ip():
    mw = RateLimitingMiddleware(app=MagicMock())
    limiter = _limiter(allowed=True)
    call_next = AsyncMock(return_value=Response())

    with (
        patch("src.middleware.rate_limiting.get_rate_limiter", return_value=limiter),
        patch("src.middleware.rate_limiting.get_request_context", return_value=None),
        patch("src.middleware.rate_limiting.settings") as s,
    ):
        s.rate_limit_enabled = True
        s.rate_limit_window_seconds = 60
        s.rate_limit_unauthenticated = 30
        await mw.dispatch(_request(host="9.9.9.9"), call_next)

    # It must NOT skip: the limiter is consulted with an IP bucket + unauth limit.
    limiter.check_rate_limit.assert_awaited_once()
    kwargs = limiter.check_rate_limit.await_args.kwargs
    assert kwargs["key"] == "ip:9.9.9.9"
    assert kwargs["limit"] == 30
    call_next.assert_awaited_once()


async def test_unauthenticated_request_gets_429_when_ip_limit_exceeded():
    mw = RateLimitingMiddleware(app=MagicMock())
    limiter = _limiter(allowed=False)
    call_next = AsyncMock(return_value=Response())

    with (
        patch("src.middleware.rate_limiting.get_rate_limiter", return_value=limiter),
        patch("src.middleware.rate_limiting.get_request_context", return_value=None),
        patch("src.middleware.rate_limiting.settings") as s,
    ):
        s.rate_limit_enabled = True
        s.rate_limit_window_seconds = 60
        s.rate_limit_unauthenticated = 30
        resp = await mw.dispatch(_request(), call_next)

    assert resp.status_code == 429
    # Blocked before reaching the handler.
    call_next.assert_not_awaited()
