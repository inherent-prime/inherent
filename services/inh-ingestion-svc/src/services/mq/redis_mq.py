"""Valkey / Redis Streams message queue backend.

Uses XADD/XREADGROUP for reliable, persistent pub/sub with consumer groups.
Works with Valkey, Redis, or any Redis-protocol-compatible server.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
from typing import TYPE_CHECKING

import structlog

from .base import BaseMQService, MessageHandler

if TYPE_CHECKING:
    from redis.asyncio import Redis

    from src.config.settings import Settings

logger = structlog.get_logger(__name__)


class RedisMQService(BaseMQService):
    """Redis Streams (Valkey-compatible) message queue service."""

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self._redis: Redis | None = None
        self._connected = False
        self._poll_tasks: dict[str, asyncio.Task] = {}
        self._running = False
        # Backpressure (#18): bound how many handler invocations (each of which
        # starts a Temporal workflow) run concurrently across a polled batch.
        # Sized from settings; falls back to max_workers. Created lazily so a
        # plain MagicMock(spec=Settings) in tests still works.
        self._concurrency_limit = self._resolve_concurrency_limit(settings)
        self._semaphore = asyncio.Semaphore(self._concurrency_limit)
        # Runtime redelivery (#17): reclaim messages that were delivered but not
        # ACKed and have sat idle for this long, so a transient handler failure
        # is retried while the service runs — not only on the next restart.
        self._reclaim_min_idle_ms = 30_000
        # Drop as poison after this many delivery attempts (feeds the warn/drop
        # path so one bad message can't loop forever).
        self._max_deliveries = 5

    @staticmethod
    def _resolve_concurrency_limit(settings: Settings) -> int:
        """Resolve the consume-loop concurrency bound from settings.

        Prefers Settings.resolved_mq_max_concurrent; tolerates partial mocks
        (falling back to max_workers, then to a safe default of 4).
        """
        resolver = getattr(settings, "resolved_mq_max_concurrent", None)
        try:
            value = int(resolver) if resolver is not None else int(settings.max_workers)
        except (TypeError, ValueError):
            value = 4
        return max(1, value)

    @property
    def backend(self) -> str:
        return "redis"

    async def connect(self) -> None:
        if self._connected:
            return

        from redis.asyncio import from_url

        url = self.settings.redis_url
        self._redis = from_url(url, decode_responses=True)

        # Verify connectivity
        await self._redis.ping()  # type: ignore[misc]
        self._connected = True
        self._running = True

        safe_url = url.split("@")[-1] if "@" in url else url
        logger.info("Redis MQ: connected", url=safe_url)

    async def disconnect(self) -> None:
        self._running = False

        # Cancel all polling tasks
        for topic, task in self._poll_tasks.items():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

        self._poll_tasks.clear()

        if self._redis:
            await self._redis.aclose()
            self._redis = None

        self._connected = False
        logger.info("Redis MQ: disconnected")

    async def publish(self, topic: str, message: dict) -> None:
        if not self._redis or not self._connected:
            raise RuntimeError("Redis MQ: not connected")

        payload = json.dumps(message)
        message_id = await self._redis.xadd(topic, {"payload": payload})

        logger.debug("Redis MQ: published", topic=topic, message_id=message_id)

    async def subscribe(
        self,
        topic: str,
        handler: MessageHandler,
        *,
        group_id: str = "default",
    ) -> None:
        if not self._redis or not self._connected:
            raise RuntimeError("Redis MQ: not connected")

        consumer_id = f"{group_id}-{socket.gethostname()}-{os.getpid()}"

        await self._ensure_consumer_group(topic, group_id)

        task = asyncio.create_task(self._poll_loop(topic, group_id, consumer_id, handler))
        self._poll_tasks[topic] = task

        logger.info(
            "Redis MQ: subscribed",
            topic=topic,
            group_id=group_id,
            consumer_id=consumer_id,
        )

    async def unsubscribe(self, topic: str) -> None:
        task = self._poll_tasks.pop(topic, None)
        if task:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        logger.info("Redis MQ: unsubscribed", topic=topic)

    def is_connected(self) -> bool:
        return self._connected

    # --------------------------------------------------------------------------
    # Internal
    # --------------------------------------------------------------------------

    async def _ensure_consumer_group(self, stream: str, group: str) -> None:
        """Create consumer group if it doesn't exist (idempotent)."""
        assert self._redis is not None

        try:
            await self._redis.xgroup_create(stream, group, id="0", mkstream=True)
            logger.info("Redis MQ: consumer group created", stream=stream, group=group)
        except Exception as e:
            if "BUSYGROUP" in str(e):
                logger.debug("Redis MQ: consumer group already exists", stream=stream, group=group)
            else:
                raise

    async def _poll_loop(
        self,
        stream: str,
        group: str,
        consumer: str,
        handler: MessageHandler,
    ) -> None:
        """Main polling loop: recover pending, then consume new messages."""
        assert self._redis is not None

        # Phase 1: Recover any pending messages from a previous crash
        await self._process_pending(stream, group, consumer, handler)

        # Phase 2: Poll for new messages
        while self._running:
            try:
                results = await self._redis.xreadgroup(
                    groupname=group,
                    consumername=consumer,
                    streams={stream: ">"},
                    block=5000,
                    count=10,
                )

                if not results:
                    continue

                # Process the batch concurrently, bounded by the semaphore
                # (#18 backpressure). Each message is still ACKed only after
                # its own handler succeeds (see _handle_message).
                tasks = [
                    self._handle_message_bounded(stream, group, message_id, fields, handler)
                    for _stream_name, messages in results
                    for message_id, fields in messages
                ]
                if tasks:
                    await asyncio.gather(*tasks)

                # Runtime redelivery (#17): reclaim any messages that failed and
                # have sat idle past the threshold, so a transient handler error
                # is retried while running, not only on the next restart.
                await self._reclaim_pending(stream, group, consumer, handler)

                # Best-effort: publish current pending/lag for this stream+group.
                await self._update_pending_gauge(stream, group)

            except asyncio.CancelledError:
                break
            except Exception as e:
                if not self._running:
                    break
                logger.error(
                    "Redis MQ: poll error, retrying in 1s",
                    stream=stream,
                    error=str(e),
                )
                await asyncio.sleep(1)

    async def _reclaim_pending(
        self,
        stream: str,
        group: str,
        consumer: str,
        handler: MessageHandler,
    ) -> None:
        """Reclaim and re-dispatch messages that failed and have sat idle (#17).

        The live poll loop only reads new (">") entries, so without this a
        transient handler failure (e.g. Temporal briefly down) would leave the
        message pending until the next restart. XAUTOCLAIM transfers messages
        idle >= ``_reclaim_min_idle_ms`` to this consumer; we re-dispatch each,
        and drop (ACK) any that have exceeded ``_max_deliveries`` as poison.
        """
        assert self._redis is not None
        try:
            _cursor, claimed, _deleted = await self._redis.xautoclaim(
                name=stream,
                groupname=group,
                consumername=consumer,
                min_idle_time=self._reclaim_min_idle_ms,
                start_id="0-0",
                count=20,
            )
        except Exception as e:
            logger.warning("Redis MQ: xautoclaim failed", stream=stream, error=str(e))
            return

        for message_id, fields in claimed:
            if not fields:
                continue  # tombstone / already ACKed
            delivered = await self._delivery_count(stream, group, message_id)
            if delivered > self._max_deliveries:
                # Poison: keeps failing. Drop it so it can't loop forever.
                await self._redis.xack(stream, group, message_id)
                logger.error(
                    "Redis MQ: message exceeded max deliveries, dropping",
                    stream=stream,
                    message_id=message_id,
                    deliveries=delivered,
                )
                continue
            await self._handle_message(stream, group, message_id, fields, handler)

    async def _delivery_count(self, stream: str, group: str, message_id: str) -> int:
        """Return how many times ``message_id`` has been delivered (>=1)."""
        try:
            pending = await self._redis.xpending_range(
                stream, group, min=message_id, max=message_id, count=1
            )
            if pending:
                return int(pending[0]["times_delivered"])
        except Exception:
            pass
        return 1

    async def _process_pending(
        self,
        stream: str,
        group: str,
        consumer: str,
        handler: MessageHandler,
    ) -> None:
        """Recover messages that were delivered but not ACKed (e.g. after crash)."""
        assert self._redis is not None

        try:
            results = await self._redis.xreadgroup(
                groupname=group,
                consumername=consumer,
                streams={stream: "0"},
                count=100,
            )

            if not results:
                return

            recovered = 0
            for _stream_name, messages in results:
                for message_id, fields in messages:
                    if not fields:
                        continue  # Already ACKed (empty fields = tombstone)
                    await self._handle_message(stream, group, message_id, fields, handler)
                    recovered += 1

            if recovered > 0:
                logger.info(
                    "Redis MQ: recovered pending messages",
                    stream=stream,
                    group=group,
                    count=recovered,
                )
        except Exception as e:
            logger.warning(
                "Redis MQ: failed to recover pending messages",
                stream=stream,
                error=str(e),
            )

    async def _handle_message(
        self,
        stream: str,
        group: str,
        message_id: str,
        fields: dict,
        handler: MessageHandler,
    ) -> None:
        """Parse a stream entry, call the handler, ACK on success."""
        assert self._redis is not None

        payload = json.loads(fields.get("payload", "{}"))

        try:
            await handler(payload)
            await self._redis.xack(stream, group, message_id)
        except Exception as e:
            # Don't ACK — message stays pending and will be redelivered on restart
            logger.error(
                "Redis MQ: handler failed, message will be retried",
                stream=stream,
                message_id=message_id,
                error=str(e),
            )

    async def _handle_message_bounded(
        self,
        stream: str,
        group: str,
        message_id: str,
        fields: dict,
        handler: MessageHandler,
    ) -> None:
        """Run _handle_message under the concurrency semaphore (#18 backpressure).

        Bounds how many handler invocations (each starting a Temporal workflow)
        run concurrently across a polled batch; each message is still ACKed only
        after its own handler succeeds.
        """
        async with self._semaphore:
            await self._handle_message(stream, group, message_id, fields, handler)

    async def _update_pending_gauge(self, stream: str, group: str) -> None:
        """Best-effort: publish the stream/group pending (lag) count as a gauge.

        Uses the XPENDING summary form. Never raises — observability must not
        disrupt the consume loop (and this tolerates mocked redis clients).
        """
        if self._redis is None:
            return
        try:
            summary = await self._redis.xpending(stream, group)
            # redis-py returns a dict for the summary form ({"pending": N, ...});
            # some clients return a sequence whose first element is the count.
            if isinstance(summary, dict):
                pending = summary.get("pending", 0)
            else:
                pending = summary[0]
            from src.services.metrics import MQ_STREAM_PENDING

            MQ_STREAM_PENDING.labels(stream=stream, group=group).set(float(pending or 0))
        except Exception as exc:  # best-effort metric; never disrupt consumption
            logger.debug(
                "Redis MQ: could not update pending gauge",
                stream=stream,
                group=group,
                error=str(exc),
            )
