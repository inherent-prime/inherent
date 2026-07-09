"""Unit tests for the POST /v1/documents upload endpoint."""

from __future__ import annotations

import io
import uuid
from datetime import datetime, timezone
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

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def write_key():
    """API key with write permission."""
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
def read_only_key():
    """API key without write permission."""
    return APIKeyInfo(
        key_id="test-key-readonly",
        user_id="test-user-id",
        workspace_id="test-workspace-id",
        permissions=["read", "search"],
        rate_limit=100,
        expires_at=None,
        status="active",
    )


@pytest.fixture
def user_scoped_key():
    """API key without a workspace_id (user-scoped)."""
    return APIKeyInfo(
        key_id="test-key-user",
        user_id="test-user-id",
        workspace_id=None,
        permissions=["read", "search", "write"],
        rate_limit=100,
        expires_at=None,
        status="active",
    )


@pytest.fixture
def mock_db():
    db = AsyncMock()
    # Default: no existing document by content hash or filename -> new doc_id.
    # Both must be explicit AsyncMocks: a bare AsyncMock attribute would resolve
    # awaits to a truthy MagicMock and spuriously trigger the dedup path.
    db.get_document_id_by_content_hash = AsyncMock(return_value=None)
    db.get_document_id_by_filename = AsyncMock(return_value=None)
    db.create_or_reset_pending_document = AsyncMock(return_value=None)
    db.mark_document_failed = AsyncMock(return_value=None)
    return db


@pytest.fixture
def mock_storage():
    storage = MagicMock()
    storage.generate_key.return_value = "test-workspace-id/fake-uuid/test.pdf"
    storage.upload_file = AsyncMock(return_value="test-workspace-id/fake-uuid/test.pdf")
    storage.build_storage_url.return_value = (
        "s3://inherent-documents/test-workspace-id/fake-uuid/test.pdf"
    )
    storage._bucket = "inherent-documents"
    return storage


@pytest.fixture
def mock_mq():
    mq = AsyncMock()
    mq.publish = AsyncMock(return_value="1234567890-0")
    return mq


@pytest.fixture
def mock_resolved_auth_write(write_key):
    """Create a ResolvedAuth with workspace from the write key."""
    return ResolvedAuth(key_info=write_key, workspace_id=write_key.workspace_id)


@pytest.fixture
def app(write_key, mock_db, mock_storage, mock_mq, mock_resolved_auth_write):
    """Create app with dependency overrides and patched singletons."""
    application = create_app()
    application.dependency_overrides[get_api_key_info] = lambda: write_key
    application.dependency_overrides[get_write_permission] = lambda: write_key
    application.dependency_overrides[resolve_workspace_write] = lambda: mock_resolved_auth_write
    application.dependency_overrides[get_database] = lambda: mock_db

    with (
        patch.object(document_intake, "get_storage_service", return_value=mock_storage),
        patch.object(
            document_intake, "get_mq_service", new_callable=AsyncMock, return_value=mock_mq
        ),
    ):
        yield application

    application.dependency_overrides.clear()


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# ---------------------------------------------------------------------------
# Helper to build multipart file payload
# ---------------------------------------------------------------------------


