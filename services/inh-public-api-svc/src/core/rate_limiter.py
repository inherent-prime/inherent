"""Token bucket rate limiter implementation.

This module provides an in-memory rate limiter using the token bucket algorithm.
Supports per-key rate limiting with configurable limits and windows.
"""

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Protocol

from src.utils import get_logger

logger = get_logger(__name__)


@dataclass
class RateLimitInfo:
    """Current rate limit state for a key."""

    limit: int
    remaining: int
    reset_at: float  # Unix timestamp
    window_seconds: int

    @property
    def reset_in_seconds(self) -> int:
        """Seconds until the rate limit resets."""
        return max(0, int(self.reset_at - time.time()))


@dataclass
class RateLimitResult:
    """Result of a rate limit check."""

    allowed: bool
    info: RateLimitInfo


class RateLimiterBackend(Protocol):
    """Protocol for rate limiter backends."""

    async def check_and_consume(self, key: str, limit: int, window_seconds: int) -> RateLimitResult:
        """Check rate limit and consume a token if allowed.

        Args:
            key: Unique identifier (e.g., API key ID).
            limit: Maximum requests allowed per window.
            window_seconds: Time window in seconds.

        Returns:
            RateLimitResult indicating if request is allowed.
        """
        ...

    async def get_info(self, key: str, limit: int, window_seconds: int) -> RateLimitInfo:
        """Get current rate limit info without consuming.

        Args:
            key: Unique identifier (e.g., API key ID).
            limit: Maximum requests allowed per window.
            window_seconds: Time window in seconds.

        Returns:
            Current rate limit state.
        """
        ...


@dataclass
class _BucketState:
    """Internal state for a single rate limit bucket."""

    tokens: float
    last_update: float
    window_start: float


class InMemoryBackend:
    """In-memory rate limiter backend using token bucket algorithm.

    This backend stores rate limit state in memory and is suitable for
    single-instance deployments. For distributed deployments, use Redis.
    """

    def __init__(self) -> None:
        self._buckets: dict[str, _BucketState] = {}
        self._lock = asyncio.Lock()
        self._cleanup_interval = 300  # Clean up every 5 minutes
        self._last_cleanup = time.time()

    async def check_and_consume(self, key: str, limit: int, window_seconds: int) -> RateLimitResult:
        """Check rate limit and consume a token if allowed."""
        async with self._lock:
            now = time.time()

            # Periodic cleanup of stale buckets
            if now - self._last_cleanup > self._cleanup_interval:
                await self._cleanup_stale_buckets(window_seconds)
                self._last_cleanup = now

            bucket = self._get_or_create_bucket(key, limit, now)

            # Refill tokens based on time elapsed
            self._refill_tokens(bucket, limit, window_seconds, now)

            # Check if we have tokens available
            if bucket.tokens >= 1.0:
                bucket.tokens -= 1.0
                remaining = int(bucket.tokens)
                reset_at = bucket.window_start + window_seconds

                return RateLimitResult(
                    allowed=True,
                    info=RateLimitInfo(
                        limit=limit,
                        remaining=remaining,
                        reset_at=reset_at,
                        window_seconds=window_seconds,
                    ),
                )
            else:
                # Rate limited
                reset_at = bucket.window_start + window_seconds
                return RateLimitResult(
                    allowed=False,
                    info=RateLimitInfo(
                        limit=limit,
                        remaining=0,
                        reset_at=reset_at,
                        window_seconds=window_seconds,
                    ),
                )

    async def get_info(self, key: str, limit: int, window_seconds: int) -> RateLimitInfo:
        """Get current rate limit info without consuming."""
        async with self._lock:
            now = time.time()
            bucket = self._get_or_create_bucket(key, limit, now)
            self._refill_tokens(bucket, limit, window_seconds, now)

            return RateLimitInfo(
                limit=limit,
                remaining=int(bucket.tokens),
                reset_at=bucket.window_start + window_seconds,
                window_seconds=window_seconds,
            )

    def _get_or_create_bucket(self, key: str, limit: int, now: float) -> _BucketState:
        """Get existing bucket or create new one."""
        if key not in self._buckets:
            self._buckets[key] = _BucketState(
                tokens=float(limit),
                last_update=now,
                window_start=now,
            )
        return self._buckets[key]

    def _refill_tokens(
        self,
        bucket: _BucketState,
        limit: int,
        window_seconds: int,
        now: float,
    ) -> None:
        """Refill tokens based on elapsed time (sliding window)."""
        elapsed = now - bucket.last_update

        # Calculate tokens to add based on refill rate
        refill_rate = limit / window_seconds  # tokens per second
        tokens_to_add = elapsed * refill_rate

        bucket.tokens = min(float(limit), bucket.tokens + tokens_to_add)
        bucket.last_update = now

        # Reset window if we've fully refilled
        if bucket.tokens >= limit:
            bucket.window_start = now

    async def _cleanup_stale_buckets(self, window_seconds: int) -> None:
        """Remove buckets that haven't been used recently."""
        now = time.time()
        stale_threshold = window_seconds * 2  # Keep for 2x the window

        stale_keys = [
            key
            for key, bucket in self._buckets.items()
            if now - bucket.last_update > stale_threshold
        ]

        for key in stale_keys:
            del self._buckets[key]


