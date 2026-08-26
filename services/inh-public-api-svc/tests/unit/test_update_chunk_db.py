"""Unit tests for DatabaseService.update_document_chunk (#133)."""

from __future__ import annotations

import hashlib
import inspect
import math
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


def _est(text: str) -> int:
    if not text:
        return 0
    return int(math.ceil(max(len(text.split()) * 1.3, len(text) / 4)))


def test_update_document_chunk_exists_and_is_async():
    fn = getattr(DatabaseService, "update_document_chunk", None)
    assert fn is not None
    assert inspect.iscoroutinefunction(fn)


def test_update_document_chunk_is_workspace_scoped():
    params = inspect.signature(DatabaseService.update_document_chunk).parameters
    assert "workspace_id" in params
    assert "chunk_index" in params
    assert "content" in params


@pytest.mark.asyncio
async def test_update_returns_none_when_absent():
    db = DatabaseService.__new__(DatabaseService)
    mock_result = MagicMock()
    mock_result.fetchone = MagicMock(return_value=None)
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.commit = AsyncMock()
    db.session = MagicMock(return_value=_session_ctx(mock_session))

    result = await db.update_document_chunk("doc-1", "ws-1", 0, "new")
    assert result is None
    mock_session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_recomputes_hash_and_tokens():
    db = DatabaseService.__new__(DatabaseService)
    content = "edited text"
    row = MagicMock()
    row.id = 9
    row.document_id = "doc-1"
    row.content = content
    row.chunk_index = 1
    row.token_count = _est(content)
    row.metadata = None
    row.content_hash = hashlib.sha256(content.encode()).hexdigest()
    row.source_uri = None
    row.ingested_at = datetime(2026, 1, 2, tzinfo=timezone.utc)

    mock_result = MagicMock()
    mock_result.fetchone = MagicMock(return_value=row)
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.commit = AsyncMock()
    db.session = MagicMock(return_value=_session_ctx(mock_session))

    result = await db.update_document_chunk("doc-1", "ws-1", 1, content)

    assert result is not None
    assert result.content == content
    assert result.token_count == _est(content)
    assert result.metadata["content_hash"] == hashlib.sha256(content.encode()).hexdigest()

    params = mock_session.execute.await_args.args[1]
    assert params["content"] == content
    assert params["token_count"] == _est(content)
    assert params["content_hash"] == hashlib.sha256(content.encode()).hexdigest()
    assert params["chunk_index"] == 1
    assert params["workspace_id"] == "ws-1"
    assert params["only_if_content_hash"] is None
    sql = str(mock_session.execute.await_args.args[0])
    assert "workspace_id" in sql
    assert "only_if_content_hash" in sql
    assert "RETURNING" in sql.upper()


@pytest.mark.asyncio
async def test_update_cas_passes_expected_hash():
    db = DatabaseService.__new__(DatabaseService)
    mock_result = MagicMock()
    mock_result.fetchone = MagicMock(return_value=None)
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.commit = AsyncMock()
    db.session = MagicMock(return_value=_session_ctx(mock_session))

    result = await db.update_document_chunk(
        "doc-1", "ws-1", 1, "old", only_if_content_hash="hash-we-wrote"
    )
    assert result is None
    params = mock_session.execute.await_args.args[1]
    assert params["only_if_content_hash"] == "hash-we-wrote"
