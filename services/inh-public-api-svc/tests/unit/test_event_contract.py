"""Producer-side test for the versioned upload event contract (#17).

The public API is the PRODUCER of the ``document.uploaded`` event. This test
intercepts the dict handed to ``mq.publish`` and asserts it carries EXACTLY the
canonical v1 key set (including ``contract_version``). The ingestion consumer
asserts the same set independently, so any drift on either side fails CI.
"""

from __future__ import annotations

import io
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from src.main import create_app
from src.models.api_key import APIKeyInfo
from src.services import document_intake
from src.services.auth import (
    ResolvedAuth,
    get_api_key_info,
    get_write_permission,
    resolve_workspace_write,
)
from src.services.database import get_database

# Part of the selectable contract-regression surface (M6 #30).
pytestmark = [pytest.mark.contract]

# The canonical v1 event contract key set. This is the single source of truth on
# the producer side and must match the ingestion consumer's expectation exactly.
CANONICAL_V1_KEYS = {
    "event_type",
    "document_id",
    "workspace_id",
    "user_id",
    "filename",
    "original_filename",
    "content_type",
    "size_bytes",
    "storage_backend",
    "storage_path",
    "storage_bucket",
    "storage_url",
    "timestamp",
    "contract_version",
}


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
def mock_db():
    db = AsyncMock()
    db.get_document_id_by_content_hash = AsyncMock(return_value=None)
    db.get_document_id_by_filename = AsyncMock(return_value=None)
    db.create_or_reset_pending_document = AsyncMock(return_value=None)
    db.mark_document_failed = AsyncMock(return_value=None)
    return db


@pytest.fixture
def mock_storage():
    storage = MagicMock()
    storage.generate_key.return_value = "test-workspace-id/fake-uuid/test.txt"
    storage.upload_file = AsyncMock(return_value="test-workspace-id/fake-uuid/test.txt")
    storage.build_storage_url.return_value = (
        "s3://inherent-documents/test-workspace-id/fake-uuid/test.txt"
    )
    storage._bucket = "inherent-documents"
    return storage


@pytest.fixture
def mock_mq():
    mq = AsyncMock()
    mq.publish = AsyncMock(return_value="1234567890-0")
    return mq


def _file_payload(
    content: bytes = b"hello world",
    filename: str = "test.txt",
    content_type: str = "text/plain",
):
    return {"file": (filename, io.BytesIO(content), content_type)}


async def _publish_upload_event(write_key, mock_db, mock_storage, mock_mq) -> dict:
    """Run the upload route with mocked deps and return the published event dict."""
    application = create_app()
    application.dependency_overrides[get_api_key_info] = lambda: write_key
    application.dependency_overrides[get_write_permission] = lambda: write_key
    application.dependency_overrides[resolve_workspace_write] = lambda: ResolvedAuth(
        key_info=write_key, workspace_id=write_key.workspace_id
    )
    application.dependency_overrides[get_database] = lambda: mock_db

    with (
        patch.object(document_intake, "get_storage_service", return_value=mock_storage),
        patch.object(
            document_intake,
            "get_mq_service",
            new_callable=AsyncMock,
            return_value=mock_mq,
        ),
    ):
        transport = ASGITransport(app=application)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            response = await ac.post(
                "/v1/documents",
                files=_file_payload(),
                headers={"X-API-Key": "ink_test_key"},
            )

    application.dependency_overrides.clear()
    assert response.status_code == 201, response.text

    mock_mq.publish.assert_awaited_once()
    # publish(topic, message) — the event dict is the second positional arg.
    return mock_mq.publish.call_args[0][1]


async def test_event_has_exact_canonical_v1_key_set(write_key, mock_db, mock_storage, mock_mq):
    """The published event must contain EXACTLY the canonical v1 keys — no more, no less."""
    event = await _publish_upload_event(write_key, mock_db, mock_storage, mock_mq)
    assert set(event.keys()) == CANONICAL_V1_KEYS


async def test_event_carries_contract_version(write_key, mock_db, mock_storage, mock_mq):
    """The contract_version must be present and pinned to 1.0.0."""
    event = await _publish_upload_event(write_key, mock_db, mock_storage, mock_mq)
    assert event["contract_version"] == "1.0.0"


async def test_event_type_is_document_uploaded(write_key, mock_db, mock_storage, mock_mq):
    event = await _publish_upload_event(write_key, mock_db, mock_storage, mock_mq)
    assert event["event_type"] == "document.uploaded"
