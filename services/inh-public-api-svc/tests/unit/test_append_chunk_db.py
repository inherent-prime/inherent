"""Unit tests for DatabaseService.append_document_chunk (#133 Option A).

Append-only: chunk_index = max(chunk_index)+1 (or 0 when empty). Computes
content_hash + token_count with chunk_math. Workspace-scoped; foreign doc → None.
"""

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


def test_append_document_chunk_exists_and_is_async():
    fn = getattr(DatabaseService, "append_document_chunk", None)
    assert fn is not None, "DatabaseService.append_document_chunk missing"
    assert inspect.iscoroutinefunction(fn)


def test_append_document_chunk_is_workspace_scoped():
    params = inspect.signature(DatabaseService.append_document_chunk).parameters
    assert "workspace_id" in params
    assert "document_id" in params
    assert "content" in params


@pytest.mark.asyncio
async def test_append_returns_none_when_document_absent():
    db = DatabaseService.__new__(DatabaseService)

    mock_result = MagicMock()
    mock_result.fetchone = MagicMock(return_value=None)

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.commit = AsyncMock()
    db.session = MagicMock(return_value=_session_ctx(mock_session))

    result = await db.append_document_chunk(
        document_id="missing", workspace_id="ws-1", content="hello"
    )
    assert result is None
    mock_session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_append_first_chunk_uses_index_zero():
    """Empty doc → chunk_index 0 (COALESCE(MAX, -1) + 1)."""
    db = DatabaseService.__new__(DatabaseService)

    parent = MagicMock()
    parent.id = 42
    parent.tenant_id = 7
    parent.storage_path = "gs://bucket/doc.pdf"
    parent.storage_url = None

    max_row = MagicMock()
    max_row.max_idx = -1  # no existing chunks

    inserted = MagicMock()
    inserted.id = 1001
    inserted.document_id = "doc-1"
    inserted.content = "hello"
    inserted.chunk_index = 0
    inserted.token_count = int(math.ceil(max(1 * 1.3, 5 / 4)))
    inserted.metadata = None
    inserted.content_hash = hashlib.sha256(b"hello").hexdigest()
    inserted.source_uri = "gs://bucket/doc.pdf"
    inserted.ingested_at = datetime(2026, 1, 1, tzinfo=timezone.utc)

    parent_result = MagicMock()
    parent_result.fetchone = MagicMock(return_value=parent)
    max_result = MagicMock()
    max_result.fetchone = MagicMock(return_value=max_row)
    insert_result = MagicMock()
    insert_result.fetchone = MagicMock(return_value=inserted)
    update_result = MagicMock()

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(
        side_effect=[parent_result, max_result, insert_result, update_result, update_result]
    )
    mock_session.commit = AsyncMock()
    db.session = MagicMock(return_value=_session_ctx(mock_session))

    result = await db.append_document_chunk(
        document_id="doc-1", workspace_id="ws-1", content="hello"
    )

    assert result is not None
    assert result.chunk_index == 0
    assert result.content == "hello"
    assert result.token_count == estimate_expected("hello")
    assert result.metadata["content_hash"] == hashlib.sha256(b"hello").hexdigest()

    # INSERT params carry computed hash/tokens and next index 0
    insert_call = mock_session.execute.await_args_list[2]
    insert_params = insert_call.args[1]
    assert insert_params["chunk_index"] == 0
    assert insert_params["content_hash"] == hashlib.sha256(b"hello").hexdigest()
    assert insert_params["token_count"] == estimate_expected("hello")
    assert insert_params["processed_document_id"] == 42


@pytest.mark.asyncio
async def test_append_uses_max_plus_one():
    """Existing chunks at 0 and 2 (gap) → append at 3, not dense re-index."""
    db = DatabaseService.__new__(DatabaseService)

    parent = MagicMock()
    parent.id = 42
    parent.tenant_id = None
    parent.storage_path = None
    parent.storage_url = "https://example/doc"

    max_row = MagicMock()
    max_row.max_idx = 2

    inserted = MagicMock()
    inserted.id = 1002
    inserted.document_id = "doc-1"
    inserted.content = "tail"
    inserted.chunk_index = 3
    inserted.token_count = estimate_expected("tail")
    inserted.metadata = None
    inserted.content_hash = hashlib.sha256(b"tail").hexdigest()
    inserted.source_uri = "https://example/doc"
    inserted.ingested_at = datetime(2026, 1, 1, tzinfo=timezone.utc)

    parent_result = MagicMock()
    parent_result.fetchone = MagicMock(return_value=parent)
    max_result = MagicMock()
    max_result.fetchone = MagicMock(return_value=max_row)
    insert_result = MagicMock()
    insert_result.fetchone = MagicMock(return_value=inserted)

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(
        side_effect=[parent_result, max_result, insert_result, MagicMock(), MagicMock()]
    )
    mock_session.commit = AsyncMock()
    db.session = MagicMock(return_value=_session_ctx(mock_session))

    result = await db.append_document_chunk(
        document_id="doc-1", workspace_id="ws-1", content="tail"
    )

    assert result is not None
    assert result.chunk_index == 3
    insert_params = mock_session.execute.await_args_list[2].args[1]
    assert insert_params["chunk_index"] == 3


@pytest.mark.asyncio
async def test_append_parent_lookup_is_workspace_scoped():
    db = DatabaseService.__new__(DatabaseService)

    mock_result = MagicMock()
    mock_result.fetchone = MagicMock(return_value=None)
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)
    db.session = MagicMock(return_value=_session_ctx(mock_session))

    await db.append_document_chunk(document_id="doc-1", workspace_id="ws-attacker", content="x")

    query_text = str(mock_session.execute.await_args.args[0])
    params = mock_session.execute.await_args.args[1]
    assert "workspace_id" in query_text
    assert "FOR UPDATE" in query_text.upper().replace("\n", " ")
    assert params["workspace_id"] == "ws-attacker"
    assert params["document_id"] == "doc-1"


def estimate_expected(text: str) -> int:
    if not text:
        return 0
    return int(math.ceil(max(len(text.split()) * 1.3, len(text) / 4)))
