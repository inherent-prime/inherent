"""Weaviate batch store must surface per-object failures (#8).

The v4 client's batch collects per-object errors in ``failed_objects`` instead
of raising, so a partial failure (dimension mismatch, transient shard error)
would otherwise be reported as a full success — Postgres says N chunks,
Weaviate stored N-k, with no error. The store must raise so the activity
retries / dead-letters.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.models.document import DocumentChunk
from src.services.weaviate import WeaviateService


@pytest.fixture
def service():
    settings = MagicMock()
    settings.weaviate_url = "http://localhost:8080"
    svc = WeaviateService(settings)
    svc.client = MagicMock()
    svc.ensure_workspace_collection = AsyncMock(return_value="Workspace_x")
    svc.ensure_user_tenant = AsyncMock(return_value="User_y")
    return svc


def _wire_batch(svc, failed_objects):
    collection = MagicMock()
    svc.client.collections.get.return_value = collection
    tenant_collection = MagicMock()
    collection.with_tenant.return_value = tenant_collection
    batch = MagicMock()
    cm = MagicMock()
    cm.__enter__.return_value = batch
    cm.__exit__.return_value = False
    tenant_collection.batch.dynamic.return_value = cm
    tenant_collection.batch.failed_objects = failed_objects
    return tenant_collection


def _chunk(i: int) -> DocumentChunk:
    return DocumentChunk(
        document_id="d", content=f"chunk {i}", chunk_index=i, start_char=0, end_char=5
    )


@pytest.mark.asyncio
async def test_raises_on_partial_batch_failure(service):
    failure = MagicMock()
    failure.message = "vector dimension mismatch"
    _wire_batch(service, [failure])
    chunks = [_chunk(0), _chunk(1)]
    with patch("src.services.embedder.embed_texts", return_value=[[0.1] * 384] * 2):
        with pytest.raises(RuntimeError, match="batch store failed"):
            await service.store_chunks_with_tenant(
                chunks=chunks,
                document_id="d",
                workspace_id="ws",
                user_id="u",
                original_filename="f.txt",
                content_type="text/plain",
            )


@pytest.mark.asyncio
async def test_returns_count_when_no_failures(service):
    _wire_batch(service, [])
    chunks = [_chunk(0), _chunk(1), _chunk(2)]
    with patch("src.services.embedder.embed_texts", return_value=[[0.1] * 384] * 3):
        n = await service.store_chunks_with_tenant(
            chunks=chunks,
            document_id="d",
            workspace_id="ws",
            user_id="u",
            original_filename="f.txt",
            content_type="text/plain",
        )
    assert n == 3


@pytest.mark.asyncio
async def test_embedding_is_offloaded_to_thread(service):
    """The synchronous embed_texts call must be offloaded to a thread so it does
    not block the event loop during document store (#19)."""
    import src.services.weaviate as wv

    _wire_batch(service, [])
    chunks = [_chunk(0)]
    with patch.object(
        wv.asyncio, "to_thread", new=AsyncMock(return_value=[[0.1] * 384])
    ) as to_thread:
        await service.store_chunks_with_tenant(
            chunks=chunks,
            document_id="d",
            workspace_id="ws",
            user_id="u",
            original_filename="f.txt",
            content_type="text/plain",
        )
    to_thread.assert_awaited_once()
    assert to_thread.await_args.args[0].__name__ == "embed_texts"
