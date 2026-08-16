"""App-flow test configuration and fixtures.

In-process app-flow tests with mocked dependencies. NOT end-to-end -- live
E2E lives in tests/integration/test_compose_*.py. Provides a fully mocked
FastAPI app with httpx.AsyncClient for exercising API request/response flow
without real database or search service connections.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from src.main import create_app
from src.models.api_key import APIKeyInfo
from src.models.search import SearchResponse
from src.services.auth import (
    ResolvedAuth,
    get_api_key_info,
    get_read_permission,
    get_search_permission,
    get_write_permission,
    resolve_workspace_read,
    resolve_workspace_search,
    resolve_workspace_write,
)
from src.services.database import get_database
from src.services.search import get_search_service

# ---------------------------------------------------------------------------
# Service mocks
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_db():
    """Mock database service."""
    mock = AsyncMock()
    mock.validate_api_key = AsyncMock(return_value=None)
    mock.get_documents = AsyncMock(return_value=([], 0))
    mock.get_document = AsyncMock(return_value=None)
    mock.get_document_chunks = AsyncMock(return_value=[])
    mock.get_user_workspace_ids = AsyncMock(return_value=["ws-123"])
    mock.get_documents_multi_workspace = AsyncMock(return_value=([], 0))
    mock.get_document_by_id = AsyncMock(return_value=None)
    mock.get_document_chunks_by_doc_id = AsyncMock(return_value=[])
    # Upload lifecycle writes (dedup + durable pending row + failure marking).
    mock.get_document_id_by_content_hash = AsyncMock(return_value=None)
    mock.get_document_id_by_filename = AsyncMock(return_value=None)
    mock.create_or_reset_pending_document = AsyncMock(return_value=None)
    mock.mark_document_failed = AsyncMock(return_value=None)
    mock.is_connected = True

    # Context manager for session
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock()
    mock.session = MagicMock(return_value=AsyncMock())
    mock.session.return_value.__aenter__ = AsyncMock(return_value=mock_session)
    mock.session.return_value.__aexit__ = AsyncMock()

    return mock


@pytest.fixture
def mock_search():
    """Mock search service."""
    mock = AsyncMock()
    mock.is_connected = AsyncMock(return_value=True)
    mock.search = AsyncMock(
        return_value=SearchResponse(
            results=[], query="", total_results=0, processing_time_ms=10, search_mode="semantic"
        )
    )
    return mock


# ---------------------------------------------------------------------------
# API key fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def valid_api_key_info():
    """API key with full permissions (read, search, write)."""
    return APIKeyInfo(
        key_id="e2e-key-id",
        user_id="e2e-user-id",
        workspace_id="ws-123",
        permissions=["read", "search", "write"],
        rate_limit=1000,
        expires_at=None,
        status="active",
    )


@pytest.fixture
def read_only_api_key_info():
    """API key with read-only permission."""
    return APIKeyInfo(
        key_id="e2e-key-readonly",
        user_id="e2e-user-id",
        workspace_id="ws-123",
        permissions=["read"],
        rate_limit=1000,
        expires_at=None,
        status="active",
    )


@pytest.fixture
def expired_api_key_info():
    """API key that expired yesterday."""
    return APIKeyInfo(
        key_id="e2e-key-expired",
        user_id="e2e-user-id",
        workspace_id="ws-123",
        permissions=["read", "search"],
        rate_limit=1000,
        expires_at=datetime.now(timezone.utc) - timedelta(days=1),
        status="active",
    )


# ---------------------------------------------------------------------------
# App and client fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def app(mock_db, mock_search, valid_api_key_info):
    """Create a test FastAPI app with mocked services."""
    test_app = create_app()

    # Override FastAPI dependencies
    async def override_db():
        return mock_db

    async def override_search():
        return mock_search

    async def override_api_key():
        return valid_api_key_info

    async def override_search_perm():
        return valid_api_key_info

    async def override_read_perm():
        return valid_api_key_info

    resolved_auth = ResolvedAuth(
        key_info=valid_api_key_info, workspace_id=valid_api_key_info.workspace_id
    )

    test_app.dependency_overrides[get_database] = override_db
    test_app.dependency_overrides[get_search_service] = override_search
    test_app.dependency_overrides[get_api_key_info] = override_api_key
    test_app.dependency_overrides[get_search_permission] = override_search_perm
    test_app.dependency_overrides[get_read_permission] = override_read_perm
    test_app.dependency_overrides[resolve_workspace_read] = lambda: resolved_auth
    test_app.dependency_overrides[resolve_workspace_search] = lambda: resolved_auth
    test_app.dependency_overrides[resolve_workspace_write] = lambda: resolved_auth
    test_app.dependency_overrides[get_write_permission] = lambda: valid_api_key_info

    yield test_app

    test_app.dependency_overrides.clear()


@pytest.fixture
async def client(app):
    """Create an authenticated async HTTP client for E2E testing."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture
async def unauth_client(mock_db, mock_search):
    """Create a client without auth overrides (for testing auth failures)."""
    test_app = create_app()

    # Override DB/search to avoid real connections, but NOT auth
    async def override_db():
        return mock_db

    async def override_search():
        return mock_search

    test_app.dependency_overrides[get_database] = override_db
    test_app.dependency_overrides[get_search_service] = override_search

    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    test_app.dependency_overrides.clear()


@pytest.fixture
async def read_only_client(mock_db, mock_search, read_only_api_key_info):
    """Create a client with read-only API key (no search or write permissions)."""
    test_app = create_app()

    async def override_db():
        return mock_db

    async def override_search():
        return mock_search

    async def override_api_key():
        return read_only_api_key_info

    async def override_read_perm():
        return read_only_api_key_info

    resolved_auth = ResolvedAuth(
        key_info=read_only_api_key_info, workspace_id=read_only_api_key_info.workspace_id
    )

    test_app.dependency_overrides[get_database] = override_db
    test_app.dependency_overrides[get_search_service] = override_search
    test_app.dependency_overrides[get_api_key_info] = override_api_key
    test_app.dependency_overrides[get_read_permission] = override_read_perm
    test_app.dependency_overrides[resolve_workspace_read] = lambda: resolved_auth
    # Note: get_search_permission is NOT overridden — search requests will fail

    yield test_app

    test_app.dependency_overrides.clear()
