"""Weaviate `section_heading` property tests (inherent#390 / #392).

A vertical pack's numbered_sections chunker records each chunk's own heading
line (``ChunkData.section_heading``). It was persisted to Postgres chunk
metadata only, never to Weaviate -- but the public API's search reads chunks
from Weaviate, so a search result (and a pack's MCP profile tool, which
advertises "text, tags, section heading") never carried a heading. Found by the
live pilot-flow E2E. These tests pin the ingest half of the fix: the property
exists on the chunk schema (new collections), reconciliation adds it to an
existing collection, and the store writes it from chunk metadata.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from weaviate.classes.config import DataType

from src.config.settings import Settings
from src.models.document import DocumentChunk
from src.services.weaviate import WeaviateService


def _service() -> WeaviateService:
    return WeaviateService(
        Settings(DATABASE_URL="postgresql://x/y", WEAVIATE_URL="http://localhost:8080")
    )


def test_section_heading_property_is_text_and_not_bm25_indexed():
    """A display field: the heading text is already inside the chunk content."""
    props = {p.name: p for p in _service()._get_chunk_properties()}
    assert props["section_heading"].dataType == DataType.TEXT
    assert props["section_heading"].indexSearchable is False


def test_reconcile_adds_section_heading_to_a_collection_that_predates_it():
    service = _service()
    existing = [p for p in service._get_chunk_properties() if p.name != "section_heading"]
    collection = MagicMock()
    collection.config.get.return_value.properties = existing
    service.client = MagicMock()
    service.client.collections.get.return_value = collection

    service._reconcile_collection_properties("Workspace_TEST")

    added = [c.args[0].name for c in collection.config.add_property.call_args_list]
    assert added == ["section_heading"]


async def _stored_properties(chunk: DocumentChunk) -> dict:
    service = _service()
    service.client = MagicMock()
    service.ensure_workspace_collection = AsyncMock(return_value="Workspace_ws1")
    service.ensure_user_tenant = AsyncMock(return_value="User_u1")
    batch = MagicMock()
    tenant_collection = MagicMock()
    tenant_collection.batch.dynamic.return_value.__enter__.return_value = batch
    tenant_collection.batch.failed_objects = []
    service.client.collections.get.return_value.with_tenant.return_value = tenant_collection

    with patch(
        "src.services.embedder.embed_texts_with_progress",
        return_value=[[0.1] * 384],
    ):
        await service.store_chunks_with_tenant(
            [chunk], "doc1", "ws1", "u1", "file.docx", "application/octet-stream"
        )
    return batch.add_object.call_args.kwargs["properties"]


@pytest.mark.asyncio
async def test_store_writes_section_heading_from_chunk_metadata():
    chunk = DocumentChunk(
        document_id="doc1",
        content="1.1 Some clause text",
        chunk_index=0,
        start_char=0,
        end_char=20,
        metadata={"section_heading": "1.1 Some clause text"},
    )
    assert (await _stored_properties(chunk))["section_heading"] == "1.1 Some clause text"


@pytest.mark.asyncio
async def test_store_writes_empty_section_heading_for_an_ordinary_chunk():
    """Every non-pack chunk (the vast majority) gets a real empty TEXT value."""
    chunk = DocumentChunk(
        document_id="doc1", content="plain text", chunk_index=0, start_char=0, end_char=10
    )
    assert (await _stored_properties(chunk))["section_heading"] == ""
