"""Unit tests for the token bucket rate limiter."""

import asyncio
import time
from unittest.mock import patch

import pytest

from src.core.rate_limiter import (
    InMemoryBackend,
    RateLimitInfo,
    RedisBackend,
    TokenBucketRateLimiter,
)


class TestRateLimitInfo:
    """Tests for RateLimitInfo dataclass."""

    def test_reset_in_seconds_positive(self):
        """Test reset_in_seconds when reset is in the future."""
        info = RateLimitInfo(
            limit=100,
            remaining=50,
            reset_at=time.time() + 30,
            window_seconds=60,
        )
        assert 28 <= info.reset_in_seconds <= 30

    def test_reset_in_seconds_zero(self):
        """Test reset_in_seconds when reset is in the past."""
        info = RateLimitInfo(
            limit=100,
            remaining=50,
            reset_at=time.time() - 10,
            window_seconds=60,
        )
        assert info.reset_in_seconds == 0


class TestInMemoryBackend:
    """Tests for InMemoryBackend."""

    @pytest.fixture
    def backend(self) -> InMemoryBackend:
        return InMemoryBackend()

    @pytest.mark.asyncio
    async def test_first_request_allowed(self, backend: InMemoryBackend):
        """First request should always be allowed."""
        result = await backend.check_and_consume("key1", limit=10, window_seconds=60)
        assert result.allowed is True
        assert result.info.remaining == 9
        assert result.info.limit == 10

    @pytest.mark.asyncio
    async def test_multiple_requests_consume_tokens(self, backend: InMemoryBackend):
        """Multiple requests should consume tokens."""
        for i in range(5):
            result = await backend.check_and_consume("key1", limit=10, window_seconds=60)
            assert result.allowed is True
            assert result.info.remaining == 10 - i - 1

    @pytest.mark.asyncio
    async def test_rate_limit_exceeded(self, backend: InMemoryBackend):
        """Requests should be denied when limit is exceeded."""
        # Consume all tokens
        for _ in range(10):
            await backend.check_and_consume("key1", limit=10, window_seconds=60)

        # Next request should be denied
        result = await backend.check_and_consume("key1", limit=10, window_seconds=60)
        assert result.allowed is False
        assert result.info.remaining == 0

    @pytest.mark.asyncio
    async def test_different_keys_independent(self, backend: InMemoryBackend):
        """Different keys should have independent limits."""
        # Exhaust key1
        for _ in range(5):
            await backend.check_and_consume("key1", limit=5, window_seconds=60)

        # key1 should be rate limited
        result1 = await backend.check_and_consume("key1", limit=5, window_seconds=60)
        assert result1.allowed is False

        # key2 should still be allowed
        result2 = await backend.check_and_consume("key2", limit=5, window_seconds=60)
        assert result2.allowed is True

    @pytest.mark.asyncio
    async def test_tokens_refill_over_time(self, backend: InMemoryBackend):
        """Tokens should refill based on elapsed time."""
        # Consume all tokens
        for _ in range(10):
            await backend.check_and_consume("key1", limit=10, window_seconds=1)

        # Should be rate limited
        result = await backend.check_and_consume("key1", limit=10, window_seconds=1)
        assert result.allowed is False

        # Wait for refill
        await asyncio.sleep(0.2)

        # Should have some tokens now
        result = await backend.check_and_consume("key1", limit=10, window_seconds=1)
        assert result.allowed is True

    @pytest.mark.asyncio
    async def test_get_info_does_not_consume(self, backend: InMemoryBackend):
        """get_info should not consume tokens."""
        # Check info twice
        info1 = await backend.get_info("key1", limit=10, window_seconds=60)
        info2 = await backend.get_info("key1", limit=10, window_seconds=60)

        assert info1.remaining == info2.remaining


