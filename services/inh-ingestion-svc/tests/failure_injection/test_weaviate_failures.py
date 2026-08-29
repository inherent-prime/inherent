"""Failure-injection: Weaviate behavior under client errors.

Two contracts are asserted:

1. ``store_chunks_with_tenant`` MUST raise when the Weaviate client errors,
   so the storage step fails loudly and is retried (no half-indexed docs
   reported as success).
2. ``delete_document_chunks_graceful`` MUST NOT raise — it returns
   ``(False, 0)`` when Weaviate is unavailable, so cleanup degrades
   gracefully and the caller can report partial success / re-attempt later.

Mocking is at the weaviate client boundary plus the embedder; no live
Weaviate or TEI is required.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.config.settings import Settings
from src.models.document import DocumentChunk
from src.services.weaviate import WeaviateService

pytestmark = pytest.mark.failure_injection


@pytest.fixture
def mock_settings():
    settings = MagicMock(spec=Settings)
    settings.weaviate_url = "http://example.invalid:8080"
    settings.weaviate_api_key = ""
    # #311 PR #314 review finding 3: _check_or_stamp_collection_identity now
    # reads this on every call.
    settings.embedding_adopt_unstamped_collections = False
    return settings


@pytest.fixture
def chunks():
    return [
        DocumentChunk(
            document_id="doc-1",
            content="first chunk",
            chunk_index=0,
            start_char=0,
            end_char=11,
            metadata={},
        )
    ]


async def test_store_chunks_with_tenant_propagates_client_error(mock_settings, chunks):
    """A Weaviate batch error during storage must propagate (be retried)."""
    service = WeaviateService(mock_settings)
    service.client = MagicMock()

    # Pretend collection/tenant already exist so we reach the storage path.
    service._collection_cache.add("Workspace_ws1")
    service._tenant_cache["Workspace_ws1"] = {"User_user1"}

    # The batch context manager blows up when entered (simulates Weaviate down).
    collection = MagicMock()
    tenant_collection = MagicMock()
    tenant_collection.batch.dynamic.side_effect = Exception("weaviate unavailable")
    collection.with_tenant.return_value = tenant_collection
    # #311 PR #314 review finding 3: this test isn't about the identity
    # guard -- resolve the mocked collection as fresh/empty (no tenants yet)
    # so it adopts silently instead of tripping the opt-in gate.
    collection.tenants.get.return_value = {}
    service.client.collections.get.return_value = collection

    with patch(
        "src.services.embedder.embed_texts",
        return_value=[[0.0, 0.1, 0.2]],
    ):
        with pytest.raises(Exception, match="weaviate unavailable"):
            await service.store_chunks_with_tenant(
                chunks=chunks,
                document_id="doc-1",
                workspace_id="ws1",
                user_id="user1",
                original_filename="report.pdf",
                content_type="application/pdf",
            )


async def test_delete_graceful_returns_false_when_client_none(mock_settings):
    """No client (Weaviate disconnected): graceful delete returns (False, 0)."""
    service = WeaviateService(mock_settings)
    service.client = None

    ok, count = await service.delete_document_chunks_graceful(
        workspace_id="ws1",
        document_id="doc-1",
        user_id="user1",
    )

    assert ok is False
    assert count == 0


async def test_delete_graceful_returns_false_when_not_ready(mock_settings):
    """Client present but not ready: graceful delete returns (False, 0)."""
    service = WeaviateService(mock_settings)
    service.client = MagicMock()
    service.client.is_ready.return_value = False

    ok, count = await service.delete_document_chunks_graceful(
        workspace_id="ws1",
        document_id="doc-1",
        user_id="user1",
    )

    assert ok is False
    assert count == 0


async def test_delete_graceful_swallows_delete_error(mock_settings):
    """A delete_many error must be swallowed: returns (False, 0), never raises."""
    service = WeaviateService(mock_settings)
    service.client = MagicMock()
    service.client.is_ready.return_value = True

    collection = MagicMock()
    tenant_collection = MagicMock()
    tenant_collection.data.delete_many.side_effect = Exception("delete failed")
    collection.with_tenant.return_value = tenant_collection
    service.client.collections.get.return_value = collection

    ok, count = await service.delete_document_chunks_graceful(
        workspace_id="ws1",
        document_id="doc-1",
        user_id="user1",
    )

    assert ok is False
    assert count == 0


async def test_delete_with_tenant_propagates_error(mock_settings):
    """The non-graceful delete variant MUST raise (contrast with graceful)."""
    service = WeaviateService(mock_settings)
    service.client = MagicMock()

    collection = MagicMock()
    tenant_collection = MagicMock()
    tenant_collection.data.delete_many.side_effect = Exception("delete failed")
    collection.with_tenant.return_value = tenant_collection
    service.client.collections.get.return_value = collection

    with pytest.raises(Exception, match="delete failed"):
        await service.delete_document_chunks_with_tenant(
            document_id="doc-1",
            workspace_id="ws1",
            user_id="user1",
        )