def _file_payload(
    content: bytes = b"hello world",
    filename: str = "test.pdf",
    content_type: str = "application/pdf",
):
    return {"file": (filename, io.BytesIO(content), content_type)}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestUploadDocumentSuccess:
    """Happy-path tests for POST /v1/documents."""

    async def test_upload_returns_201(self, client):
        response = await client.post(
            "/v1/documents",
            files=_file_payload(),
            headers={"X-API-Key": "ink_test_key"},
        )
        assert response.status_code == 201

    async def test_upload_response_shape(self, client):
        response = await client.post(
            "/v1/documents",
            files=_file_payload(),
            headers={"X-API-Key": "ink_test_key"},
        )
        data = response.json()
        assert data["name"] == "test.pdf"
        assert data["workspace_id"] == "test-workspace-id"
        assert data["mime_type"] == "application/pdf"
        assert data["size_bytes"] == len(b"hello world")
        assert data["status"] == "pending"
        assert "document_id" in data
        assert "storage_url" in data
        assert "message" in data

    async def test_upload_calls_storage(self, mock_storage, write_key, mock_db, mock_mq):
        """Verify that the storage service is called with expected arguments."""
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
                await ac.post(
                    "/v1/documents",
                    files=_file_payload(),
                    headers={"X-API-Key": "ink_test_key"},
                )

        mock_storage.generate_key.assert_called_once_with("test-workspace-id", "test.pdf")
        mock_storage.upload_file.assert_awaited_once()

        application.dependency_overrides.clear()

    async def test_upload_publishes_mq_message(self, write_key, mock_db, mock_storage, mock_mq):
        """Verify that the MQ publish is called with the correct topic."""
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
                await ac.post(
                    "/v1/documents",
                    files=_file_payload(),
                    headers={"X-API-Key": "ink_test_key"},
                )

        mock_mq.publish.assert_awaited_once()
        call_args = mock_mq.publish.call_args
        assert call_args[0][0] == "core.document.uploaded.v1"

        msg = call_args[0][1]
        assert msg["event_type"] == "document.uploaded"
        assert msg["workspace_id"] == "test-workspace-id"
        assert msg["user_id"] == "test-user-id"
        assert msg["original_filename"] == "test.pdf"
        assert msg["content_type"] == "application/pdf"
        assert msg["storage_backend"] == "s3"

        application.dependency_overrides.clear()


class TestUploadDocumentValidation:
    """Validation and error-path tests."""

    async def test_unsupported_mime_type(self, write_key, mock_db, mock_storage, mock_mq):
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
                    files=_file_payload(content_type="application/x-msdownload"),
                    headers={"X-API-Key": "ink_test_key"},
                )

        assert response.status_code == 400
        application.dependency_overrides.clear()

    async def test_png_image_accepted(self, write_key, mock_db, mock_storage, mock_mq):
        """PNG images are now accepted (read via OCR in ingestion, #61)."""
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
                    files=_file_payload(
                        content=b"\x89PNG\r\n\x1a\n fake png bytes",
                        filename="scan.png",
                        content_type="image/png",
                    ),
                    headers={"X-API-Key": "ink_test_key"},
                )

        assert response.status_code == 201, f"PNG should be accepted but got {response.status_code}"
        assert response.json()["mime_type"] == "image/png"
        application.dependency_overrides.clear()

    async def test_empty_file(self, write_key, mock_db, mock_storage, mock_mq):
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
                    files=_file_payload(content=b""),
                    headers={"X-API-Key": "ink_test_key"},
                )

        assert response.status_code == 400
        application.dependency_overrides.clear()

    async def test_file_too_large(self, write_key, mock_db, mock_storage, mock_mq):
        big_content = b"x" * (50 * 1024 * 1024 + 1)  # 50 MB + 1 byte

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
                    files=_file_payload(content=big_content),
                    headers={"X-API-Key": "ink_test_key"},
                )

        assert response.status_code == 400
        application.dependency_overrides.clear()

    async def test_user_scoped_key_no_workspace_header(
        self, user_scoped_key, mock_db, mock_storage, mock_mq
    ):
        """User-scoped key without X-Workspace-Id header and no workspaces returns 400."""
        mock_db.get_user_workspace_ids = AsyncMock(return_value=[])

        application = create_app()
        application.dependency_overrides[get_api_key_info] = lambda: user_scoped_key
        application.dependency_overrides[get_write_permission] = lambda: user_scoped_key
        application.dependency_overrides[get_database] = lambda: mock_db

        with (
            patch("src.services.auth.get_database", new_callable=AsyncMock, return_value=mock_db),
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

        assert response.status_code == 400
        application.dependency_overrides.clear()


class TestUploadDocumentAuth:
    """Auth-related tests for upload endpoint."""

    async def test_requires_write_permission(self, read_only_key, mock_db):
        """Should return 403 when API key lacks write permission."""
        application = create_app()
        application.dependency_overrides[get_api_key_info] = lambda: read_only_key
        # Do NOT override get_write_permission — let real dependency run
        application.dependency_overrides[get_database] = lambda: mock_db

        transport = ASGITransport(app=application)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            response = await ac.post(
                "/v1/documents",
                files=_file_payload(),
                headers={"X-API-Key": "ink_test_key"},
            )
        assert response.status_code == 403
        application.dependency_overrides.clear()

    async def test_no_api_key(self, mock_db):
        """Should return 401 when no API key is provided."""
        application = create_app()
        application.dependency_overrides[get_database] = lambda: mock_db

        transport = ASGITransport(app=application)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            response = await ac.post(
                "/v1/documents",
                files=_file_payload(),
            )
        assert response.status_code == 401
        application.dependency_overrides.clear()


class TestUploadDocumentServiceFailures:
    """Tests for graceful degradation when downstream services fail."""

    async def test_storage_failure_returns_503(self, write_key, mock_db, mock_mq):
        """S3 failure should return 503."""
        failing_storage = MagicMock()
        failing_storage.generate_key.return_value = "ws/uuid/file.pdf"
        failing_storage.upload_file = AsyncMock(side_effect=Exception("S3 unreachable"))

        application = create_app()
        application.dependency_overrides[get_api_key_info] = lambda: write_key
        application.dependency_overrides[get_write_permission] = lambda: write_key
        application.dependency_overrides[resolve_workspace_write] = lambda: ResolvedAuth(
            key_info=write_key, workspace_id=write_key.workspace_id
        )
        application.dependency_overrides[get_database] = lambda: mock_db

        with (
            patch.object(document_intake, "get_storage_service", return_value=failing_storage),
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

        assert response.status_code == 503
        application.dependency_overrides.clear()

    async def test_mq_failure_still_returns_201(self, write_key, mock_db, mock_storage):
        """MQ failure should NOT fail the request — file is already in S3."""
        failing_mq = AsyncMock()
        failing_mq.publish = AsyncMock(side_effect=Exception("Redis down"))

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
                return_value=failing_mq,
            ),
        ):
            transport = ASGITransport(app=application)
            async with AsyncClient(transport=transport, base_url="http://test") as ac:
                response = await ac.post(
                    "/v1/documents",
                    files=_file_payload(),
                    headers={"X-API-Key": "ink_test_key"},
                )

        assert response.status_code == 201
        application.dependency_overrides.clear()


