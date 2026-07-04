"""A document must be observable in the status API before the store step (#10).

No processed_documents row existed until the store step, so an early
'processing'/'failed' status write hit 0 rows and a document that failed during
fetch/extract/chunk showed 'not found'. The workflow now creates a minimal
'processing' row up front.
"""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.services.database import DatabaseService, DocumentStatus
from src.temporal.activities.status import create_pending_document
from src.temporal.models import CreatePendingDocumentInput


def _input() -> CreatePendingDocumentInput:
    return CreatePendingDocumentInput(
        document_id="doc-1",
        workspace_id="ws",
        user_id="u",
        filename="f.txt",
        original_filename="orig.txt",
        content_type="text/plain",
        size_bytes=10,
        storage_backend="local",
        storage_path="p",
    )


@pytest.mark.asyncio
async def test_activity_delegates_to_db():
    db = MagicMock()
    db.create_pending_document = AsyncMock(return_value=True)
    with patch("src.temporal.shared_services.get_db_service", return_value=db):
        result = await create_pending_document(_input())
    assert result is True
    kwargs = db.create_pending_document.await_args.kwargs
    assert kwargs["document_id"] == "doc-1"
    assert kwargs["workspace_id"] == "ws"


@pytest.mark.asyncio
async def test_db_create_pending_returns_true_on_insert():
    session = MagicMock()
    session.execute.return_value = MagicMock(rowcount=1)
    # Use a fully-initialised service so self.processed_documents exists.
    from src.config.settings import Settings

    db = DatabaseService.__new__(DatabaseService)
    DatabaseService.__init__(db, Settings.model_construct())
    db.engine = MagicMock()

    @contextmanager
    def _gs():
        yield session

    db.get_session = _gs

    created = await db.create_pending_document(
        document_id="doc-1",
        workspace_id="ws",
        user_id="u",
        filename="f.txt",
        original_filename="orig.txt",
        content_type="text/plain",
        size_bytes=10,
        storage_backend="local",
        storage_path="p",
    )
    assert created is True
    assert session.execute.call_count == 1
    assert DocumentStatus.PROCESSING.value == "processing"
