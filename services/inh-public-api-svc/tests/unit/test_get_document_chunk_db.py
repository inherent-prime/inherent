"""Unit tests for DatabaseService.get_document_chunk (#87 single-chunk fetch).

Mirrors the surface/tenancy smoke style of test_eval_db_sql.py: pins the method
exists, is async, and is workspace-scoped (tenancy guard). Also exercises the
raw-SQL path against a mocked session to confirm the None-on-absent/foreign
behavior and the row-to-model mapping, mirroring how get_document_chunks
(database.py:684) and get_document (database.py:545) are tested elsewhere in
this module family.
"""

import inspect
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.services.database import DatabaseService


def test_get_document_chunk_exists_and_is_async():
    fn = getattr(DatabaseService, "get_document_chunk", None)
    assert fn is not None, "DatabaseService.get_document_chunk missing"
    assert inspect.iscoroutinefunction(fn), "DatabaseService.get_document_chunk must be async"


def test_get_document_chunk_is_workspace_scoped():
    """Tenancy guard: the method must take workspace_id explicitly."""
    params = inspect.signature(DatabaseService.get_document_chunk).parameters
    assert "workspace_id" in params
    assert "document_id" in params
    assert "chunk_id" in params


@pytest.mark.asyncio
async def test_get_document_chunk_returns_none_when_row_absent():
    """No matching row (wrong chunk_id, wrong document_id, or foreign
    workspace_id) -> None, never raises."""
    db = DatabaseService.__new__(DatabaseService)

    mock_result = MagicMock()
    mock_result.fetchone = MagicMock(return_value=None)

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)

    class _SessionCtx:
        async def __aenter__(self):
            return mock_session

        async def __aexit__(self, *args):
            return False

    db.session = MagicMock(return_value=_SessionCtx())

    result = await db.get_document_chunk(
        document_id="doc-1", chunk_id="chunk-missing", workspace_id="ws-1"
    )
    assert result is None


@pytest.mark.asyncio
async def test_get_document_chunk_maps_row_to_model():
    """A matching row is mapped into a DocumentChunk with provenance folded
    into metadata (mirrors get_document_chunks row mapping)."""
    db = DatabaseService.__new__(DatabaseService)

    row = MagicMock()
    row.id = "chunk-1"
    row.document_id = "doc-1"
    row.content = "hello world"
    row.chunk_index = 0
    row.token_count = 5
    row.metadata = {"heading": "Intro"}
    row.content_hash = "abc123"
    row.source_uri = "s3://bucket/doc.pdf"
    row.ingested_at = datetime(2024, 1, 1, tzinfo=timezone.utc)

    mock_result = MagicMock()
    mock_result.fetchone = MagicMock(return_value=row)

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)

    class _SessionCtx:
        async def __aenter__(self):
            return mock_session

        async def __aexit__(self, *args):
            return False

    db.session = MagicMock(return_value=_SessionCtx())

    result = await db.get_document_chunk(
        document_id="doc-1", chunk_id="chunk-1", workspace_id="ws-1"
    )
    assert result is not None
    assert result.id == "chunk-1"
    assert result.document_id == "doc-1"
    assert result.content == "hello world"
    assert result.chunk_index == 0
    assert result.token_count == 5
    assert result.metadata["heading"] == "Intro"
    assert result.metadata["content_hash"] == "abc123"


@pytest.mark.asyncio
async def test_get_document_chunk_query_is_workspace_scoped_in_sql():
    """The query must filter on workspace_id so a foreign workspace's chunk_id
    can never be returned, even if the chunk_id exists elsewhere."""
    db = DatabaseService.__new__(DatabaseService)

    mock_result = MagicMock()
    mock_result.fetchone = MagicMock(return_value=None)

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)

    class _SessionCtx:
        async def __aenter__(self):
            return mock_session

        async def __aexit__(self, *args):
            return False

    db.session = MagicMock(return_value=_SessionCtx())

    await db.get_document_chunk(document_id="doc-1", chunk_id="chunk-1", workspace_id="ws-1")

    assert mock_session.execute.await_count == 1
    call_args = mock_session.execute.await_args
    query_text = str(call_args.args[0])
    params = call_args.args[1]
    assert "workspace_id" in query_text
    assert params == {
        "document_id": "doc-1",
        "chunk_id": "chunk-1",
        "workspace_id": "ws-1",
    }
