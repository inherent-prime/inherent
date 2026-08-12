"""Unit tests for DatabaseService.delete_document_chunk (#133 Option A).

Hard-delete by chunk_index; gaps allowed (no sibling re-index). Workspace-scoped.
"""

from __future__ import annotations

import inspect
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.services.database import DatabaseService


def _session_ctx(mock_session):
    class _SessionCtx:
        async def __aenter__(self):
            return mock_session

        async def __aexit__(self, *args):
            return False

    return _SessionCtx()


def test_delete_document_chunk_exists_and_is_async():
    fn = getattr(DatabaseService, "delete_document_chunk", None)
    assert fn is not None, "DatabaseService.delete_document_chunk missing"
    assert inspect.iscoroutinefunction(fn)


def test_delete_document_chunk_is_workspace_scoped():
    params = inspect.signature(DatabaseService.delete_document_chunk).parameters
    assert "workspace_id" in params
    assert "document_id" in params
    assert "chunk_index" in params


@pytest.mark.asyncio
async def test_delete_returns_none_when_chunk_absent():
    db = DatabaseService.__new__(DatabaseService)

    mock_result = MagicMock()
    mock_result.fetchone = MagicMock(return_value=None)
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.commit = AsyncMock()
    db.session = MagicMock(return_value=_session_ctx(mock_session))

    result = await db.delete_document_chunk(
        document_id="doc-1", workspace_id="ws-1", chunk_index=99
    )
    assert result is None
    mock_session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_returns_deleted_chunk_and_does_not_reindex():
    """Deleting index 1 leaves 0 and 2 untouched (gaps OK — Option A)."""
    db = DatabaseService.__new__(DatabaseService)

    row = MagicMock()
    row.id = 55
    row.document_id = "doc-1"
    row.content = "middle"
    row.chunk_index = 1
    row.token_count = 3
    row.metadata = None
    row.content_hash = "abc"
    row.source_uri = None
    row.ingested_at = datetime(2026, 1, 1, tzinfo=timezone.utc)

    select_result = MagicMock()
    select_result.fetchone = MagicMock(return_value=row)
    delete_result = MagicMock()
    delete_result.rowcount = 1

    mock_session = AsyncMock()
    # SELECT → DELETE → bump processed_documents → bump workspace_metadata
    mock_session.execute = AsyncMock(
        side_effect=[select_result, delete_result, MagicMock(), MagicMock()]
    )
    mock_session.commit = AsyncMock()
    db.session = MagicMock(return_value=_session_ctx(mock_session))

    result = await db.delete_document_chunk(document_id="doc-1", workspace_id="ws-1", chunk_index=1)

    assert result is not None
    assert result.chunk_index == 1
    assert result.content == "middle"
    mock_session.commit.assert_awaited_once()

    # No UPDATE of sibling chunk_index values — only DELETE of this row.
    sql_texts = [str(c.args[0]) for c in mock_session.execute.await_args_list]
    assert any("DELETE FROM document_chunks" in s for s in sql_texts)
    assert not any("UPDATE document_chunks" in s and "chunk_index" in s for s in sql_texts)


@pytest.mark.asyncio
async def test_delete_rolls_back_when_delete_rowcount_is_not_one():
    db = DatabaseService.__new__(DatabaseService)

    row = MagicMock()
    row.id = 55
    row.document_id = "doc-1"
    row.content = "middle"
    row.chunk_index = 1
    row.token_count = 3
    row.metadata = None
    row.content_hash = "abc"
    row.source_uri = None
    row.ingested_at = datetime(2026, 1, 1, tzinfo=timezone.utc)

    select_result = MagicMock()
    select_result.fetchone = MagicMock(return_value=row)
    delete_result = MagicMock()
    delete_result.rowcount = 0

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(side_effect=[select_result, delete_result])
    mock_session.commit = AsyncMock()
    mock_session.rollback = AsyncMock()
    db.session = MagicMock(return_value=_session_ctx(mock_session))

    result = await db.delete_document_chunk(document_id="doc-1", workspace_id="ws-1", chunk_index=1)

    assert result is None
    mock_session.rollback.assert_awaited_once()
    mock_session.commit.assert_not_awaited()
    assert mock_session.execute.await_count == 2


@pytest.mark.asyncio
async def test_delete_query_is_workspace_scoped():
    db = DatabaseService.__new__(DatabaseService)

    mock_result = MagicMock()
    mock_result.fetchone = MagicMock(return_value=None)
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)
    db.session = MagicMock(return_value=_session_ctx(mock_session))

    await db.delete_document_chunk(document_id="doc-1", workspace_id="ws-1", chunk_index=0)

    query_text = str(mock_session.execute.await_args.args[0])
    params = mock_session.execute.await_args.args[1]
    assert "workspace_id" in query_text
    assert params == {
        "document_id": "doc-1",
        "workspace_id": "ws-1",
        "chunk_index": 0,
    }
