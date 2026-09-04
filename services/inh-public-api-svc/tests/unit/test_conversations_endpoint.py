"""Unit tests for the conversation ingestion API endpoints (#306).

    POST   /v1/conversations/{external_id}/turns
    GET    /v1/conversations/{external_id}
    DELETE /v1/conversations/{external_id}

Mirrors tests/unit/test_upload_document.py / test_documents_endpoint.py's
dependency-override pattern.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from src.main import create_app
from src.models.api_key import APIKeyInfo
from src.services import conversation_intake
from src.services.auth import (
    ResolvedAuth,
    get_api_key_info,
    get_read_permission,
    get_write_permission,
    resolve_workspace_read,
    resolve_workspace_write,
)
from src.services.database import get_database


@pytest.fixture
def write_key():
    return APIKeyInfo(
        key_id="test-key-write",
        user_id="test-user-id",
        workspace_id="test-workspace-id",
        permissions=["read", "search", "write"],
        rate_limit=100,
        expires_at=None,
        status="active",
    )


@pytest.fixture
def mock_resolved_auth_write(write_key):
    return ResolvedAuth(key_info=write_key, workspace_id=write_key.workspace_id)


@pytest.fixture
def mock_resolved_auth_read(write_key):
    return ResolvedAuth(key_info=write_key, workspace_id=write_key.workspace_id)


@pytest.fixture
def mock_mq():
    mq = AsyncMock()
    mq.publish = AsyncMock(return_value="1234567890-0")
    return mq


@pytest.fixture
def mock_db():
    db = AsyncMock()
    db.get_conversation = AsyncMock(return_value=None)
    db.get_document_id_by_external_id = AsyncMock(return_value=None)
    return db


@pytest.fixture
def app(write_key, mock_db, mock_mq, mock_resolved_auth_write, mock_resolved_auth_read):
    application = create_app()
    application.dependency_overrides[get_api_key_info] = lambda: write_key
    application.dependency_overrides[get_write_permission] = lambda: write_key
    application.dependency_overrides[get_read_permission] = lambda: write_key
    application.dependency_overrides[resolve_workspace_write] = lambda: mock_resolved_auth_write
    application.dependency_overrides[resolve_workspace_read] = lambda: mock_resolved_auth_read
    application.dependency_overrides[get_database] = lambda: mock_db

    with patch.object(
        conversation_intake, "get_mq_service", new_callable=AsyncMock, return_value=mock_mq
    ):
        yield application

    application.dependency_overrides.clear()


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


class TestAppendConversationTurns:
    async def test_batch_of_turns_returns_202(self, client, mock_mq):
        response = await client.post(
            "/v1/conversations/conv-1/turns",
            json={
                "turns": [
                    {
                        "turn_id": "t1",
                        "role": "user",
                        "text": "hello",
                        "ts": "2026-08-31T10:00:00Z",
                        "client": "agent-cli",
                    },
                    {
                        "turn_id": "t2",
                        "role": "assistant",
                        "text": "hi there",
                        "ts": "2026-08-31T10:00:05Z",
                    },
                ]
            },
        )

        assert response.status_code == 202
        body = response.json()
        assert body["external_id"] == "conv-1"
        assert body["workspace_id"] == "test-workspace-id"
        assert body["accepted"] == 2
        assert mock_mq.publish.await_count == 2

    async def test_empty_turns_list_is_rejected(self, client):
        response = await client.post("/v1/conversations/conv-1/turns", json={"turns": []})
        assert response.status_code == 422

    async def test_invalid_role_is_rejected(self, client):
        response = await client.post(
            "/v1/conversations/conv-1/turns",
            json={
                "turns": [
                    {
                        "turn_id": "t1",
                        "role": "system",  # only "user"/"assistant" are valid
                        "text": "hello",
                        "ts": "2026-08-31T10:00:00Z",
                    }
                ]
            },
        )
        assert response.status_code == 422

    async def test_missing_write_permission_is_rejected(self, app):
        from src.core.exceptions import AuthorizationError

        async def _deny():
            raise AuthorizationError(detail="API key does not have 'write' permission")

        # resolve_workspace_write is the dependency the route itself takes
        # (already overridden in the `app` fixture to always succeed) --
        # overriding it directly, not get_write_permission underneath it,
        # is what actually changes what the route sees.
        app.dependency_overrides[resolve_workspace_write] = _deny
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            response = await ac.post(
                "/v1/conversations/conv-1/turns",
                json={
                    "turns": [
                        {
                            "turn_id": "t1",
                            "role": "user",
                            "text": "hello",
                            "ts": "2026-08-31T10:00:00Z",
                        }
                    ]
                },
            )
        assert response.status_code == 403


class TestGetConversation:
    async def test_returns_404_when_conversation_not_found(self, client, mock_db):
        mock_db.get_conversation = AsyncMock(return_value=None)

        response = await client.get("/v1/conversations/does-not-exist")

        assert response.status_code == 404

    async def test_returns_conversation_stats(self, client, mock_db):
        now = datetime.now(timezone.utc)
        mock_db.get_conversation = AsyncMock(
            return_value={
                "document_id": "conv-test-workspace-id-conv-1",
                "workspace_id": "test-workspace-id",
                "external_id": "conv-1",
                "status": "processed",
                "chunk_count": 7,
                "turn_count": 12,
                "last_flushed_at": "2026-08-31T10:05:00Z",
                "created_at": now,
                "updated_at": now,
            }
        )

        response = await client.get("/v1/conversations/conv-1")

        assert response.status_code == 200
        body = response.json()
        assert body["external_id"] == "conv-1"
        assert body["workspace_id"] == "test-workspace-id"
        assert body["turn_count"] == 12
        assert body["chunk_count"] == 7
        assert body["status"] == "processed"

    async def test_scoped_to_the_authenticated_workspace(self, client, mock_db):
        """The DB call must be scoped by workspace_id -- existence in
        another workspace must never leak."""
        mock_db.get_conversation = AsyncMock(return_value=None)

        await client.get("/v1/conversations/conv-1")

        mock_db.get_conversation.assert_awaited_once_with("test-workspace-id", "conv-1")


class TestDeleteConversation:
    async def test_returns_404_when_conversation_not_found(self, client, mock_db):
        mock_db.get_document_id_by_external_id = AsyncMock(return_value=None)

        response = await client.delete("/v1/conversations/does-not-exist")

        assert response.status_code == 404

    async def test_deletes_and_returns_204(self, client, mock_db):
        from src.api.v1 import conversations as conversations_route
        from src.services.deletion import DeletionOutcome

        mock_db.get_document_id_by_external_id = AsyncMock(
            return_value="conv-test-workspace-id-conv-1"
        )
        outcome = DeletionOutcome(found=True, vectors_deleted=5, chunks_deleted=5)

        with patch.object(
            conversations_route, "delete_document_everywhere", new=AsyncMock(return_value=outcome)
        ):
            response = await client.delete("/v1/conversations/conv-1")

        assert response.status_code == 204

    async def test_repeat_delete_returns_404(self, client, mock_db):
        from src.api.v1 import conversations as conversations_route
        from src.services.deletion import DeletionOutcome

        mock_db.get_document_id_by_external_id = AsyncMock(
            return_value="conv-test-workspace-id-conv-1"
        )
        outcome = DeletionOutcome(found=False)

        with patch.object(
            conversations_route, "delete_document_everywhere", new=AsyncMock(return_value=outcome)
        ):
            response = await client.delete("/v1/conversations/conv-1")

        assert response.status_code == 404
