"""Tests for cascade document deletion to Weaviate (DE-S036).

Covers:
- WeaviateService.delete_document_chunks_graceful()
- DELETE /documents/{document_id} endpoint
- Graceful handling when Weaviate is unavailable
- Weaviate failure does not block PG delete
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.config.settings import Settings
from src.services.weaviate import WeaviateService, get_user_tenant_name

# ---------------------------------------------------------------------------
# Override conftest autouse fixtures -- these tests don't need PostgreSQL
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
async def cleanup_test_data():
    """Override global autouse cleanup -- no DB needed."""
    yield


@pytest.fixture()
def db_service():
    """Override -- not used directly."""
    yield None


# ---------------------------------------------------------------------------
# WeaviateService unit tests
# ---------------------------------------------------------------------------


class TestDeleteDocumentChunksGraceful:
    """Tests for WeaviateService.delete_document_chunks_graceful()."""

    @pytest.fixture
    def mock_settings(self):
        settings = MagicMock(spec=Settings)
        settings.weaviate_url = "http://localhost:8080"
        settings.weaviate_api_key = None
        return settings

    @pytest.fixture
    def weaviate_service(self, mock_settings):
        service = WeaviateService(mock_settings)
        service.client = MagicMock()
        service.client.is_ready.return_value = True
        return service

    async def test_successful_deletion(self, weaviate_service):
        """Graceful delete calls batch delete with correct filters and returns success."""
        mock_collection = MagicMock()
        mock_tenant_collection = MagicMock()
        weaviate_service.client.collections.get.return_value = mock_collection
        mock_collection.with_tenant.return_value = mock_tenant_collection

        mock_result = MagicMock()
        mock_result.successful = 3
        mock_tenant_collection.data.delete_many.return_value = mock_result

        success, count = await weaviate_service.delete_document_chunks_graceful(
            workspace_id="ws_test",
            document_id="doc_test",
            user_id="user_test",
        )

        assert success is True
        assert count == 3
        mock_collection.with_tenant.assert_called_with(get_user_tenant_name("user_test"))
        mock_tenant_collection.data.delete_many.assert_called_once()

    async def test_client_not_connected(self, weaviate_service):
        """Returns (False, 0) when the Weaviate client is None."""
        weaviate_service.client = None

        success, count = await weaviate_service.delete_document_chunks_graceful(
            workspace_id="ws1",
            document_id="doc1",
            user_id="user1",
        )

        assert success is False
        assert count == 0

    async def test_client_not_ready(self, weaviate_service):
        """Returns (False, 0) when the Weaviate client is not ready."""
        weaviate_service.client.is_ready.return_value = False

        success, count = await weaviate_service.delete_document_chunks_graceful(
            workspace_id="ws1",
            document_id="doc1",
            user_id="user1",
        )

        assert success is False
        assert count == 0

    async def test_weaviate_exception_is_swallowed(self, weaviate_service):
        """Exceptions from Weaviate are caught; returns (False, 0)."""
        weaviate_service.client.collections.get.side_effect = Exception("connection refused")

        success, count = await weaviate_service.delete_document_chunks_graceful(
            workspace_id="ws1",
            document_id="doc1",
            user_id="user1",
        )

        assert success is False
        assert count == 0

    async def test_zero_chunks_deleted(self, weaviate_service):
        """Returns (True, 0) when the document has no chunks in Weaviate."""
        mock_collection = MagicMock()
        mock_tenant_collection = MagicMock()
        weaviate_service.client.collections.get.return_value = mock_collection
        mock_collection.with_tenant.return_value = mock_tenant_collection

        mock_result = MagicMock()
        mock_result.successful = 0
        mock_tenant_collection.data.delete_many.return_value = mock_result

        success, count = await weaviate_service.delete_document_chunks_graceful(
            workspace_id="ws1",
            document_id="doc_none",
            user_id="user1",
        )

        assert success is True
        assert count == 0


# ---------------------------------------------------------------------------
# DELETE /documents/{document_id} endpoint tests
# ---------------------------------------------------------------------------

VALID_API_KEY = "test-secret-key-abc123"


def _make_mock_settings(**overrides):
    defaults = {
        "ingestion_api_key": VALID_API_KEY,
        "api_host": "127.0.0.1",
        "api_port": 8000,
        "temporal_host": "localhost:7233",
        "temporal_namespace": "default",
        "temporal_task_queue": "document-ingestion",
        "log_level": "INFO",
    }
    defaults.update(overrides)
    s = MagicMock()
    for k, v in defaults.items():
        setattr(s, k, v)
    return s


@pytest.fixture()
def client():
    """Yield a TestClient with mocked Temporal and shared services."""
    mock_settings = _make_mock_settings()

    mock_temporal_client = AsyncMock()
    mock_handle = AsyncMock()
    mock_temporal_client.start_workflow = AsyncMock(return_value=mock_handle)

    with (
        patch("src.api.app.TemporalWorkerManager") as mock_manager_cls,
        patch("src.api.auth.get_settings", return_value=mock_settings),
    ):
        instance = mock_manager_cls.return_value
        instance.start = AsyncMock()
        instance.stop = AsyncMock()
        instance.get_client = AsyncMock(return_value=mock_temporal_client)
        instance.is_running = True

        from src.api.app import create_app

        app = create_app(mock_settings)

        with TestClient(app) as tc:
            yield tc


class TestDeleteDocumentEndpoint:
    """Tests for DELETE /documents/{document_id}."""

    def test_requires_auth(self, client: TestClient):
        resp = client.delete("/documents/doc1?workspace_id=ws1&user_id=u1")
        assert resp.status_code == 401

    def test_requires_workspace_and_user_params(self, client: TestClient):
        resp = client.delete(
            "/documents/doc1",
            headers={"X-API-Key": VALID_API_KEY},
        )
        assert resp.status_code == 422  # missing query params

    @patch("src.temporal.shared_services.get_db_service")
    @patch("src.temporal.shared_services.get_weaviate_service")
    def test_successful_delete_with_weaviate(
        self, mock_get_weaviate, mock_get_db, client: TestClient
    ):
        """Full delete: PG + Weaviate both succeed."""
        mock_weaviate_svc = MagicMock()
        mock_weaviate_svc.delete_document_chunks_graceful = AsyncMock(return_value=(True, 5))
        mock_get_weaviate.return_value = mock_weaviate_svc

        mock_db_svc = MagicMock()
        mock_db_svc.delete_document = AsyncMock(return_value=True)
        mock_get_db.return_value = mock_db_svc

        resp = client.delete(
            "/documents/doc1?workspace_id=ws1&user_id=u1",
            headers={"X-API-Key": VALID_API_KEY},
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["deleted"] is True
        assert data["document_id"] == "doc1"
        assert data["weaviate_cleaned"] is True

        mock_db_svc.delete_document.assert_called_once_with("doc1")
        mock_weaviate_svc.delete_document_chunks_graceful.assert_called_once_with(
            workspace_id="ws1",
            document_id="doc1",
            user_id="u1",
        )

    @patch("src.temporal.shared_services.get_db_service")
    @patch("src.temporal.shared_services.get_weaviate_service")
    def test_weaviate_failure_does_not_block_pg_delete(
        self, mock_get_weaviate, mock_get_db, client: TestClient
    ):
        """Weaviate fails but PG delete still succeeds; weaviate_cleaned=false."""
        mock_weaviate_svc = MagicMock()
        mock_weaviate_svc.delete_document_chunks_graceful = AsyncMock(return_value=(False, 0))
        mock_get_weaviate.return_value = mock_weaviate_svc

        mock_db_svc = MagicMock()
        mock_db_svc.delete_document = AsyncMock(return_value=True)
        mock_get_db.return_value = mock_db_svc

        resp = client.delete(
            "/documents/doc1?workspace_id=ws1&user_id=u1",
            headers={"X-API-Key": VALID_API_KEY},
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["deleted"] is True
        assert data["weaviate_cleaned"] is False
        mock_db_svc.delete_document.assert_called_once()

    @patch("src.temporal.shared_services.get_db_service")
    @patch("src.temporal.shared_services.get_weaviate_service")
    def test_weaviate_service_unavailable(self, mock_get_weaviate, mock_get_db, client: TestClient):
        """Weaviate service is None (unavailable); PG delete still works."""
        mock_get_weaviate.return_value = None

        mock_db_svc = MagicMock()
        mock_db_svc.delete_document = AsyncMock(return_value=True)
        mock_get_db.return_value = mock_db_svc

        resp = client.delete(
            "/documents/doc1?workspace_id=ws1&user_id=u1",
            headers={"X-API-Key": VALID_API_KEY},
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["deleted"] is True
        assert data["weaviate_cleaned"] is False

    @patch("src.temporal.shared_services.get_db_service")
    @patch("src.temporal.shared_services.get_weaviate_service")
    def test_document_not_found_returns_404(
        self, mock_get_weaviate, mock_get_db, client: TestClient
    ):
        """PG delete returns False (no matching row) -> 404."""
        mock_get_weaviate.return_value = None

        mock_db_svc = MagicMock()
        mock_db_svc.delete_document = AsyncMock(return_value=False)
        mock_get_db.return_value = mock_db_svc

        resp = client.delete(
            "/documents/doc_nonexistent?workspace_id=ws1&user_id=u1",
            headers={"X-API-Key": VALID_API_KEY},
        )

        assert resp.status_code == 404
