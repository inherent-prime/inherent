"""Regression: service singletons must not leak across tests (event-loop pollution).

The module-level singletons in ``src.services`` (``_mq_service``, ``_database``,
``_search_service``, ``_storage_service``) hold connections bound to the event
loop of whichever test first created them. A later test's ``TestClient`` lifespan
*shutdown* calls ``close_*()`` on those singletons; if a singleton leaked from an
earlier (now-closed) event loop, the close touches a dead loop and raises
``RuntimeError: Event loop is closed`` at teardown.

These two tests reproduce that ordering: the first leaks a singleton whose
``close()`` blows up like a stale-loop connection would; the second runs a real
lifespan shutdown that must survive it. An autouse reset fixture (see
``tests/conftest.py``) resets the singletons between tests so the leak never
reaches the second test's shutdown.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

import src.services.mq as mq
from src.main import create_app


def test_a_leaks_mq_singleton_with_stale_loop_close() -> None:
    """Simulate an earlier test that leaves a connected MQ singleton behind.

    Its ``_redis.close()`` raises the exact error a connection bound to a
    now-closed event loop would raise, standing in for real cross-loop pollution.
    """
    svc = mq.MQService.__new__(mq.MQService)  # skip __init__ (no real redis)
    stale_redis = AsyncMock()
    stale_redis.close = AsyncMock(side_effect=RuntimeError("Event loop is closed"))
    svc._redis = stale_redis
    svc._connected = True

    # Leak it: a real polluting test never resets the module global.
    mq._mq_service = svc

    assert mq._mq_service is not None


def test_b_lifespan_shutdown_survives_leaked_singleton() -> None:
    """A later test's TestClient shutdown must not error on a leaked singleton.

    Without the reset fixture, the singleton leaked by ``test_a`` survives into
    this test; the ``TestClient`` context-manager exit runs the lifespan
    shutdown, which calls ``close_mq_service()`` and raises at teardown.
    """
    app = create_app()
    with patch("src.main.get_database", new_callable=AsyncMock):
        with TestClient(app) as client:
            assert client.get("/health").status_code == 200
    # Reaching here (clean TestClient exit) means shutdown did not raise.