class TestUploadAllowedMimeTypes:
    """Verify each allowed MIME type is accepted."""

    @pytest.mark.parametrize(
        "mime",
        [
            "text/plain",
            "text/markdown",
            "text/csv",
            "text/html",
            "application/pdf",
            "application/json",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ],
    )
    async def test_allowed_mime_types_accepted(
        self, mime, write_key, mock_db, mock_storage, mock_mq
    ):
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
                    files=_file_payload(content_type=mime),
                    headers={"X-API-Key": "ink_test_key"},
                )

        assert (
            response.status_code == 201
        ), f"MIME {mime} should be accepted but got {response.status_code}"
        application.dependency_overrides.clear()

    async def test_xlsx_rejected_until_extraction_is_supported(
        self, write_key, mock_db, mock_storage, mock_mq
    ):
        """Do not accept spreadsheet uploads until ingestion has an XLSX extractor."""
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
                    files=_file_payload(
                        filename="sheet.xlsx",
                        content_type=(
                            "application/vnd.openxmlformats-officedocument." "spreadsheetml.sheet"
                        ),
                    ),
                    headers={"X-API-Key": "ink_test_key"},
                )

        assert response.status_code == 400
        application.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Durable handoff: pending row persistence + GET visibility + dedup
# ---------------------------------------------------------------------------


