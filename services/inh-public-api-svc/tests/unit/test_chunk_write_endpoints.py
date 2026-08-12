"""Unit tests for REST chunk Create / Update / Delete (#133 Sprint 2)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from src.main import create_app
from src.models.api_key import APIKeyInfo
from src.models.document import DocumentChunk
from src.services.auth import (
    ResolvedAuth,
    get_api_key_info,
    get_write_permission,
    resolve_workspace_write,
)
from src.services.chunk_writes import ChunkDeleteOutcome, ChunkWriteOutcome
from src.services.database import get_database


@pytest.fixture
def write_key():
    return APIKeyInfo(
        key_id="write-key",
        user_id="test-user-id",
        workspace_id="test-workspace-id",
        permissions=["read", "write"],
        rate_limit=100,
        expires_at=None,
        status="active",
    )


@pytest.fixture
def write_auth(write_key):
    return ResolvedAuth(key_info=write_key, workspace_id=write_key.workspace_id)


@pytest.fixture
def mock_db():
    return AsyncMock()


@pytest.fixture
def app(write_key, write_auth, mock_db):
    application = create_app()
    application.dependency_overrides[get_api_key_info] = lambda: write_key
    application.dependency_overrides[get_write_permission] = lambda: write_key
    application.dependency_overrides[resolve_workspace_write] = lambda: write_auth
    application.dependency_overrides[get_database] = lambda: mock_db
    yield application
    application.dependency_overrides.clear()


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


def _chunk(**kwargs) -> DocumentChunk:
    defaults = dict(
        id="10",
        document_id="doc-001",
        content="hello",
        chunk_index=0,
        token_count=1,
        metadata={"content_hash": "h"},
    )
    defaults.update(kwargs)
    return DocumentChunk(**defaults)


class TestCreateChunkRest:
    async def test_create_returns_201(self, client):
        chunk = _chunk(chunk_index=5, content="appended")
        with patch(
            "src.api.v1.chunks.create_chunk_everywhere",
            AsyncMock(return_value=ChunkWriteOutcome(found=True, chunk=chunk)),
        ) as create:
            resp = await client.post(
                "/v1/chunks/doc-001",
                headers={"X-API-Key": "ink_test"},
                json={"content": "appended"},
            )
        assert resp.status_code == 201
        assert resp.json()["chunk_index"] == 5
        assert resp.json()["content"] == "appended"
        create.assert_awaited_once()

    async def test_create_document_not_found(self, client):
        with patch(
            "src.api.v1.chunks.create_chunk_everywhere",
            AsyncMock(return_value=ChunkWriteOutcome(found=False)),
        ):
            resp = await client.post(
                "/v1/chunks/missing",
                headers={"X-API-Key": "ink_test"},
                json={"content": "x"},
            )
        assert resp.status_code == 404

    async def test_create_vector_failure_returns_503(self, client):
        with patch(
            "src.api.v1.chunks.create_chunk_everywhere",
            AsyncMock(side_effect=RuntimeError("weaviate down")),
        ):
            resp = await client.post(
                "/v1/chunks/doc-001",
                headers={"X-API-Key": "ink_test"},
                json={"content": "x"},
            )
        assert resp.status_code == 503

    async def test_create_requires_write_permission(self, mock_db):
        """Read-only key → 403 via real get_write_permission (no resolve override)."""
        read_only = APIKeyInfo(
            key_id="ro",
            user_id="u",
            workspace_id="test-workspace-id",
            permissions=["read"],
            rate_limit=100,
            expires_at=None,
            status="active",
        )
        application = create_app()
        application.dependency_overrides[get_api_key_info] = lambda: read_only
        application.dependency_overrides[get_database] = lambda: mock_db
        transport = ASGITransport(app=application)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.post(
                "/v1/chunks/doc-001",
                headers={"X-API-Key": "ink_test"},
                json={"content": "x"},
            )
        application.dependency_overrides.clear()
        assert resp.status_code == 403


class TestUpdateChunkRest:
    async def test_update_returns_200(self, client):
        chunk = _chunk(chunk_index=1, content="edited")
        with patch(
            "src.api.v1.chunks.update_chunk_everywhere",
            AsyncMock(return_value=ChunkWriteOutcome(found=True, chunk=chunk)),
        ):
            resp = await client.patch(
                "/v1/chunks/doc-001/1",
                headers={"X-API-Key": "ink_test"},
                json={"content": "edited"},
            )
        assert resp.status_code == 200
        assert resp.json()["content"] == "edited"

    async def test_update_not_found(self, client):
        with patch(
            "src.api.v1.chunks.update_chunk_everywhere",
            AsyncMock(return_value=ChunkWriteOutcome(found=False)),
        ):
            resp = await client.patch(
                "/v1/chunks/doc-001/99",
                headers={"X-API-Key": "ink_test"},
                json={"content": "x"},
            )
        assert resp.status_code == 404

    async def test_update_vector_failure_returns_503(self, client):
        with patch(
            "src.api.v1.chunks.update_chunk_everywhere",
            AsyncMock(side_effect=RuntimeError("embed fail")),
        ):
            resp = await client.patch(
                "/v1/chunks/doc-001/0",
                headers={"X-API-Key": "ink_test"},
                json={"content": "x"},
            )
        assert resp.status_code == 503


class TestDeleteChunkRest:
    async def test_delete_returns_204(self, client):
        with patch(
            "src.api.v1.chunks.delete_chunk_everywhere",
            AsyncMock(return_value=ChunkDeleteOutcome(found=True)),
        ):
            resp = await client.delete(
                "/v1/chunks/doc-001/0",
                headers={"X-API-Key": "ink_test"},
            )
        assert resp.status_code == 204

    async def test_delete_not_found(self, client):
        with patch(
            "src.api.v1.chunks.delete_chunk_everywhere",
            AsyncMock(return_value=ChunkDeleteOutcome(found=False)),
        ):
            resp = await client.delete(
                "/v1/chunks/doc-001/99",
                headers={"X-API-Key": "ink_test"},
            )
        assert resp.status_code == 404

    async def test_delete_vector_failure_returns_503(self, client):
        with patch(
            "src.api.v1.chunks.delete_chunk_everywhere",
            AsyncMock(side_effect=RuntimeError("weaviate down")),
        ):
            resp = await client.delete(
                "/v1/chunks/doc-001/0",
                headers={"X-API-Key": "ink_test"},
            )
        assert resp.status_code == 503
