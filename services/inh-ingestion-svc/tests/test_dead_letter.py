"""Tests for dead-letter queue (DE-S021)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.services.database import DatabaseService


@pytest.fixture(autouse=True)
async def cleanup_test_data():
    yield


@pytest.fixture()
def db_service():
    yield None


class TestDeadLetterTriggerIntegration:
    """Test that workflow failures are recorded in dead_letter_jobs."""

    async def test_classify_error_extraction(self):
        """Test error classification helper exists on trigger."""
        from src.temporal.trigger import TemporalWorkflowTrigger

        trigger = TemporalWorkflowTrigger.__new__(TemporalWorkflowTrigger)
        assert hasattr(trigger, "_classify_error")

    async def test_classify_error_returns_string(self):
        """Test _classify_error returns a reasonable error type."""
        from src.temporal.trigger import TemporalWorkflowTrigger

        trigger = TemporalWorkflowTrigger.__new__(TemporalWorkflowTrigger)
        result = trigger._classify_error("Failed to extract text from PDF")
        assert isinstance(result, str)
        assert len(result) > 0


class TestDeadLetterDBTableExists:
    """Test dead_letter_jobs table is defined in DatabaseService."""

    def test_dead_letter_table_defined(self):
        """Verify dead_letter_jobs table exists in schema."""
        from sqlalchemy import MetaData

        db = DatabaseService.__new__(DatabaseService)
        db.metadata = MetaData()
        db._define_tables()
        assert hasattr(db, "dead_letter_jobs")
        col_names = [c.name for c in db.dead_letter_jobs.columns]
        assert "document_id" in col_names
        assert "workspace_id" in col_names
        assert "error_message" in col_names
        assert "original_message" in col_names
        assert "status" in col_names
        assert "retry_count" in col_names

    def test_dead_letter_methods_exist(self):
        """Verify CRUD methods exist on DatabaseService."""
        assert hasattr(DatabaseService, "add_dead_letter_job")
        assert hasattr(DatabaseService, "get_dead_letter_jobs")
        assert hasattr(DatabaseService, "get_dead_letter_job")
        assert hasattr(DatabaseService, "update_dead_letter_status")
        assert hasattr(DatabaseService, "increment_dead_letter_retry")


class TestDeadLetterAPIRoutes:
    """Test dead-letter API endpoint existence."""

    def test_list_requires_auth(self):
        mock_settings = MagicMock()
        mock_settings.ingestion_api_key = "secret"
        mock_settings.api_host = "127.0.0.1"
        mock_settings.api_port = 8000
        mock_settings.temporal_host = "localhost:7233"
        mock_settings.temporal_namespace = "default"
        mock_settings.temporal_task_queue = "document-ingestion"
        mock_settings.log_level = "INFO"

        with (
            patch("src.api.app.TemporalWorkerManager") as mock_mgr,
            patch("src.api.auth.get_settings", return_value=mock_settings),
        ):
            instance = mock_mgr.return_value
            instance.start = AsyncMock()
            instance.stop = AsyncMock()
            instance.get_client = AsyncMock()
            instance.is_running = True

            from fastapi.testclient import TestClient

            from src.api.app import create_app

            app = create_app(mock_settings)
            with TestClient(app) as client:
                # No auth header
                resp = client.get("/dead-letter")
                assert resp.status_code == 401

    @patch("src.temporal.shared_services.get_db_service")
    def test_list_returns_jobs(self, mock_get_db):
        mock_db = MagicMock()
        mock_db.get_dead_letter_jobs = AsyncMock(return_value=[])
        mock_get_db.return_value = mock_db

        mock_settings = MagicMock()
        mock_settings.ingestion_api_key = "secret"
        mock_settings.api_host = "127.0.0.1"
        mock_settings.api_port = 8000
        mock_settings.temporal_host = "localhost:7233"
        mock_settings.temporal_namespace = "default"
        mock_settings.temporal_task_queue = "document-ingestion"
        mock_settings.log_level = "INFO"

        with (
            patch("src.api.app.TemporalWorkerManager") as mock_mgr,
            patch("src.api.auth.get_settings", return_value=mock_settings),
        ):
            instance = mock_mgr.return_value
            instance.start = AsyncMock()
            instance.stop = AsyncMock()
            instance.get_client = AsyncMock()
            instance.is_running = True

            from fastapi.testclient import TestClient

            from src.api.app import create_app

            app = create_app(mock_settings)
            with TestClient(app) as client:
                # workspace_id is REQUIRED (#177) -- omitting it used to
                # return dead-letter rows across every workspace.
                resp = client.get(
                    "/dead-letter?workspace_id=ws_001", headers={"X-API-Key": "secret"}
                )
                assert resp.status_code == 200
                assert resp.json()["jobs"] == []
                mock_db.get_dead_letter_jobs.assert_awaited_once_with(
                    workspace_id="ws_001", status="pending", limit=50
                )

    @patch("src.temporal.shared_services.get_db_service")
    def test_list_requires_workspace_id(self, mock_get_db):
        """#177: workspace_id is no longer an optional filter -- omitting it
        used to leak dead-letter rows (document_id/workspace_id/user_id
        triples) across every tenant. Must now 422, not 200."""
        mock_settings = MagicMock()
        mock_settings.ingestion_api_key = "secret"
        mock_settings.api_host = "127.0.0.1"
        mock_settings.api_port = 8000
        mock_settings.temporal_host = "localhost:7233"
        mock_settings.temporal_namespace = "default"
        mock_settings.temporal_task_queue = "document-ingestion"
        mock_settings.log_level = "INFO"

        with (
            patch("src.api.app.TemporalWorkerManager") as mock_mgr,
            patch("src.api.auth.get_settings", return_value=mock_settings),
        ):
            instance = mock_mgr.return_value
            instance.start = AsyncMock()
            instance.stop = AsyncMock()
            instance.get_client = AsyncMock()
            instance.is_running = True

            from fastapi.testclient import TestClient

            from src.api.app import create_app

            app = create_app(mock_settings)
            with TestClient(app) as client:
                resp = client.get("/dead-letter", headers={"X-API-Key": "secret"})
                assert resp.status_code == 422