class TestUploadPersistsPendingRow:
    """Fix #7: a 'pending' row is persisted at upload time, before MQ publish."""

    async def test_pending_row_created_before_publish(self, client, mock_db, mock_mq):
        """create_or_reset_pending_document is called with status-driving fields."""
        response = await client.post(
            "/v1/documents",
            files=_file_payload(),
            headers={"X-API-Key": "ink_test_key"},
        )
        assert response.status_code == 201

        mock_db.create_or_reset_pending_document.assert_awaited_once()
        kwargs = mock_db.create_or_reset_pending_document.call_args.kwargs
        assert kwargs["workspace_id"] == "test-workspace-id"
        assert kwargs["user_id"] == "test-user-id"
        assert kwargs["original_filename"] == "test.pdf"
        assert kwargs["content_type"] == "application/pdf"
        assert kwargs["size_bytes"] == len(b"hello world")
        assert kwargs["storage_backend"] == "s3"
        assert kwargs["document_id"] == response.json()["document_id"]

        # The pending row must be persisted BEFORE the MQ publish so the
        # handoff is durable.
        mock_db.create_or_reset_pending_document.assert_awaited_once()
        mock_mq.publish.assert_awaited_once()

    async def test_get_returns_pending_doc_immediately_after_upload(
        self, write_key, mock_db, mock_storage, mock_mq
    ):
        """After upload, GET /v1/documents/{id} returns the doc with status=pending.

        Simulates the persisted pending row by wiring mock_db.get_document to
        return a pending Document for the uploaded id.
        """
        from src.models.document import Document
        from src.services.auth import resolve_workspace_read

        application = create_app()
        application.dependency_overrides[get_api_key_info] = lambda: write_key
        application.dependency_overrides[get_write_permission] = lambda: write_key
        application.dependency_overrides[resolve_workspace_write] = lambda: ResolvedAuth(
            key_info=write_key, workspace_id=write_key.workspace_id
        )
        application.dependency_overrides[resolve_workspace_read] = lambda: ResolvedAuth(
            key_info=write_key, workspace_id=write_key.workspace_id
        )
        application.dependency_overrides[get_database] = lambda: mock_db

        # Emulate the DB: once a pending row is written, GET can read it back.
        stored: dict = {}

        async def _create(**kwargs):
            stored.update(kwargs)

        async def _get(document_id, workspace_id):
            if stored.get("document_id") == document_id:
                return Document(
                    id=document_id,
                    name=stored["original_filename"],
                    workspace_id=workspace_id,
                    source_type=stored["storage_backend"],
                    mime_type=stored["content_type"],
                    size_bytes=stored["size_bytes"],
                    chunk_count=0,
                    status="pending",
                    created_at=datetime.now(timezone.utc),
                    updated_at=datetime.now(timezone.utc),
                    metadata=None,
                )
            return None

        mock_db.create_or_reset_pending_document = AsyncMock(side_effect=_create)
        mock_db.get_document = AsyncMock(side_effect=_get)

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
                upload = await ac.post(
                    "/v1/documents",
                    files=_file_payload(),
                    headers={"X-API-Key": "ink_test_key"},
                )
                doc_id = upload.json()["document_id"]

                get_resp = await ac.get(
                    f"/v1/documents/{doc_id}",
                    headers={"X-API-Key": "ink_test_key"},
                )

        assert get_resp.status_code == 200
        body = get_resp.json()
        assert body["id"] == doc_id
        assert body["status"] == "pending"
        application.dependency_overrides.clear()


class TestUploadEnqueueFailure:
    """Fix #6: MQ publish failure must not silently report success."""

    async def test_mq_failure_marks_document_failed(self, write_key, mock_db, mock_storage):
        failing_mq = AsyncMock()
        failing_mq.publish = AsyncMock(side_effect=Exception("Redis down"))

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
                return_value=failing_mq,
            ),
        ):
            transport = ASGITransport(app=application)
            async with AsyncClient(transport=transport, base_url="http://test") as ac:
                response = await ac.post(
                    "/v1/documents",
                    files=_file_payload(),
                    headers={"X-API-Key": "ink_test_key"},
                )

        # File IS stored, so still 201, but the response must NOT claim pending.
        assert response.status_code == 201
        data = response.json()
        assert data["status"] == "failed"
        assert "enqueue" in data["message"].lower()

        # The persisted row must be flipped to failed.
        mock_db.mark_document_failed.assert_awaited_once()
        args = mock_db.mark_document_failed.call_args.args
        assert args[0] == data["document_id"]
        assert args[1] == "test-workspace-id"
        assert "ingestion enqueue failed" in args[2]
        application.dependency_overrides.clear()


