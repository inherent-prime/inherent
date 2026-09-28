"""Chunk reuse detection tests (inherent#394).

Unit-level, everything mocked (no live Weaviate/Postgres needed): schema
properties, the deterministic-UUID helper, the near-object/patch Weaviate
methods, and the detect_and_record_chunk_reuse orchestration (threshold,
same-document exclusion, idempotency passthrough, cost bound / min chunk
size, best-effort failure handling).
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from weaviate.classes.config import DataType

from src.config.settings import Settings
from src.models.document import DocumentChunk
from src.services.reuse_detection import (
    DEFAULT_MIN_CHUNK_CHARS,
    detect_and_record_chunk_reuse,
)
from src.services.weaviate import WeaviateService


def _settings() -> Settings:
    return Settings(DATABASE_URL="postgresql://x/y", WEAVIATE_URL="http://localhost:8080")


# ---------------------------------------------------------------------------
# Schema (additive, backward-compatible -- same pattern as #390's `tags`)
# ---------------------------------------------------------------------------


def test_reuse_count_property_is_int():
    service = WeaviateService(_settings())
    props = {p.name: p for p in service._get_chunk_properties()}
    assert "reuse_count" in props
    assert props["reuse_count"].dataType == DataType.INT


def test_last_reused_at_property_is_date():
    service = WeaviateService(_settings())
    props = {p.name: p for p in service._get_chunk_properties()}
    assert "last_reused_at" in props
    assert props["last_reused_at"].dataType == DataType.DATE


def test_reconcile_adds_reuse_properties_to_existing_collection_missing_them():
    """A collection created before #394 has every other property but not
    reuse_count/last_reused_at -- reconcile must add just those, additively."""
    service = WeaviateService(_settings())

    existing_props = [
        p
        for p in service._get_chunk_properties()
        if p.name not in ("reuse_count", "last_reused_at")
    ]
    mock_collection = MagicMock()
    mock_collection.config.get.return_value.properties = existing_props

    mock_client = MagicMock()
    mock_client.collections.get.return_value = mock_collection
    service.client = mock_client

    service._reconcile_collection_properties("Workspace_TEST")

    added_names = {call.args[0].name for call in mock_collection.config.add_property.call_args_list}
    assert added_names == {"reuse_count", "last_reused_at"}


# ---------------------------------------------------------------------------
# Deterministic UUID helper
# ---------------------------------------------------------------------------


def test_chunk_object_uuid_matches_manual_formula():
    expected = uuid.uuid5(uuid.NAMESPACE_DNS, "ws1:u1:doc1:0")
    assert WeaviateService.chunk_object_uuid("ws1", "u1", "doc1", 0) == expected


def test_chunk_object_uuid_is_deterministic():
    a = WeaviateService.chunk_object_uuid("ws1", "u1", "doc1", 3)
    b = WeaviateService.chunk_object_uuid("ws1", "u1", "doc1", 3)
    assert a == b


def test_chunk_object_uuid_differs_by_chunk_index():
    a = WeaviateService.chunk_object_uuid("ws1", "u1", "doc1", 0)
    b = WeaviateService.chunk_object_uuid("ws1", "u1", "doc1", 1)
    assert a != b


# ---------------------------------------------------------------------------
# WeaviateService.find_similar_chunks / set_chunk_reuse_count
# ---------------------------------------------------------------------------


def _mock_object(document_id: str, chunk_index: int, content: str, certainty: float, obj_uuid=None):
    obj = MagicMock()
    obj.uuid = obj_uuid or uuid.uuid4()
    obj.metadata.certainty = certainty
    obj.properties = {
        "document_id": document_id,
        "chunk_index": chunk_index,
        "content": content,
    }
    return obj


@pytest.mark.asyncio
async def test_find_similar_chunks_maps_weaviate_response():
    service = WeaviateService(_settings())
    mock_tenant_collection = MagicMock()
    mock_tenant_collection.query.near_object.return_value = MagicMock(
        objects=[_mock_object("doc_old", 2, "reused text", 0.95)]
    )
    mock_collection = MagicMock()
    mock_collection.with_tenant.return_value = mock_tenant_collection
    mock_client = MagicMock()
    mock_client.collections.get.return_value = mock_collection
    service.client = mock_client

    results = await service.find_similar_chunks(
        workspace_id="ws1",
        user_id="u1",
        chunk_uuid=uuid.uuid4(),
        exclude_document_id="doc_new",
        certainty_threshold=0.92,
        top_k=5,
    )

    assert results == [
        {
            "uuid": results[0]["uuid"],
            "certainty": 0.95,
            "document_id": "doc_old",
            "chunk_index": 2,
            "content": "reused text",
        }
    ]
    # certainty/limit are pushed server-side, not filtered client-side.
    _, kwargs = mock_tenant_collection.query.near_object.call_args
    assert kwargs["certainty"] == 0.92
    assert kwargs["limit"] == 5


@pytest.mark.asyncio
async def test_find_similar_chunks_no_client_returns_empty():
    service = WeaviateService(_settings())
    service.client = None
    results = await service.find_similar_chunks(
        workspace_id="ws1",
        user_id="u1",
        chunk_uuid=uuid.uuid4(),
        exclude_document_id="doc_new",
        certainty_threshold=0.92,
        top_k=5,
    )
    assert results == []


@pytest.mark.asyncio
async def test_set_chunk_reuse_count_patches_properties():
    service = WeaviateService(_settings())
    mock_tenant_collection = MagicMock()
    mock_collection = MagicMock()
    mock_collection.with_tenant.return_value = mock_tenant_collection
    mock_client = MagicMock()
    mock_client.collections.get.return_value = mock_collection
    service.client = mock_client

    chunk_uuid = uuid.uuid4()
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    await service.set_chunk_reuse_count(
        workspace_id="ws1", user_id="u1", chunk_uuid=chunk_uuid, reuse_count=3, last_reused_at=now
    )

    mock_tenant_collection.data.update.assert_called_once_with(
        uuid=chunk_uuid, properties={"reuse_count": 3, "last_reused_at": now}
    )


# ---------------------------------------------------------------------------
# detect_and_record_chunk_reuse orchestration
# ---------------------------------------------------------------------------


def _chunk(content: str, index: int = 0) -> DocumentChunk:
    return DocumentChunk(
        document_id="doc_new",
        content=content,
        chunk_index=index,
        start_char=0,
        end_char=len(content),
    )


@pytest.mark.asyncio
async def test_tiny_chunks_are_skipped():
    """Below min_chunk_chars: no Weaviate lookup at all (cost bound)."""
    weaviate_service = MagicMock()
    weaviate_service.chunk_object_uuid = MagicMock(return_value=uuid.uuid4())
    weaviate_service.find_similar_chunks = AsyncMock()
    db_service = MagicMock()

    tiny = _chunk("short")
    assert len(tiny.content) < DEFAULT_MIN_CHUNK_CHARS

    recorded = await detect_and_record_chunk_reuse(
        weaviate_service=weaviate_service,
        db_service=db_service,
        workspace_id="ws1",
        user_id="u1",
        document_id="doc_new",
        chunks=[tiny],
    )

    assert recorded == 0
    weaviate_service.find_similar_chunks.assert_not_called()


@pytest.mark.asyncio
async def test_no_candidates_records_nothing():
    weaviate_service = MagicMock()
    weaviate_service.chunk_object_uuid = MagicMock(return_value=uuid.uuid4())
    weaviate_service.find_similar_chunks = AsyncMock(return_value=[])
    db_service = MagicMock()
    db_service.get_chunk_for_reuse = AsyncMock()
    db_service.record_chunk_reuse = AsyncMock()

    content = "x" * 100
    recorded = await detect_and_record_chunk_reuse(
        weaviate_service=weaviate_service,
        db_service=db_service,
        workspace_id="ws1",
        user_id="u1",
        document_id="doc_new",
        chunks=[_chunk(content)],
    )

    assert recorded == 0
    db_service.get_chunk_for_reuse.assert_not_called()


@pytest.mark.asyncio
async def test_cosine_threshold_is_converted_to_weaviate_certainty():
    """Weaviate certainty is (1 + cosine) / 2; passing cosine 0.92 raw would
    enforce cosine 0.84 instead, silently loosening the match threshold."""
    weaviate_service = MagicMock()
    weaviate_service.chunk_object_uuid = MagicMock(return_value=uuid.uuid4())
    weaviate_service.find_similar_chunks = AsyncMock(return_value=[])
    db_service = MagicMock()

    await detect_and_record_chunk_reuse(
        weaviate_service=weaviate_service,
        db_service=db_service,
        workspace_id="ws1",
        user_id="u1",
        document_id="doc_new",
        chunks=[_chunk("x" * 100)],
        vector_similarity_threshold=0.92,
    )

    _, kwargs = weaviate_service.find_similar_chunks.call_args
    assert kwargs["certainty_threshold"] == pytest.approx(0.96)


@pytest.mark.asyncio
async def test_matched_candidate_is_recorded_and_mirrored_to_weaviate():
    content = "reused content " * 5
    candidate_uuid = uuid.uuid4()
    weaviate_service = MagicMock()
    weaviate_service.chunk_object_uuid = MagicMock(return_value=uuid.uuid4())
    weaviate_service.find_similar_chunks = AsyncMock(
        return_value=[
            {
                "uuid": candidate_uuid,
                "certainty": 0.95,
                "document_id": "doc_old",
                "chunk_index": 2,
                "content": content,  # identical text -> passes the text-similarity confirm
            }
        ]
    )
    weaviate_service.set_chunk_reuse_count = AsyncMock()

    db_service = MagicMock()
    db_service.get_chunk_for_reuse = AsyncMock(return_value={"id": 42, "reuse_count": 0})
    db_service.record_chunk_reuse = AsyncMock(return_value=1)

    recorded = await detect_and_record_chunk_reuse(
        weaviate_service=weaviate_service,
        db_service=db_service,
        workspace_id="ws1",
        user_id="u1",
        document_id="doc_new",
        chunks=[_chunk(content)],
    )

    assert recorded == 1
    db_service.record_chunk_reuse.assert_awaited_once()
    _, kwargs = db_service.record_chunk_reuse.call_args
    assert kwargs["source_chunk_id"] == 42
    assert kwargs["reusing_document_id"] == "doc_new"
    weaviate_service.set_chunk_reuse_count.assert_awaited_once()
    _, patch_kwargs = weaviate_service.set_chunk_reuse_count.call_args
    assert patch_kwargs["reuse_count"] == 1
    assert patch_kwargs["chunk_uuid"] == candidate_uuid


@pytest.mark.asyncio
async def test_same_document_candidate_is_never_counted():
    """Belt-and-braces exclusion: even if Weaviate somehow returned a match
    from the SAME document (e.g. a future filter bug), it must never be
    recorded as reuse."""
    content = "x" * 100
    weaviate_service = MagicMock()
    weaviate_service.chunk_object_uuid = MagicMock(return_value=uuid.uuid4())
    weaviate_service.find_similar_chunks = AsyncMock(
        return_value=[
            {
                "uuid": uuid.uuid4(),
                "certainty": 0.99,
                "document_id": "doc_new",
                "chunk_index": 1,
                "content": content,
            }
        ]
    )
    db_service = MagicMock()
    db_service.get_chunk_for_reuse = AsyncMock()
    db_service.record_chunk_reuse = AsyncMock()

    recorded = await detect_and_record_chunk_reuse(
        weaviate_service=weaviate_service,
        db_service=db_service,
        workspace_id="ws1",
        user_id="u1",
        document_id="doc_new",
        chunks=[_chunk(content)],
    )

    assert recorded == 0
    db_service.get_chunk_for_reuse.assert_not_called()


@pytest.mark.asyncio
async def test_idempotent_rerun_records_nothing_new():
    """record_chunk_reuse returning None (already-recorded triple, e.g. a
    re-ingest of an unchanged document) must not be double counted."""
    content = "reused content " * 5
    weaviate_service = MagicMock()
    weaviate_service.chunk_object_uuid = MagicMock(return_value=uuid.uuid4())
    weaviate_service.find_similar_chunks = AsyncMock(
        return_value=[
            {
                "uuid": uuid.uuid4(),
                "certainty": 0.95,
                "document_id": "doc_old",
                "chunk_index": 2,
                "content": content,
            }
        ]
    )
    weaviate_service.set_chunk_reuse_count = AsyncMock()
    db_service = MagicMock()
    db_service.get_chunk_for_reuse = AsyncMock(return_value={"id": 42, "reuse_count": 1})
    db_service.record_chunk_reuse = AsyncMock(return_value=None)  # already recorded

    recorded = await detect_and_record_chunk_reuse(
        weaviate_service=weaviate_service,
        db_service=db_service,
        workspace_id="ws1",
        user_id="u1",
        document_id="doc_new",
        chunks=[_chunk(content)],
    )

    assert recorded == 0
    weaviate_service.set_chunk_reuse_count.assert_not_called()


@pytest.mark.asyncio
async def test_textually_unrelated_candidate_is_skipped():
    """A vector near-duplicate that reads completely differently must not
    count as reuse -- the cheap text-similarity confirm guards this."""
    content = "alpha beta gamma delta epsilon zeta eta theta iota kappa"
    unrelated = "zzz yyy xxx www vvv uuu ttt sss rrr qqq"
    weaviate_service = MagicMock()
    weaviate_service.chunk_object_uuid = MagicMock(return_value=uuid.uuid4())
    weaviate_service.find_similar_chunks = AsyncMock(
        return_value=[
            {
                "uuid": uuid.uuid4(),
                "certainty": 0.93,
                "document_id": "doc_old",
                "chunk_index": 2,
                "content": unrelated,
            }
        ]
    )
    db_service = MagicMock()
    db_service.get_chunk_for_reuse = AsyncMock()

    recorded = await detect_and_record_chunk_reuse(
        weaviate_service=weaviate_service,
        db_service=db_service,
        workspace_id="ws1",
        user_id="u1",
        document_id="doc_new",
        chunks=[_chunk(content)],
    )

    assert recorded == 0
    db_service.get_chunk_for_reuse.assert_not_called()


@pytest.mark.asyncio
async def test_text_similarity_check_disabled_when_threshold_zero():
    content = "alpha beta gamma delta epsilon zeta eta theta iota kappa"
    unrelated = "zzz yyy xxx www vvv uuu ttt sss rrr qqq"
    weaviate_service = MagicMock()
    weaviate_service.chunk_object_uuid = MagicMock(return_value=uuid.uuid4())
    weaviate_service.find_similar_chunks = AsyncMock(
        return_value=[
            {
                "uuid": uuid.uuid4(),
                "certainty": 0.93,
                "document_id": "doc_old",
                "chunk_index": 2,
                "content": unrelated,
            }
        ]
    )
    weaviate_service.set_chunk_reuse_count = AsyncMock()
    db_service = MagicMock()
    db_service.get_chunk_for_reuse = AsyncMock(return_value={"id": 1, "reuse_count": 0})
    db_service.record_chunk_reuse = AsyncMock(return_value=1)

    recorded = await detect_and_record_chunk_reuse(
        weaviate_service=weaviate_service,
        db_service=db_service,
        workspace_id="ws1",
        user_id="u1",
        document_id="doc_new",
        chunks=[_chunk(content)],
        text_similarity_threshold=0.0,
    )

    assert recorded == 1


@pytest.mark.asyncio
async def test_lookup_failure_is_best_effort_and_does_not_raise():
    weaviate_service = MagicMock()
    weaviate_service.chunk_object_uuid = MagicMock(return_value=uuid.uuid4())
    weaviate_service.find_similar_chunks = AsyncMock(side_effect=RuntimeError("weaviate down"))
    db_service = MagicMock()

    recorded = await detect_and_record_chunk_reuse(
        weaviate_service=weaviate_service,
        db_service=db_service,
        workspace_id="ws1",
        user_id="u1",
        document_id="doc_new",
        chunks=[_chunk("x" * 100)],
    )

    assert recorded == 0  # never raises -- ingestion must not fail (#394)


@pytest.mark.asyncio
async def test_record_failure_is_best_effort_and_continues_other_chunks():
    content_a = "reused content a " * 5
    content_b = "reused content b " * 5
    weaviate_service = MagicMock()
    weaviate_service.chunk_object_uuid = MagicMock(return_value=uuid.uuid4())
    weaviate_service.find_similar_chunks = AsyncMock(
        side_effect=[
            [
                {
                    "uuid": uuid.uuid4(),
                    "certainty": 0.95,
                    "document_id": "doc_old",
                    "chunk_index": 2,
                    "content": content_a,
                }
            ],
            [
                {
                    "uuid": uuid.uuid4(),
                    "certainty": 0.95,
                    "document_id": "doc_old",
                    "chunk_index": 3,
                    "content": content_b,
                }
            ],
        ]
    )
    weaviate_service.set_chunk_reuse_count = AsyncMock()

    db_service = MagicMock()
    # First chunk's DB lookup blows up; second succeeds.
    db_service.get_chunk_for_reuse = AsyncMock(
        side_effect=[RuntimeError("db hiccup"), {"id": 99, "reuse_count": 0}]
    )
    db_service.record_chunk_reuse = AsyncMock(return_value=1)

    recorded = await detect_and_record_chunk_reuse(
        weaviate_service=weaviate_service,
        db_service=db_service,
        workspace_id="ws1",
        user_id="u1",
        document_id="doc_new",
        chunks=[_chunk(content_a, index=0), _chunk(content_b, index=1)],
    )

    assert recorded == 1  # first chunk's failure didn't stop the second
