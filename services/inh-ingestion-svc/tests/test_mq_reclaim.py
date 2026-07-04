"""Runtime redelivery of stuck MQ messages via XAUTOCLAIM (#17).

The poll loop only reads NEW (">") entries after startup, so a message whose
handler failed transiently (e.g. Temporal briefly down) stayed pending until a
pod restart. A periodic reclaim pass re-dispatches idle-pending messages while
the service runs, and drops a message that keeps failing past a delivery cap.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.services.mq.redis_mq import RedisMQService


@pytest.fixture
def service():
    settings = MagicMock()
    settings.max_workers = 4
    settings.resolved_mq_max_concurrent = 4
    return RedisMQService(settings)


@pytest.mark.asyncio
async def test_reclaims_and_redispatches_stuck_message(service):
    redis = AsyncMock()
    service._redis = redis
    redis.xautoclaim.return_value = (
        "0-0",
        [("1-0", {"payload": json.dumps({"k": "v"})})],
        [],
    )
    redis.xpending_range.return_value = [{"times_delivered": 2}]
    handler = AsyncMock()

    await service._reclaim_pending("stream", "group", "consumer", handler)

    # Message was re-dispatched to the handler and ACKed on success.
    handler.assert_awaited_once_with({"k": "v"})
    redis.xack.assert_awaited_once_with("stream", "group", "1-0")


@pytest.mark.asyncio
async def test_drops_poison_message_over_delivery_cap(service):
    redis = AsyncMock()
    service._redis = redis
    service._max_deliveries = 3
    redis.xautoclaim.return_value = (
        "0-0",
        [("1-0", {"payload": json.dumps({"k": "v"})})],
        [],
    )
    redis.xpending_range.return_value = [{"times_delivered": 4}]  # over the cap
    handler = AsyncMock()

    await service._reclaim_pending("stream", "group", "consumer", handler)

    # Poison message is NOT re-dispatched; it's ACKed (dropped) so it can't loop.
    handler.assert_not_awaited()
    redis.xack.assert_awaited_once_with("stream", "group", "1-0")


@pytest.mark.asyncio
async def test_reclaim_uses_min_idle_threshold(service):
    redis = AsyncMock()
    service._redis = redis
    redis.xautoclaim.return_value = ("0-0", [], [])
    handler = AsyncMock()

    await service._reclaim_pending("stream", "group", "consumer", handler)

    # Only messages idle >= the configured threshold are eligible.
    assert redis.xautoclaim.await_args.kwargs["min_idle_time"] == service._reclaim_min_idle_ms