class TestTokenBucketRateLimiter:
    """Tests for TokenBucketRateLimiter."""

    @pytest.fixture
    def limiter(self) -> TokenBucketRateLimiter:
        return TokenBucketRateLimiter()

    @pytest.mark.asyncio
    async def test_check_rate_limit_allowed(self, limiter: TokenBucketRateLimiter):
        """Rate limit check should return allowed=True when tokens available."""
        result = await limiter.check_rate_limit("key1", limit=100, window_seconds=60)
        assert result.allowed is True
        assert result.info.remaining == 99

    @pytest.mark.asyncio
    async def test_check_rate_limit_denied(self, limiter: TokenBucketRateLimiter):
        """Rate limit check should return allowed=False when tokens exhausted."""
        # Exhaust tokens
        for _ in range(5):
            await limiter.check_rate_limit("key1", limit=5, window_seconds=60)

        result = await limiter.check_rate_limit("key1", limit=5, window_seconds=60)
        assert result.allowed is False

    @pytest.mark.asyncio
    async def test_get_current_state(self, limiter: TokenBucketRateLimiter):
        """Should return current state without consuming."""
        await limiter.check_rate_limit("key1", limit=10, window_seconds=60)

        state = await limiter.get_current_state("key1", limit=10, window_seconds=60)
        assert state.remaining == 9

        # Check again - should be the same
        state2 = await limiter.get_current_state("key1", limit=10, window_seconds=60)
        assert state2.remaining == 9


class _FakeAsyncRedis:
    """Minimal async Redis fake supporting the fixed-window counter ops."""

    def __init__(self) -> None:
        self.counters: dict[str, int] = {}
        self.ttls: dict[str, int] = {}

    async def incr(self, key: str) -> int:
        self.counters[key] = self.counters.get(key, 0) + 1
        return self.counters[key]

    async def ttl(self, key: str) -> int:
        return self.ttls.get(key, -1)

    async def expire(self, key: str, seconds: int) -> bool:
        self.ttls[key] = seconds
        return True

    async def get(self, key: str):
        val = self.counters.get(key)
        return None if val is None else str(val)


class TestRedisBackend:
    """RedisBackend enforces a shared fixed-window counter (#5)."""

    @pytest.mark.asyncio
    async def test_allows_up_to_limit_then_blocks(self):
        backend = RedisBackend(_FakeAsyncRedis())
        results = [
            await backend.check_and_consume("k", limit=3, window_seconds=60) for _ in range(4)
        ]
        assert [r.allowed for r in results] == [True, True, True, False]
        assert results[-1].info.remaining == 0

    @pytest.mark.asyncio
    async def test_sets_expiry_on_first_hit(self):
        fake = _FakeAsyncRedis()
        backend = RedisBackend(fake)
        await backend.check_and_consume("k", limit=5, window_seconds=42)
        # Exactly one key, with the window's expiry applied so it can't count forever.
        assert list(fake.ttls.values()) == [42]

    @pytest.mark.asyncio
    async def test_distinct_keys_are_independent(self):
        backend = RedisBackend(_FakeAsyncRedis())
        a = await backend.check_and_consume("ip:1.1.1.1", limit=1, window_seconds=60)
        b = await backend.check_and_consume("ip:2.2.2.2", limit=1, window_seconds=60)
        assert a.allowed and b.allowed


class TestBackendSelection:
    """get_rate_limiter selects Redis when REDIS_URL is set, else in-memory (#5)."""

    def setup_method(self):
        import src.core.rate_limiter as rl

        rl._rate_limiter = None

    def teardown_method(self):
        import src.core.rate_limiter as rl

        rl._rate_limiter = None

    def test_uses_redis_backend_when_url_set(self):
        import src.core.rate_limiter as rl
        from src.config import settings as settings_obj

        with (
            patch.object(settings_obj, "redis_url", "redis://localhost:6379"),
            patch("redis.asyncio.from_url", return_value=object()),
        ):
            limiter = rl.get_rate_limiter()
        assert isinstance(limiter._backend, RedisBackend)

    def test_uses_inmemory_backend_when_no_url(self):
        import src.core.rate_limiter as rl
        from src.config import settings as settings_obj

        with patch.object(settings_obj, "redis_url", None):
            limiter = rl.get_rate_limiter()
        assert isinstance(limiter._backend, InMemoryBackend)
