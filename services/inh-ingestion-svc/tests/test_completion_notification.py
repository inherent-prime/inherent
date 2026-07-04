"""Tests for document completion notification feature.

Tests cover:
- MemoryMQService.publish_completion() — success, failure, no topic, error swallowing
- DocumentProcessor completion publishing — success, failure, no mq_service
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.config.settings import Settings
from src.models.document import DocumentUploadMessage, ProcessingResult
from src.services.mq.memory_mq import MemoryMQService
from src.services.processor import DocumentProcessor

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_upload_message(**overrides: object) -> dict:
    """Return a valid DocumentUploadMessage dict with optional overrides."""
    base: dict = {
        "event_type": "document.uploaded",
        "document_id": "507f1f77bcf86cd799439011",
        "workspace_id": "507f1f77bcf86cd799439012",
        "user_id": "507f1f77bcf86cd799439013",
        "filename": "test-file.txt",
        "original_filename": "test-file.txt",
        "content_type": "text/plain",
        "size_bytes": 1024,
        "storage_backend": "local",
        "storage_path": "workspaces/ws1/test-file.txt",
        "storage_bucket": None,
        "storage_url": "http://localhost:4000/api/v1/storage/documents/test-file.txt",
        "timestamp": "2026-01-01T00:00:00Z",
    }
    base.update(overrides)
    return base


def make_mock_settings(**overrides: object) -> MagicMock:
    """Return a MagicMock that quacks like Settings."""
    settings = MagicMock(spec=Settings)
    settings.mq_completion_topic = "core.document.processed.v1"
    settings.gcp_project_id = "test-project"
    settings.pubsub_subscription = "projects/test-project/subscriptions/test-sub"
    settings.max_workers = 1
    settings.chunking_strategy = "sentences"
    settings.max_chunk_size = 1000
    settings.chunk_overlap = 200
    for k, v in overrides.items():
        setattr(settings, k, v)
    return settings


# ---------------------------------------------------------------------------
# MemoryMQService.publish_completion tests
# ---------------------------------------------------------------------------


class TestPublishCompletion:
    """Tests for MemoryMQService.publish_completion()."""

    @pytest.fixture
    def mock_settings(self) -> MagicMock:
        return make_mock_settings()

    @pytest.fixture
    def mq_service(self, mock_settings: MagicMock) -> MemoryMQService:
        return MemoryMQService(mock_settings)

    @pytest.fixture
    def upload_message(self) -> DocumentUploadMessage:
        return DocumentUploadMessage(**make_upload_message())

    # 1 ---------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_publish_completion_success(
        self, mq_service: MemoryMQService, upload_message: DocumentUploadMessage
    ) -> None:
        """publish_completion publishes the correct message on success."""
        mq_service.publish = AsyncMock()  # type: ignore[method-assign]

        result = ProcessingResult(
            document_id="507f1f77bcf86cd799439011",
            success=True,
            chunks_created=5,
            processing_time_ms=1234,
        )

        await mq_service.publish_completion(result, upload_message)

        mq_service.publish.assert_called_once()
        call_args = mq_service.publish.call_args
        topic = call_args[0][0]
        message = call_args[0][1]

        assert topic == "core.document.processed.v1"
        assert message["event_type"] == "document.processed"
        assert message["document_id"] == "507f1f77bcf86cd799439011"
        assert message["workspace_id"] == upload_message.workspace_id
        assert message["user_id"] == upload_message.user_id
        assert message["original_filename"] == upload_message.original_filename
        assert message["success"] is True
        assert message["status"] == "ready"
        assert message["chunks_created"] == 5
        assert message["processing_time_ms"] == 1234
        assert message["error"] is None
        assert "timestamp" in message

    # 2 ---------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_publish_completion_failure_result(
        self, mq_service: MemoryMQService, upload_message: DocumentUploadMessage
    ) -> None:
        """publish_completion publishes the correct message on failure."""
        mq_service.publish = AsyncMock()  # type: ignore[method-assign]

        result = ProcessingResult(
            document_id="507f1f77bcf86cd799439011",
            success=False,
            error="Some error",
        )

        await mq_service.publish_completion(result, upload_message)

        mq_service.publish.assert_called_once()
        message = mq_service.publish.call_args[0][1]

        assert message["event_type"] == "document.failed"
        assert message["success"] is False
        assert message["status"] == "failed"
        assert message["error"] == "Some error"
        assert message["chunks_created"] == 0

    # 3 ---------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_publish_completion_no_topic(
        self, mq_service: MemoryMQService, upload_message: DocumentUploadMessage
    ) -> None:
        """publish_completion is a no-op when the completion topic is not configured."""
        mq_service.settings.mq_completion_topic = None
        mq_service.publish = AsyncMock()  # type: ignore[method-assign]

        result = ProcessingResult(
            document_id="507f1f77bcf86cd799439011",
            success=True,
            chunks_created=3,
        )

        await mq_service.publish_completion(result, upload_message)

        mq_service.publish.assert_not_called()

    # 4 ---------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_publish_completion_publish_error_does_not_raise(
        self, mq_service: MemoryMQService, upload_message: DocumentUploadMessage
    ) -> None:
        """publish_completion swallows exceptions raised by publish()."""
        mq_service.publish = AsyncMock(  # type: ignore[method-assign]
            side_effect=RuntimeError("Pub/Sub exploded")
        )

        result = ProcessingResult(
            document_id="507f1f77bcf86cd799439011",
            success=True,
            chunks_created=1,
        )

        # Must not propagate the exception, but the drop must be observable via
        # a metric (best-effort publish is no longer a silent loss) (#37).
        from src.services.metrics import COMPLETION_PUBLISH_FAILURES_TOTAL

        before = COMPLETION_PUBLISH_FAILURES_TOTAL._value.get()
        await mq_service.publish_completion(result, upload_message)

        mq_service.publish.assert_called_once()
        assert COMPLETION_PUBLISH_FAILURES_TOTAL._value.get() == before + 1


# ---------------------------------------------------------------------------
# DocumentProcessor completion publishing tests
# ---------------------------------------------------------------------------


class TestCompletionMessageStorageMetadata:
    """Tests for storage metadata fields in completion messages."""

    @pytest.fixture
    def mock_settings(self) -> MagicMock:
        return make_mock_settings()

    @pytest.fixture
    def mq_service(self, mock_settings: MagicMock) -> MemoryMQService:
        return MemoryMQService(mock_settings)

    @pytest.fixture
    def upload_message(self) -> DocumentUploadMessage:
        return DocumentUploadMessage(**make_upload_message())

    @pytest.mark.asyncio
    async def test_completion_includes_storage_metadata(
        self, mq_service: MemoryMQService, upload_message: DocumentUploadMessage
    ) -> None:
        """Completion message includes all storage metadata from upload message."""
        mq_service.publish = AsyncMock()  # type: ignore[method-assign]

        result = ProcessingResult(
            document_id="507f1f77bcf86cd799439011",
            success=True,
            chunks_created=3,
            processing_time_ms=500,
        )

        await mq_service.publish_completion(result, upload_message)

        message = mq_service.publish.call_args[0][1]

        assert message["content_type"] == "text/plain"
        assert message["size_bytes"] == 1024
        assert message["storage_backend"] == "local"
        assert message["storage_path"] == "workspaces/ws1/test-file.txt"
        assert message["storage_bucket"] is None
        assert (
            message["storage_url"] == "http://localhost:4000/api/v1/storage/documents/test-file.txt"
        )

    @pytest.mark.asyncio
    async def test_completion_storage_path_falls_back_to_filename(
        self, mq_service: MemoryMQService
    ) -> None:
        """storage_path falls back to filename when upload_message.storage_path is falsy."""
        upload_msg = DocumentUploadMessage(
            **make_upload_message(storage_path="", filename="fallback-file.txt")
        )
        mq_service.publish = AsyncMock()  # type: ignore[method-assign]

        result = ProcessingResult(
            document_id="507f1f77bcf86cd799439011",
            success=True,
            chunks_created=1,
        )

        await mq_service.publish_completion(result, upload_msg)

        message = mq_service.publish.call_args[0][1]
        assert message["storage_path"] == "fallback-file.txt"

    @pytest.mark.asyncio
    async def test_completion_with_s3_storage_metadata(self, mq_service: MemoryMQService) -> None:
        """Completion message correctly includes S3 storage metadata."""
        upload_msg = DocumentUploadMessage(
            **make_upload_message(
                storage_backend="s3",
                storage_path="workspaces/ws1/doc.pdf",
                storage_bucket="inherent-documents",
                storage_url="https://s3.example.com/inherent-documents/workspaces/ws1/doc.pdf",
                content_type="application/pdf",
                size_bytes=204800,
            )
        )
        mq_service.publish = AsyncMock()  # type: ignore[method-assign]

        result = ProcessingResult(
            document_id="507f1f77bcf86cd799439011",
            success=True,
            chunks_created=10,
            processing_time_ms=2000,
        )

        await mq_service.publish_completion(result, upload_msg)

        message = mq_service.publish.call_args[0][1]

        assert message["content_type"] == "application/pdf"
        assert message["size_bytes"] == 204800
        assert message["storage_backend"] == "s3"
        assert message["storage_path"] == "workspaces/ws1/doc.pdf"
        assert message["storage_bucket"] == "inherent-documents"
        assert (
            message["storage_url"]
            == "https://s3.example.com/inherent-documents/workspaces/ws1/doc.pdf"
        )

    @pytest.mark.asyncio
    async def test_completion_failure_still_includes_storage_metadata(
        self, mq_service: MemoryMQService, upload_message: DocumentUploadMessage
    ) -> None:
        """Storage metadata is included even when processing fails."""
        mq_service.publish = AsyncMock()  # type: ignore[method-assign]

        result = ProcessingResult(
            document_id="507f1f77bcf86cd799439011",
            success=False,
            error="Parse error",
        )

        await mq_service.publish_completion(result, upload_message)

        message = mq_service.publish.call_args[0][1]

        assert message["event_type"] == "document.failed"
        assert message["content_type"] == "text/plain"
        assert message["size_bytes"] == 1024
        assert message["storage_backend"] == "local"


class TestDocumentCompletionMessageModel:
    """Tests for the DocumentCompletionMessage pydantic model."""

    def test_model_with_storage_metadata(self) -> None:
        """DocumentCompletionMessage accepts storage metadata fields."""
        from src.models.document import DocumentCompletionMessage

        msg = DocumentCompletionMessage(
            event_type="document.processed",
            document_id="abc123",
            workspace_id="ws123",
            user_id="user123",
            original_filename="test.pdf",
            success=True,
            status="ready",
            chunks_created=5,
            processing_time_ms=1000,
            timestamp="2026-01-01T00:00:00Z",
            content_type="application/pdf",
            size_bytes=2048,
            storage_backend="s3",
            storage_path="workspaces/ws123/test.pdf",
            storage_bucket="inherent-documents",
            storage_url="https://s3.example.com/test.pdf",
        )

        assert msg.content_type == "application/pdf"
        assert msg.size_bytes == 2048
        assert msg.storage_backend == "s3"
        assert msg.storage_path == "workspaces/ws123/test.pdf"
        assert msg.storage_bucket == "inherent-documents"
        assert msg.storage_url == "https://s3.example.com/test.pdf"

    def test_model_without_storage_metadata_backward_compatible(self) -> None:
        """DocumentCompletionMessage works without storage fields (backward compat)."""
        from src.models.document import DocumentCompletionMessage

        msg = DocumentCompletionMessage(
            event_type="document.processed",
            document_id="abc123",
            workspace_id="ws123",
            user_id="user123",
            original_filename="test.pdf",
            success=True,
            status="ready",
            timestamp="2026-01-01T00:00:00Z",
        )

        assert msg.content_type is None
        assert msg.size_bytes is None
        assert msg.storage_backend is None
        assert msg.storage_path is None
        assert msg.storage_bucket is None
        assert msg.storage_url is None

    def test_model_serialization_includes_storage_fields(self) -> None:
        """Serialized model includes storage metadata when set."""
        from src.models.document import DocumentCompletionMessage

        msg = DocumentCompletionMessage(
            event_type="document.processed",
            document_id="abc123",
            workspace_id="ws123",
            user_id="user123",
            original_filename="test.pdf",
            success=True,
            status="ready",
            timestamp="2026-01-01T00:00:00Z",
            content_type="text/plain",
            size_bytes=512,
        )

        data = msg.model_dump()
        assert data["content_type"] == "text/plain"
        assert data["size_bytes"] == 512
        assert data["storage_backend"] is None


class TestProcessorCompletionPublishing:
    """Tests for how DocumentProcessor calls publish_completion."""

    @pytest.fixture
    def mock_settings(self) -> MagicMock:
        return make_mock_settings()

    def _make_processor(
        self,
        settings: MagicMock,
        mq_service: MemoryMQService | MagicMock | None = None,
    ) -> DocumentProcessor:
        """Create a processor with all internal services mocked out."""
        processor = DocumentProcessor(settings, mq_service=mq_service)
        processor._initialized = True

        # Mock storage service — returns bytes
        mock_storage = MagicMock()
        mock_storage.read_file_from_url = MagicMock(return_value=b"Hello world document text.")
        mock_storage.read_file = MagicMock(return_value=b"Hello world document text.")
        processor.storage_service = mock_storage

        # Mock database service
        mock_db = MagicMock()
        mock_db.store_processed_document = AsyncMock(return_value=1)
        processor.db_service = mock_db

        # Mock weaviate service
        mock_weaviate = MagicMock()
        mock_weaviate.store_chunks_with_tenant = AsyncMock(return_value=1)
        processor.weaviate_service = mock_weaviate

        # Mock tenant manager
        mock_tenant_manager = MagicMock()
        mock_tenant_manager.ensure_workspace_ready = AsyncMock(return_value=1)
        mock_tenant_manager.update_workspace_stats = AsyncMock()
        processor.tenant_manager = mock_tenant_manager

        return processor

    # 5 ---------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_processor_publishes_completion_on_success(
        self, mock_settings: MagicMock
    ) -> None:
        """Processor calls publish_completion with success=True on successful processing."""
        mock_mq = MagicMock()
        mock_mq.publish_completion = AsyncMock()

        processor = self._make_processor(mock_settings, mq_service=mock_mq)
        message = make_upload_message()

        result = await processor.process_message(message)

        assert result.success is True

        mock_mq.publish_completion.assert_called_once()
        call_result, call_upload_msg = mock_mq.publish_completion.call_args[0]

        assert isinstance(call_result, ProcessingResult)
        assert call_result.success is True
        assert call_result.document_id == message["document_id"]
        assert call_result.chunks_created >= 1

        assert isinstance(call_upload_msg, DocumentUploadMessage)
        assert call_upload_msg.document_id == message["document_id"]

    # 6 ---------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_processor_publishes_completion_on_failure(
        self, mock_settings: MagicMock
    ) -> None:
        """Processor calls publish_completion with success=False when processing raises."""
        mock_mq = MagicMock()
        mock_mq.publish_completion = AsyncMock()

        processor = self._make_processor(mock_settings, mq_service=mock_mq)

        # Make _chunk_text raise so the outer except block in process_message
        # is triggered.  That block builds a failure ProcessingResult and
        # calls publish_completion.
        processor._chunk_text = MagicMock(  # type: ignore[method-assign]
            side_effect=RuntimeError("Chunking exploded")
        )

        message = make_upload_message()
        result = await processor.process_message(message)

        assert result.success is False
        assert "Chunking exploded" in (result.error or "")

        mock_mq.publish_completion.assert_called_once()
        call_result, call_upload_msg = mock_mq.publish_completion.call_args[0]

        assert isinstance(call_result, ProcessingResult)
        assert call_result.success is False
        assert "Chunking exploded" in (call_result.error or "")

    # 7 ---------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_processor_works_without_mq_service(self, mock_settings: MagicMock) -> None:
        """Processor completes without error when mq_service is None."""
        processor = self._make_processor(mock_settings, mq_service=None)
        message = make_upload_message()

        result = await processor.process_message(message)

        # Should succeed without raising
        assert result.success is True
        assert result.chunks_created >= 1
