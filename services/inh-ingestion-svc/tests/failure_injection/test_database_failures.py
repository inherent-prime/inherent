"""Failure-injection: database (PostgreSQL) errors must propagate.

A transient DB outage (connection drop, deadlock) during a write must raise
so the ingestion step fails and is retried — never silently treated as a
successful persist. We mock the SQLAlchemy session/engine so no live
PostgreSQL is required.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from sqlalchemy.exc import OperationalError

from src.config.settings import Settings
from src.services.database import DatabaseService, DocumentStatus

pytestmark = pytest.mark.failure_injection


@pytest.fixture
def mock_settings():
    settings = MagicMock(spec=Settings)
    settings.database_url = "postgresql://user:pass@example.invalid:5432/db"
    return settings


def _operational_error() -> OperationalError:
    """A SQLAlchemy OperationalError as raised on connection loss."""
    return OperationalError("SELECT 1", {}, Exception("server closed the connection"))


def _wire_failing_session(service: DatabaseService) -> None:
    """Make the service appear connected, but every session.execute fails.

    ``get_session`` requires both ``engine`` and ``SessionLocal``. We provide
    mocks so the method body runs, then have ``execute`` raise to simulate a
    live failure mid-statement.
    """
    service.engine = MagicMock()

    session = MagicMock()
    session.execute.side_effect = _operational_error()

    session_factory = MagicMock(return_value=session)
    service.SessionLocal = session_factory
    service._failing_session = session  # exposed for assertions


async def test_store_processed_document_propagates_operational_error(mock_settings):
    """A DB OperationalError during document persist must propagate."""
    service = DatabaseService(mock_settings)
    _wire_failing_session(service)

    message = MagicMock()
    message.document_id = "doc-1"
    message.workspace_id = "ws1"
    message.user_id = "user1"
    message.filename = "f.pdf"
    message.original_filename = "f.pdf"
    message.content_type = "application/pdf"
    message.size_bytes = 10
    message.storage_backend = "local"
    message.storage_path = "p"
    message.storage_bucket = None
    message.storage_url = None

    with pytest.raises(OperationalError):
        await service.store_processed_document(
            message=message,
            chunks=[],
            text_length=0,
            processing_time_ms=1,
            workflow_run_id="test-run",
        )

    # The session must be rolled back, not committed, on failure.
    service._failing_session.rollback.assert_called_once()
    service._failing_session.commit.assert_not_called()


async def test_update_document_status_propagates_operational_error(mock_settings):
    """A status-update write that hits a DB error must propagate."""
    service = DatabaseService(mock_settings)
    _wire_failing_session(service)

    with pytest.raises(OperationalError):
        await service.update_document_status(
            document_id="doc-1",
            status=DocumentStatus.PROCESSED,
        )

    service._failing_session.rollback.assert_called_once()


async def test_get_document_status_propagates_operational_error(mock_settings):
    """A read query that hits a DB error must propagate (not return None)."""
    service = DatabaseService(mock_settings)
    _wire_failing_session(service)

    with pytest.raises(OperationalError):
        await service.get_document_status("doc-1")


async def test_methods_raise_when_engine_missing(mock_settings):
    """With no engine (never connected / connection lost), methods raise."""
    service = DatabaseService(mock_settings)
    service.engine = None

    with pytest.raises(RuntimeError, match="Database not connected"):
        await service.get_document_status("doc-1")
