"""Unit tests for SearchService single-chunk vector upsert/delete (#133)."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.services.search import (
    SearchService,
    _get_user_tenant_name,
    _get_workspace_collection_name,
    chunk_vector_uuid,
)

WS = "ws-1"
USER = "user-1"
DOC = "doc-1"


def test_chunk_vector_uuid_is_deterministic():
    a = chunk_vector_uuid(WS, USER, DOC, 3)
    b = chunk_vector_uuid(WS, USER, DOC, 3)
    assert a == b
    assert a == str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{WS}:{USER}:{DOC}:3"))


class TestUpsertChunkVector:
    def _service(self):
        return SearchService(database=AsyncMock(), weaviate_url="http://weaviate:8080")

    @pytest.mark.asyncio
    async def test_create_posts_object_with_vector_and_tenant(self):
        service = self._service()
        response = MagicMock()
        response.status_code = 200
        response.text = "{}"
        client = AsyncMock()
        client.request = AsyncMock(return_value=response)
        service._client = client

        with patch("src.services.embedder.embed_query", return_value=(0.1, 0.2, 0.3)):
            await service.upsert_chunk_vector(
                workspace_id=WS,
                user_id=USER,
                document_id=DOC,
                chunk_index=0,
                content="hello",
                content_hash="abc",
                original_filename="f.pdf",
                content_type="application/pdf",
                source_uri="s3://x",
                create=True,
            )

        call = client.request.call_args
        assert call[0][0] == "POST"
        assert call[0][1] == "/v1/objects"
        body = call.kwargs["json"]
        assert body["class"] == _get_workspace_collection_name(WS)
        assert body["tenant"] == _get_user_tenant_name(USER)
        assert body["id"] == chunk_vector_uuid(WS, USER, DOC, 0)
        assert body["vector"] == [0.1, 0.2, 0.3]
        assert body["properties"]["content"] == "hello"
        assert body["properties"]["chunk_index"] == 0
        assert body["properties"]["content_hash"] == "abc"

    @pytest.mark.asyncio
    async def test_update_patches_with_new_vector(self):
        service = self._service()
        response = MagicMock()
        response.status_code = 200
        response.text = "{}"
        client = AsyncMock()
        client.request = AsyncMock(return_value=response)
        service._client = client

        with patch("src.services.embedder.embed_query", return_value=(1.0, 0.0)):
            await service.upsert_chunk_vector(
                workspace_id=WS,
                user_id=USER,
                document_id=DOC,
                chunk_index=2,
                content="edited",
                content_hash="def",
                create=False,
            )

        call = client.request.call_args
        assert call[0][0] == "PATCH"
        expected_uuid = chunk_vector_uuid(WS, USER, DOC, 2)
        coll = _get_workspace_collection_name(WS)
        assert call[0][1] == f"/v1/objects/{coll}/{expected_uuid}"
        assert call.kwargs["params"] == {"tenant": _get_user_tenant_name(USER)}
        body = call.kwargs["json"]
        assert body["vector"] == [1.0, 0.0]
        assert body["properties"]["content"] == "edited"
        assert "content_hash" in body["properties"]
        assert "ingested_at" in body["properties"]

    @pytest.mark.asyncio
    async def test_weaviate_error_raises(self):
        service = self._service()
        response = MagicMock()
        response.status_code = 500
        response.text = "boom"
        client = AsyncMock()
        client.request = AsyncMock(return_value=response)
        service._client = client

        with (
            patch("src.services.embedder.embed_query", return_value=(0.0,)),
            pytest.raises(RuntimeError, match="Weaviate"),
        ):
            await service.upsert_chunk_vector(
                workspace_id=WS,
                user_id=USER,
                document_id=DOC,
                chunk_index=0,
                content="x",
                content_hash="h",
                create=True,
            )


class TestDeleteChunkVector:
    def _service(self):
        return SearchService(database=AsyncMock(), weaviate_url="http://weaviate:8080")

    @pytest.mark.asyncio
    async def test_deletes_by_deterministic_uuid(self):
        service = self._service()
        response = MagicMock()
        response.status_code = 204
        response.text = ""
        client = AsyncMock()
        client.request = AsyncMock(return_value=response)
        service._client = client

        await service.delete_chunk_vector(WS, USER, DOC, 1)

        call = client.request.call_args
        assert call[0][0] == "DELETE"
        coll = _get_workspace_collection_name(WS)
        uid = chunk_vector_uuid(WS, USER, DOC, 1)
        assert call[0][1] == f"/v1/objects/{coll}/{uid}"
        assert call.kwargs["params"] == {"tenant": _get_user_tenant_name(USER)}

    @pytest.mark.asyncio
    async def test_missing_object_is_already_clean(self):
        service = self._service()
        response = MagicMock()
        response.status_code = 404
        response.text = "not found"
        client = AsyncMock()
        client.request = AsyncMock(return_value=response)
        service._client = client

        # Does not raise
        await service.delete_chunk_vector(WS, USER, DOC, 99)

    @pytest.mark.asyncio
    async def test_other_error_raises(self):
        service = self._service()
        response = MagicMock()
        response.status_code = 500
        response.text = "down"
        client = AsyncMock()
        client.request = AsyncMock(return_value=response)
        service._client = client

        with pytest.raises(RuntimeError):
            await service.delete_chunk_vector(WS, USER, DOC, 0)
