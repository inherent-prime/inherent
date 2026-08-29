"""Pytest configuration and fixtures."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.models.api_key import APIKeyInfo


def pytest_configure(config):
    """Register custom markers."""
    config.addinivalue_line("markers", "compose: mark test as requiring a Docker Compose stack")


def pytest_collection_modifyitems(config, items):
    """Enforce the 'not compose' default even when command-line -m is used.

    Problem: pytest's `-m` option REPLACES (not intersects) the default
    `addopts = "-m 'not compose'"` from pyproject.toml. So a developer
    running `pytest -m benchmark` would inadvertently select compose-marked
    tests that need a live Docker stack, causing confusing failures.

    Solution: If the effective marker expression does not mention "compose"
    (meaning it's not explicitly included or excluded), deselect all
    compose-marked items. This ensures the `not compose` safety default
    is honored even when -m overrides addopts.

    Rule: if "compose" is not in markexpr, deselect compose items.
    This handles:
    - No -m: markexpr = "not compose" (has "compose") → do nothing ✓
    - -m smoke: markexpr = "smoke" (no "compose") → deselect ✓
    - -m "compose and smoke": markexpr has "compose" → do nothing ✓
    - -m "not compose": markexpr has "compose" → do nothing ✓

    See issue #286.
    """
    # Get the effective marker expression (includes both addopts and command line)
    markexpr = config.option.markexpr

    # If the marker expression mentions "compose" (either inclusion or exclusion),
    # respect it and don't deselect. Otherwise, enforce the default by deselecting.
    if markexpr and "compose" in markexpr:
        return

    # Deselect all compose-marked tests
    deselected = [item for item in items if "compose" in item.keywords]
    if deselected:
        config.hook.pytest_deselected(items=deselected)
        items[:] = [item for item in items if "compose" not in item.keywords]


@pytest.fixture(autouse=True)
def _reset_rate_limiter_singleton():
    """Isolate the global rate limiter between tests.

    The limiter is a process-wide singleton whose in-memory buckets now count
    unauthenticated (per-IP) traffic too (#5). Without resetting it, every
    TestClient request shares one ``ip:testclient`` bucket that fills up across
    the session and 429s later tests. Reset to a fresh limiter per test.
    """
    import src.core.rate_limiter as rl

    rl._rate_limiter = None
    yield
    rl._rate_limiter = None


@pytest.fixture(autouse=True)
def _reset_service_singletons():
    """Isolate the process-wide service singletons between tests.

    ``_mq_service`` / ``_database`` / ``_search_service`` / ``_storage_service``
    are module-level globals that cache connections (redis, asyncpg) bound to the
    event loop of whichever test first created them. pytest-asyncio gives each
    test its own loop, so a singleton leaked from an earlier test carries a
    connection tied to a now-closed loop. A later test's ``TestClient`` lifespan
    *shutdown* calls ``close_*()`` on that stale singleton, which touches the dead
    loop and raises ``RuntimeError: Event loop is closed`` at teardown.

    Null the globals before and after each test so no test inherits another's
    connections. Mirrors ``_reset_rate_limiter_singleton`` above. Each test that
    legitimately builds a singleton does so on — and closes it on — its own loop.
    """
    import src.services.database as database
    import src.services.mq as mq
    import src.services.search as search
    import src.services.storage as storage

    def _clear() -> None:
        mq._mq_service = None
        database._database = None
        search._search_service = None
        storage._storage_service = None

    _clear()
    yield
    _clear()


@pytest.fixture
def mock_api_key_info():
    """Create a mock API key info with full permissions."""
    return APIKeyInfo(
        key_id="test-key-id",
        user_id="test-user-id",
        workspace_id="test-workspace-id",
        permissions=["read", "search"],
        rate_limit=100,
        expires_at=None,
        status="active",
    )


@pytest.fixture
def mock_api_key_info_write():
    """Create a mock API key info with full permissions including write."""
    return APIKeyInfo(
        key_id="test-key-write",
        user_id="test-user-id",
        workspace_id="test-workspace-id",
        permissions=["read", "search", "write"],
        rate_limit=100,
        expires_at=None,
        status="active",
    )


@pytest.fixture
def mock_api_key_info_read_only():
    """Create a mock API key info with read-only permission."""
    return APIKeyInfo(
        key_id="test-key-readonly",
        user_id="test-user-id",
        workspace_id="test-workspace-id",
        permissions=["read"],
        rate_limit=50,
        expires_at=None,
        status="active",
    )


@pytest.fixture
def mock_api_key_info_expired():
    """Create a mock expired API key info."""
    return APIKeyInfo(
        key_id="test-key-expired",
        user_id="test-user-id",
        workspace_id="test-workspace-id",
        permissions=["read", "search"],
        rate_limit=100,
        expires_at=datetime.now(timezone.utc) - timedelta(days=1),
        status="active",
    )


@pytest.fixture
def mock_api_key_info_revoked():
    """Create a mock revoked API key info."""
    return APIKeyInfo(
        key_id="test-key-revoked",
        user_id="test-user-id",
        workspace_id="test-workspace-id",
        permissions=["read", "search"],
        rate_limit=100,
        expires_at=None,
        status="revoked",
    )


@pytest.fixture
def mock_database():
    """Create a mock database service."""
    mock = AsyncMock()
    mock.validate_api_key = AsyncMock(return_value=None)
    mock.get_documents = AsyncMock(return_value=([], 0))
    mock.get_document = AsyncMock(return_value=None)
    mock.get_document_chunks = AsyncMock(return_value=[])
    mock.get_user_workspace_ids = AsyncMock(return_value=["test-workspace-id"])

    # Context manager for session
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock()
    mock.get_session = MagicMock(return_value=AsyncMock())
    mock.get_session.return_value.__aenter__ = AsyncMock(return_value=mock_session)
    mock.get_session.return_value.__aexit__ = AsyncMock()

    return mock


@pytest.fixture
def mock_search_service():
    """Create a mock search service."""
    mock = AsyncMock()
    mock.is_connected = AsyncMock(return_value=True)
    mock.search = AsyncMock(
        return_value=MagicMock(results=[], total_results=0, processing_time_ms=10)
    )
    return mock


@pytest.fixture
def mock_rate_limiter():
    """Create a mock rate limiter."""
    from src.core.rate_limiter import RateLimitInfo, RateLimitResult

    mock = AsyncMock()
    mock.check_rate_limit = AsyncMock(
        return_value=RateLimitResult(
            allowed=True,
            info=RateLimitInfo(
                limit=100,
                remaining=99,
                reset_at=datetime.now(timezone.utc).timestamp() + 60,
                window_seconds=60,
            ),
        )
    )
    mock.get_current_state = AsyncMock(
        return_value=RateLimitInfo(
            limit=100,
            remaining=99,
            reset_at=datetime.now(timezone.utc).timestamp() + 60,
            window_seconds=60,
        )
    )
    return mock


@pytest.fixture
def sample_document():
    """Create a sample document for testing."""
    return {
        "id": "doc-123",
        "name": "test-document.txt",
        "workspace_id": "test-workspace-id",
        "source_type": "upload",
        "mime_type": "text/plain",
        "size_bytes": 1024,
        "chunk_count": 5,
        "status": "processed",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


@pytest.fixture
def sample_chunk():
    """Create a sample document chunk for testing."""
    return {
        "id": "chunk-123",
        "document_id": "doc-123",
        "content": "This is sample content for testing.",
        "chunk_index": 0,
        "token_count": 10,
        "metadata": {"heading": "Introduction"},
    }