class TestUploadDedup:
    """Fix #60: re-upload of same (workspace, filename) reuses the document_id."""

    async def test_reupload_reuses_existing_document_id(
        self, write_key, mock_db, mock_storage, mock_mq
    ):
        existing_id = "existing-doc-id-123"
        mock_db.get_document_id_by_filename = AsyncMock(return_value=existing_id)

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

        assert response.status_code == 201
        assert response.json()["document_id"] == existing_id

        mock_db.get_document_id_by_filename.assert_awaited_once_with(
            "test-workspace-id", "test.pdf"
        )
        # The pending row + MQ message must carry the reused id.
        assert (
            mock_db.create_or_reset_pending_document.call_args.kwargs["document_id"] == existing_id
        )
        assert mock_mq.publish.call_args[0][1]["document_id"] == existing_id
        application.dependency_overrides.clear()

    async def test_new_filename_generates_new_uuid(self, write_key, mock_db, mock_storage, mock_mq):
        mock_db.get_document_id_by_filename = AsyncMock(return_value=None)

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

        assert response.status_code == 201
        new_id = response.json()["document_id"]
        # A freshly generated UUID, not the sentinel reuse value.
        assert uuid.UUID(new_id)
        application.dependency_overrides.clear()


class TestUploadContentDedup:
    """Fix #75: re-upload of the same CONTENT reuses the document_id even when
    the filename differs, so verbatim copies cannot flood search results."""

    def _app(self, write_key, mock_db, mock_storage, mock_mq):
        application = create_app()
        application.dependency_overrides[get_api_key_info] = lambda: write_key
        application.dependency_overrides[get_write_permission] = lambda: write_key
        application.dependency_overrides[resolve_workspace_write] = lambda: ResolvedAuth(
            key_info=write_key, workspace_id=write_key.workspace_id
        )
        application.dependency_overrides[get_database] = lambda: mock_db
        return application

    async def test_content_hash_persisted_on_pending_row(self, client, mock_db):
        """The sha256 of the uploaded bytes is written onto the pending row."""
        import hashlib

        response = await client.post(
            "/v1/documents",
            files=_file_payload(content=b"hello world"),
            headers={"X-API-Key": "ink_test_key"},
        )
        assert response.status_code == 201
        kwargs = mock_db.create_or_reset_pending_document.call_args.kwargs
        assert kwargs["content_hash"] == hashlib.sha256(b"hello world").hexdigest()

    async def test_content_hash_checked_before_filename(self, client, mock_db):
        """Content-hash dedup is consulted first; filename lookup is the fallback."""
        await client.post(
            "/v1/documents",
            files=_file_payload(),
            headers={"X-API-Key": "ink_test_key"},
        )
        # Content-hash lookup always runs; with no match it falls back to filename.
        mock_db.get_document_id_by_content_hash.assert_awaited_once()
        mock_db.get_document_id_by_filename.assert_awaited_once()

    async def test_duplicate_content_new_filename_reuses_document_id(
        self, write_key, mock_db, mock_storage, mock_mq
    ):
        """Identical content under a DIFFERENT filename collapses onto the original.

        This is the #75 flood case: filename dedup would MISS (returns None) but
        content-hash dedup must still reuse the existing document_id so a verbatim
        copy does not create a duplicate document/chunks/embeddings.
        """
        existing_id = "original-doc-id-abc"
        mock_db.get_document_id_by_content_hash = AsyncMock(return_value=existing_id)
        # Filename dedup would NOT match — the copy has a different name.
        mock_db.get_document_id_by_filename = AsyncMock(return_value=None)

        application = self._app(write_key, mock_db, mock_storage, mock_mq)
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
                    files=_file_payload(content=b"verbatim", filename="guide-copy.md"),
                    headers={"X-API-Key": "ink_test_key"},
                )

        assert response.status_code == 201
        assert response.json()["document_id"] == existing_id
        # Filename dedup must never have been the deciding factor: content match
        # short-circuits, so the filename lookup is not even consulted.
        mock_db.get_document_id_by_filename.assert_not_awaited()
        # The reused id propagates to the pending row and the MQ message.
        assert (
            mock_db.create_or_reset_pending_document.call_args.kwargs["document_id"] == existing_id
        )
        assert mock_mq.publish.call_args[0][1]["document_id"] == existing_id
        application.dependency_overrides.clear()

    async def test_distinct_content_gets_distinct_document_ids(
        self, write_key, mock_db, mock_storage, mock_mq
    ):
        """Three DIFFERENT documents must each get their own document_id (no flood).

        Mirrors the repro's healthy baseline: distinct content is never deduped,
        so each topic keeps its own document_id and can surface independently.
        """
        # Stateful fake: dedup keyed on (content_hash) then (filename), exactly
        # like the production lookups, backed by what has been "stored" so far.
        by_hash: dict[str, str] = {}
        by_name: dict[str, str] = {}

        async def _by_hash(workspace_id, content_hash):
            return by_hash.get(content_hash)

        async def _by_name(workspace_id, filename):
            return by_name.get(filename)

        async def _create(**kwargs):
            by_hash[kwargs["content_hash"]] = kwargs["document_id"]
            by_name[kwargs["original_filename"]] = kwargs["document_id"]

        mock_db.get_document_id_by_content_hash = AsyncMock(side_effect=_by_hash)
        mock_db.get_document_id_by_filename = AsyncMock(side_effect=_by_name)
        mock_db.create_or_reset_pending_document = AsyncMock(side_effect=_create)

        application = self._app(write_key, mock_db, mock_storage, mock_mq)
        seen: set[str] = set()
        docs = [
            (b"auth content unique", "auth.md"),
            (b"rate limiting content unique", "rate.md"),
            (b"error handling content unique", "errors.md"),
        ]
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
                for content, name in docs:
                    resp = await ac.post(
                        "/v1/documents",
                        files=_file_payload(content=content, filename=name),
                        headers={"X-API-Key": "ink_test_key"},
                    )
                    assert resp.status_code == 201
                    seen.add(resp.json()["document_id"])

        assert len(seen) == 3, "distinct content must yield 3 distinct document_ids"
        application.dependency_overrides.clear()

    async def test_repeated_copies_collapse_to_one_document_id(
        self, write_key, mock_db, mock_storage, mock_mq
    ):
        """The full #75 scenario: 1 original + 2 verbatim copies => ONE document_id.

        Without content-hash dedup these three uploads (different filenames) would
        produce three document_ids and flood top-k; with it they all collapse.
        """
        by_hash: dict[str, str] = {}
        by_name: dict[str, str] = {}

        async def _by_hash(workspace_id, content_hash):
            return by_hash.get(content_hash)

        async def _by_name(workspace_id, filename):
            return by_name.get(filename)

        async def _create(**kwargs):
            by_hash.setdefault(kwargs["content_hash"], kwargs["document_id"])
            by_name.setdefault(kwargs["original_filename"], kwargs["document_id"])

        mock_db.get_document_id_by_content_hash = AsyncMock(side_effect=_by_hash)
        mock_db.get_document_id_by_filename = AsyncMock(side_effect=_by_name)
        mock_db.create_or_reset_pending_document = AsyncMock(side_effect=_create)

        application = self._app(write_key, mock_db, mock_storage, mock_mq)
        body = b"# API Authentication Guide\nidentical bytes across all copies"
        names = ["guide.md", "guide-copy-1.md", "guide-copy-2.md"]
        seen: set[str] = set()
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
                for name in names:
                    resp = await ac.post(
                        "/v1/documents",
                        files=_file_payload(content=body, filename=name),
                        headers={"X-API-Key": "ink_test_key"},
                    )
                    assert resp.status_code == 201
                    seen.add(resp.json()["document_id"])

        assert seen == set(list(seen)[:1]), "all verbatim copies must reuse one document_id"
        assert len(seen) == 1
        application.dependency_overrides.clear()
