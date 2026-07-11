"""Unit tests for src/services/document_intake.py (#87 Task 3).

``intake_document`` is a PURE MOVE of the POST /v1/documents body (steps 1-6:
content-type validation, size validation, content-hash dedup, S3 upload,
pending-row persistence, MQ publish) out of the REST handler so both REST and
the ``upload_document`` MCP tool share identical behaviour. These tests pin
that behaviour at the service boundary — the REST-layer tests in
tests/unit/test_upload_document.py exercise the same logic through the HTTP
route and must stay green after the extraction.
"""

from __future__ import annotations

import hashlib
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.exceptions import BadRequestError, ServiceUnavailableError
from src.services import document_intake

pytestmark = [pytest.mark.unit]


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


def _patches(mock_storage, mock_mq):
    return (
        patch.object(document_intake, "get_storage_service", return_value=mock_storage),
        patch.object(document_intake, "get_mq_service", new=AsyncMock(return_value=mock_mq)),
    )


class TestIntakeDocumentSuccess:
    async def test_returns_pending_upload_response(self, mock_db, mock_storage, mock_mq):
        p1, p2 = _patches(mock_storage, mock_mq)
        with p1, p2:
            result = await document_intake.intake_document(
                database=mock_db,
                workspace_id="test-workspace-id",
                user_id="test-user-id",
                content_bytes=b"hello world",
                filename="test.pdf",
                content_type="application/pdf",
            )

        assert result.name == "test.pdf"
        assert result.workspace_id == "test-workspace-id"
        assert result.mime_type == "application/pdf"
        assert result.size_bytes == len(b"hello world")
        assert result.status == "pending"
        assert result.document_id
        assert result.storage_url

    async def test_calls_storage_with_expected_args(self, mock_db, mock_storage, mock_mq):
        p1, p2 = _patches(mock_storage, mock_mq)
        with p1, p2:
            await document_intake.intake_document(
                database=mock_db,
                workspace_id="test-workspace-id",
                user_id="test-user-id",
                content_bytes=b"hello world",
                filename="test.pdf",
                content_type="application/pdf",
            )

        mock_storage.generate_key.assert_called_once_with("test-workspace-id", "test.pdf")
        mock_storage.upload_file.assert_awaited_once()

    async def test_publishes_mq_message(self, mock_db, mock_storage, mock_mq):
        p1, p2 = _patches(mock_storage, mock_mq)
        with p1, p2:
            await document_intake.intake_document(
                database=mock_db,
                workspace_id="test-workspace-id",
                user_id="test-user-id",
                content_bytes=b"hello world",
                filename="test.pdf",
                content_type="application/pdf",
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

    async def test_content_hash_persisted_and_checked_before_filename(
        self, mock_db, mock_storage, mock_mq
    ):
        p1, p2 = _patches(mock_storage, mock_mq)
        with p1, p2:
            await document_intake.intake_document(
                database=mock_db,
                workspace_id="test-workspace-id",
                user_id="test-user-id",
                content_bytes=b"hello world",
                filename="test.pdf",
                content_type="application/pdf",
            )

        kwargs = mock_db.create_or_reset_pending_document.call_args.kwargs
        assert kwargs["content_hash"] == hashlib.sha256(b"hello world").hexdigest()
        mock_db.get_document_id_by_content_hash.assert_awaited_once()
        mock_db.get_document_id_by_filename.assert_awaited_once()


class TestIntakeDocumentValidation:
    async def test_unsupported_mime_type_raises_bad_request(self, mock_db, mock_storage, mock_mq):
        p1, p2 = _patches(mock_storage, mock_mq)
        with p1, p2, pytest.raises(BadRequestError):
            await document_intake.intake_document(
                database=mock_db,
                workspace_id="test-workspace-id",
                user_id="test-user-id",
                content_bytes=b"hello world",
                filename="malware.exe",
                content_type="application/x-msdownload",
            )

    async def test_empty_content_raises_bad_request(self, mock_db, mock_storage, mock_mq):
        p1, p2 = _patches(mock_storage, mock_mq)
        with p1, p2, pytest.raises(BadRequestError):
            await document_intake.intake_document(
                database=mock_db,
                workspace_id="test-workspace-id",
                user_id="test-user-id",
                content_bytes=b"",
                filename="empty.txt",
                content_type="text/plain",
            )

    async def test_oversized_content_raises_bad_request(self, mock_db, mock_storage, mock_mq):
        big_content = b"x" * (50 * 1024 * 1024 + 1)
        p1, p2 = _patches(mock_storage, mock_mq)
        with p1, p2, pytest.raises(BadRequestError):
            await document_intake.intake_document(
                database=mock_db,
                workspace_id="test-workspace-id",
                user_id="test-user-id",
                content_bytes=big_content,
                filename="big.txt",
                content_type="text/plain",
            )


class TestIntakeDocumentDedup:
    async def test_reupload_reuses_existing_document_id_by_filename(
        self, mock_db, mock_storage, mock_mq
    ):
        existing_id = "existing-doc-id-123"
        mock_db.get_document_id_by_filename = AsyncMock(return_value=existing_id)

        p1, p2 = _patches(mock_storage, mock_mq)
        with p1, p2:
            result = await document_intake.intake_document(
                database=mock_db,
                workspace_id="test-workspace-id",
                user_id="test-user-id",
                content_bytes=b"hello world",
                filename="test.pdf",
                content_type="application/pdf",
            )

        assert result.document_id == existing_id
        assert (
            mock_db.create_or_reset_pending_document.call_args.kwargs["document_id"] == existing_id
        )
        assert mock_mq.publish.call_args[0][1]["document_id"] == existing_id

    async def test_reupload_reuses_existing_document_id_by_content_hash(
        self, mock_db, mock_storage, mock_mq
    ):
        existing_id = "original-doc-id-abc"
        mock_db.get_document_id_by_content_hash = AsyncMock(return_value=existing_id)
        mock_db.get_document_id_by_filename = AsyncMock(return_value=None)

        p1, p2 = _patches(mock_storage, mock_mq)
        with p1, p2:
            result = await document_intake.intake_document(
                database=mock_db,
                workspace_id="test-workspace-id",
                user_id="test-user-id",
                content_bytes=b"verbatim",
                filename="guide-copy.md",
                content_type="text/markdown",
            )

        assert result.document_id == existing_id
        mock_db.get_document_id_by_filename.assert_not_awaited()

    async def test_new_content_generates_new_uuid(self, mock_db, mock_storage, mock_mq):
        p1, p2 = _patches(mock_storage, mock_mq)
        with p1, p2:
            result = await document_intake.intake_document(
                database=mock_db,
                workspace_id="test-workspace-id",
                user_id="test-user-id",
                content_bytes=b"hello world",
                filename="test.pdf",
                content_type="application/pdf",
            )

        assert uuid.UUID(result.document_id)


class TestIntakeDocumentServiceFailures:
    async def test_storage_failure_raises_service_unavailable(self, mock_db, mock_mq):
        failing_storage = MagicMock()
        failing_storage.generate_key.return_value = "ws/uuid/file.pdf"
        failing_storage.upload_file = AsyncMock(side_effect=Exception("S3 unreachable"))

        p1, p2 = _patches(failing_storage, mock_mq)
        with p1, p2, pytest.raises(ServiceUnavailableError):
            await document_intake.intake_document(
                database=mock_db,
                workspace_id="test-workspace-id",
                user_id="test-user-id",
                content_bytes=b"hello world",
                filename="test.pdf",
                content_type="application/pdf",
            )

    async def test_mq_failure_returns_failed_status_not_raise(self, mock_db, mock_storage):
        failing_mq = AsyncMock()
        failing_mq.publish = AsyncMock(side_effect=Exception("Redis down"))

        p1, p2 = _patches(mock_storage, failing_mq)
        with p1, p2:
            result = await document_intake.intake_document(
                database=mock_db,
                workspace_id="test-workspace-id",
                user_id="test-user-id",
                content_bytes=b"hello world",
                filename="test.pdf",
                content_type="application/pdf",
            )

        assert result.status == "failed"
        assert "enqueue" in result.message.lower()
        mock_db.mark_document_failed.assert_awaited_once()