class RedisBackend:
    """Distributed rate limiter backend using a Redis fixed-window counter.

    Unlike InMemoryBackend, state is shared across all API instances, so the
    effective limit does not multiply by the number of autoscaled workers (#5).

    Uses an atomic ``INCR`` (+ ``EXPIRE`` on the first hit of a window) — the
    canonical distributed counter. ``INCR`` is atomic, and expiry is (re)applied
    whenever the key has none, so a crash between INCR and EXPIRE can never leave
    a key that counts forever.
    """

    def __init__(self, redis_client: Any, key_prefix: str = "ratelimit:") -> None:
        self._redis = redis_client
        self._prefix = key_prefix

    def _bucket_key(self, key: str, window_seconds: int) -> str:
        return f"{self._prefix}{key}:{window_seconds}"

    async def check_and_consume(self, key: str, limit: int, window_seconds: int) -> RateLimitResult:
        bucket = self._bucket_key(key, window_seconds)
        count = int(await self._redis.incr(bucket))

        ttl = int(await self._redis.ttl(bucket))
        if ttl < 0:
            # No expiry set yet (first hit, or a prior crash before EXPIRE).
            await self._redis.expire(bucket, window_seconds)
            ttl = window_seconds

        allowed = count <= limit
        remaining = max(0, limit - count)
        return RateLimitResult(
            allowed=allowed,
            info=RateLimitInfo(
                limit=limit,
                remaining=remaining,
                reset_at=time.time() + ttl,
                window_seconds=window_seconds,
            ),
        )

    async def get_info(self, key: str, limit: int, window_seconds: int) -> RateLimitInfo:
        bucket = self._bucket_key(key, window_seconds)
        raw = await self._redis.get(bucket)
        count = int(raw) if raw is not None else 0
        ttl = int(await self._redis.ttl(bucket))
        return RateLimitInfo(
            limit=limit,
            remaining=max(0, limit - count),
            reset_at=time.time() + (ttl if ttl > 0 else window_seconds),
            window_seconds=window_seconds,
        )


class TokenBucketRateLimiter:
    """Token bucket rate limiter with pluggable backend.

    This is the main interface for rate limiting. By default uses an
    in-memory backend, but can be configured with Redis for distributed
    rate limiting.
    """

    def __init__(self, backend: RateLimiterBackend | None = None) -> None:
        """Initialize rate limiter.

        Args:
            backend: Rate limiter backend. Defaults to InMemoryBackend.
        """
        self._backend = backend or InMemoryBackend()

    async def check_rate_limit(
        self, key: str, limit: int, window_seconds: int = 60
    ) -> RateLimitResult:
        """Check if a request is allowed and consume a token.

        Args:
            key: Unique identifier (e.g., API key ID or user ID).
            limit: Maximum requests allowed per window.
            window_seconds: Time window in seconds. Default 60.

        Returns:
            RateLimitResult with allowed status and current state.
        """
        return await self._backend.check_and_consume(key, limit, window_seconds)

    async def get_current_state(
        self, key: str, limit: int, window_seconds: int = 60
    ) -> RateLimitInfo:
        """Get current rate limit state without consuming.

        Args:
            key: Unique identifier (e.g., API key ID or user ID).
            limit: Maximum requests allowed per window.
            window_seconds: Time window in seconds. Default 60.

        Returns:
            Current rate limit state.
        """
        return await self._backend.get_info(key, limit, window_seconds)


# Global rate limiter instance
_rate_limiter: TokenBucketRateLimiter | None = None


def _build_backend() -> RateLimiterBackend:
    """Select the rate-limiter backend from configuration.

    Uses Redis (distributed, correct across autoscaled instances) when
    ``REDIS_URL`` is set; otherwise falls back to in-memory and warns loudly
    that limits are per-process and will be exceeded across multiple workers (#5).
    """
    from src.config import settings

    redis_url = getattr(settings, "redis_url", None)
    if redis_url:
        try:
            import redis.asyncio as aioredis

            client = aioredis.from_url(redis_url, encoding="utf-8", decode_responses=True)
            logger.info("Rate limiter using distributed Redis backend")
            return RedisBackend(client)
        except Exception as e:  # pragma: no cover - defensive
            logger.warning(
                "Failed to initialise Redis rate-limiter backend; falling back to "
                "in-memory (limits are per-process)",
                error=str(e),
            )
            return InMemoryBackend()

    logger.warning(
        "Rate limiter using in-memory backend — limits are PER-PROCESS and will be "
        "exceeded across multiple instances. Set REDIS_URL for distributed limiting."
    )
    return InMemoryBackend()


def get_rate_limiter() -> TokenBucketRateLimiter:
    """Get the global rate limiter instance."""
    global _rate_limiter
    if _rate_limiter is None:
        _rate_limiter = TokenBucketRateLimiter(_build_backend())
    return _rate_limiter


def set_rate_limiter(limiter: TokenBucketRateLimiter) -> None:
    """Set the global rate limiter instance (for testing)."""
    global _rate_limiter
    _rate_limiter = limiter
