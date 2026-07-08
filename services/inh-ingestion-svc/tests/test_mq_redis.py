"""Unit tests for RedisMQService."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.config.settings import Settings
from src.services.mq.redis_mq import RedisMQService


@pytest.fixture(autouse=True)
def cleanup_test_data():
    """No-op override of the package-level DB-dependent autouse fixture.

    These are pure unit tests over a mocked Redis client — no PostgreSQL is
    needed, so they must not skip when the DB is unavailable.
    """
    yield


@pytest.fixture
def mock_settings():
    """Create mock settings for RedisMQService."""
    settings = MagicMock(spec=Settings)
    settings.redis_url = "redis://localhost:6379"
    settings.mq_completion_topic = "core.document.processed.v1"
    return settings


@pytest.fixture
def service(mock_settings):
    """Create a RedisMQService instance."""
    return RedisMQService(mock_settings)


@pytest.fixture
def mock_redis_client():
    """Create a mock Redis async client."""
    client = MagicMock()
    client.ping = AsyncMock(return_value=True)
    client.xadd = AsyncMock(return_value="1234567890-0")
    client.xgroup_create = AsyncMock(return_value=True)
    client.xreadgroup = AsyncMock(return_value=[])
    client.xack = AsyncMock(return_value=1)
    client.aclose = AsyncMock(return_value=None)
    client.close = AsyncMock(return_value=None)
    return client


class TestRedisMQServiceBackend:
    """Tests for RedisMQService backend property."""

    def test_backend_returns_redis(self, service):
        """Test that backend property returns 'redis'."""
        assert service.backend == "redis"


class TestRedisMQServiceConnectionState:
    """Tests for RedisMQService connection state."""

    def test_not_connected_initially(self, service):
        """Test that service is not connected before connect() is called."""
        assert service.is_connected() is False
        assert service._connected is False
        assert service._redis is None

    @pytest.mark.asyncio
    async def test_connect_sets_connected_flag(self, service, mock_redis_client):
        """Test that connect() sets _connected to True after successful ping."""
        with patch("redis.asyncio.from_url", return_value=mock_redis_client):
            await service.connect()

            assert service.is_connected() is True
            assert service._connected is True
            assert service._running is True
            mock_redis_client.ping.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_disconnect_clears_state(self, service, mock_redis_client):
        """Test that disconnect() clears connection state."""
        with patch("redis.asyncio.from_url", return_value=mock_redis_client):
            await service.connect()
            assert service.is_connected() is True

        await service.disconnect()

        assert service.is_connected() is False
        assert service._connected is False
        assert service._redis is None
        assert service._running is False
        mock_redis_client.aclose.assert_awaited_once()


class TestRedisMQServicePublish:
    """Tests for RedisMQService publish functionality."""

    @pytest.mark.asyncio
    async def test_publish_calls_xadd(self, service, mock_redis_client):
        """Test that publish() calls xadd on the Redis stream."""
        with patch("redis.asyncio.from_url", return_value=mock_redis_client):
            await service.connect()

        topic = "core.document.uploaded.v1"
        message = {"event_type": "document.uploaded", "document_id": "doc123"}

        await service.publish(topic, message)

        mock_redis_client.xadd.assert_awaited_once()
        call_args = mock_redis_client.xadd.call_args
        # First positional arg is the topic/stream name
        assert call_args[0][0] == topic
        # Second positional arg is the fields dict with "payload" key
        assert "payload" in call_args[0][1]

    @pytest.mark.asyncio
    async def test_publish_raises_when_not_connected(self, service):
        """Test that publish() raises RuntimeError when not connected."""
        with pytest.raises(RuntimeError, match="Redis MQ: not connected"):
            await service.publish("test-topic", {"key": "value"})


class TestRedisMQServiceIdlePoll:
    """Idle blocking reads must be silent (#90).

    redis-py >= 8 raises redis.exceptions.TimeoutError when a blocking
    XREADGROUP expires with no messages, where older versions returned [].
    Both must be treated as the normal empty-poll case: no error log, no
    1s penalty sleep — those are reserved for genuine failures.
    """

    def _install_xreadgroup(self, service, mock_redis_client, exc_factory, stop_after):
        """Make xreadgroup return [] for the pending-recovery read (start id '0')
        and raise exc_factory() for live ('>') reads, stopping the loop after
        ``stop_after`` live polls so the test terminates.
        """
        live_polls = {"count": 0}

        async def xreadgroup(*, groupname, consumername, streams, block=None, count=None):
            if ">" not in streams.values():
                return []  # Phase 1 pending-recovery read
            live_polls["count"] += 1
            if live_polls["count"] >= stop_after:
                service._running = False
            raise exc_factory()

        mock_redis_client.xreadgroup = xreadgroup
        return live_polls

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "exc_factory",
        [
            pytest.param(
                lambda: __import__("redis").exceptions.TimeoutError(
                    "Timeout reading from valkey:6379"
                ),
                id="redis-timeout",
            ),
            pytest.param(lambda: TimeoutError("timed out"), id="builtin-asyncio-timeout"),
        ],
    )
    async def test_idle_block_expiry_is_silent(
        self, service, mock_redis_client, monkeypatch, exc_factory
    ):
        """An idle blocking read expiring continues the loop: no error log, no sleep."""
        from src.services.mq import redis_mq as redis_mq_module

        service._redis = mock_redis_client
        service._running = True
        live_polls = self._install_xreadgroup(service, mock_redis_client, exc_factory, stop_after=3)

        mock_logger = MagicMock()
        monkeypatch.setattr(redis_mq_module, "logger", mock_logger)
        sleep_calls: list[float] = []
        real_sleep = asyncio.sleep

        async def recording_sleep(delay, *args, **kwargs):
            sleep_calls.append(delay)
            await real_sleep(0)

        monkeypatch.setattr(redis_mq_module.asyncio, "sleep", recording_sleep)

        await service._poll_loop("core.document.uploaded.v1", "g", "c", AsyncMock())

        # The loop kept polling (no crash) and treated every expiry as idle.
        assert live_polls["count"] == 3
        mock_logger.error.assert_not_called()
        assert sleep_calls == []

    @pytest.mark.asyncio
    async def test_unexpected_error_still_logs_and_backs_off(
        self, service, mock_redis_client, monkeypatch
    ):
        """Genuine failures keep the existing error-log + 1s retry behavior."""
        from src.services.mq import redis_mq as redis_mq_module

        service._redis = mock_redis_client
        service._running = True
        live_polls = self._install_xreadgroup(
            service, mock_redis_client, lambda: ValueError("boom"), stop_after=2
        )

        mock_logger = MagicMock()
        monkeypatch.setattr(redis_mq_module, "logger", mock_logger)
        sleep_calls: list[float] = []
        real_sleep = asyncio.sleep

        async def recording_sleep(delay, *args, **kwargs):
            sleep_calls.append(delay)
            await real_sleep(0)

        monkeypatch.setattr(redis_mq_module.asyncio, "sleep", recording_sleep)

        await service._poll_loop("core.document.uploaded.v1", "g", "c", AsyncMock())

        # Poll 1 fails and takes the log+backoff path; poll 2 stops the loop
        # (the shutdown check runs before any further logging).
        assert live_polls["count"] == 2
        assert mock_logger.error.call_count == 1
        assert sleep_calls == [1]

    @pytest.mark.asyncio
    async def test_connect_sets_explicit_socket_options(self, service, mock_redis_client):
        """The client is created with socket options that outlast the block window,
        so blocking reads don't race the socket timeout in the first place (#90).
        """
        with patch("redis.asyncio.from_url", return_value=mock_redis_client) as mock_from_url:
            await service.connect()

        kwargs = mock_from_url.call_args.kwargs
        # Must comfortably exceed the 5s XREADGROUP block window.
        assert kwargs["socket_timeout"] > 5
        assert kwargs["socket_connect_timeout"] > 0
        assert kwargs["health_check_interval"] > 0


class TestRedisMQServiceSubscribe:
    """Tests for RedisMQService subscribe/unsubscribe functionality."""

    @pytest.mark.asyncio
    async def test_subscribe_creates_poll_task(self, service, mock_redis_client):
        """Test that subscribe() creates a poll task entry in _poll_tasks."""
        with patch("redis.asyncio.from_url", return_value=mock_redis_client):
            await service.connect()

        topic = "core.document.uploaded.v1"
        handler = AsyncMock()

        await service.subscribe(topic, handler, group_id="test-group")

        assert topic in service._poll_tasks
        task = service._poll_tasks[topic]
        assert isinstance(task, asyncio.Task)

        # Cleanup: cancel the task to avoid warnings
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    @pytest.mark.asyncio
    async def test_unsubscribe_removes_poll_task(self, service, mock_redis_client):
        """Test that unsubscribe() removes the poll task for the topic."""
        with patch("redis.asyncio.from_url", return_value=mock_redis_client):
            await service.connect()

        topic = "core.document.uploaded.v1"
        handler = AsyncMock()

        await service.subscribe(topic, handler, group_id="test-group")
        assert topic in service._poll_tasks

        await service.unsubscribe(topic)

        assert topic not in service._poll_tasks
