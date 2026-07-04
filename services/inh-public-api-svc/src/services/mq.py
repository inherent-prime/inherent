"""Lightweight Redis Streams publisher for document upload notifications.

Publishes messages via XADD so that ingestion-svc (which subscribes via
XREADGROUP) picks them up reliably. The payload format matches
``DocumentUploadMessage`` in ingestion-svc.
"""

from __future__ import annotations

import json

import redis.asyncio as aioredis

from src.config import settings
from src.utils import get_logger

logger = get_logger(__name__)


class MQService:
    """Redis Streams publisher."""

    def __init__(self, redis_url: str) -> None:
        self._redis: aioredis.Redis = aioredis.from_url(redis_url, decode_responses=True)
        self._connected = False

    async def connect(self) -> None:
        """Verify Redis connectivity."""
        if self._connected:
            return
        await self._redis.ping()  # type: ignore[misc]  # redis-py stubs union return
        self._connected = True
        safe_url = (
            settings.mq_redis_url.split("@")[-1]
            if "@" in settings.mq_redis_url
            else settings.mq_redis_url
        )
        logger.info("MQService connected", url=safe_url)

    async def publish(self, topic: str, message: dict) -> str:
        """Publish *message* to a Redis Stream via XADD. Returns the stream message ID."""
        if not self._connected:
            await self.connect()

        payload = json.dumps(message)
        message_id: str = await self._redis.xadd(topic, {"payload": payload})
        logger.info(
            "MQ message published",
            topic=topic,
            message_id=message_id,
        )
        return message_id

    async def close(self) -> None:
        """Close the underlying Redis connection."""
        if self._redis:
            await self._redis.close()
            self._redis = None  # type: ignore[assignment]
        self._connected = False
        logger.info("MQService closed")


# ---------------------------------------------------------------------------
# Singleton management
# ---------------------------------------------------------------------------

_mq_service: MQService | None = None


async def get_mq_service() -> MQService:
    """Return (and lazily create + connect) the singleton MQService."""
    global _mq_service
    if _mq_service is None:
        _mq_service = MQService(settings.mq_redis_url)
        await _mq_service.connect()
    return _mq_service


async def close_mq_service() -> None:
    """Tear down the MQService singleton (idempotent)."""
    global _mq_service
    if _mq_service is not None:
        await _mq_service.close()
        _mq_service = None
