"""Tests for document processor.

These tests verify the document processing pipeline including:
- Text extraction from various document types
- Text chunking
- Storage to PostgreSQL
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.config.settings import Settings
from src.models.document import DocumentUploadMessage
from src.services.processor import DocumentProcessor


class TestDocumentProcessorInit:
    """Tests for DocumentProcessor initialization."""

    def test_processor_creation(self, test_settings: Settings):
        """Test processor can be created."""
        processor = DocumentProcessor(test_settings)
        assert processor is not None
        assert processor._initialized is False

    def test_processor_initialization(self, test_settings: Settings):
        """Test processor initialization connects services."""
        processor = DocumentProcessor(test_settings)

        # Initialize (may fail to connect to missing services, but shouldn't crash)
        processor.initialize()

        assert processor._initialized is True


class TestMessageValidation:
    """Tests for message validation in processor."""

    @pytest.mark.asyncio
    async def test_invalid_message_returns_error(self, test_settings: Settings):
        """Test that invalid message returns error result."""
        processor = DocumentProcessor(test_settings)
        processor._initialized = True  # Skip service initialization

        invalid_message = {
            "event_type": "invalid.event",
            "document_id": "test",
        }

        result = await processor.process_message(invalid_message)

        assert result.success is False
        assert "Invalid message format" in result.error

    @pytest.mark.asyncio
    async def test_valid_message_structure_accepted(
        self,
        test_settings: Settings,
        sample_upload_message: dict,
    ):
        """Test that valid message structure is accepted."""
        # Verify the message can be parsed
        message = DocumentUploadMessage(**sample_upload_message)
        assert message.document_id == "test_doc_12345"


class TestTextChunking:
    """Tests for text chunking logic."""

    def test_chunk_text_creates_chunks(self, test_settings: Settings):
        """Test that chunking creates multiple chunks from long text."""
        processor = DocumentProcessor(test_settings)
        processor._initialized = True

        # Create a message for context
        message = DocumentUploadMessage(
            event_type="document.uploaded",
            document_id="test_chunk",
            workspace_id="test_ws",
            user_id="test_user",
            filename="test.txt",
            original_filename="test.txt",
            content_type="text/plain",
            size_bytes=1000,
            storage_backend="local",
            storage_path="/test/path",
            timestamp=datetime.now(UTC).isoformat() + "Z",
        )

        # Create long text that should be chunked
        long_text = "This is a test sentence. " * 100  # ~2500 chars

        chunks = processor._chunk_text(long_text, message)

        assert len(chunks) > 1
        # Verify chunk indices are sequential
        for i, chunk in enumerate(chunks):
            assert chunk.chunk_index == i
            assert chunk.document_id == message.document_id

    def test_chunk_by_sentences(self, test_settings: Settings):
        """Test chunking by sentences."""
        test_settings.chunking_strategy = "sentences"
        processor = DocumentProcessor(test_settings)
        processor._initialized = True

        message = DocumentUploadMessage(
            event_type="document.uploaded",
            document_id="test_sentences",
            workspace_id="test_ws",
            user_id="test_user",
            filename="test.txt",
            original_filename="test.txt",
            content_type="text/plain",
            size_bytes=100,
            storage_backend="local",
            storage_path="/test/path",
            timestamp=datetime.now(UTC).isoformat() + "Z",
        )

        text = "First sentence. Second sentence. Third sentence."
        # Force smaller chunk size
        processor.settings.max_chunk_size = 20

        chunks = processor._chunk_text(text, message)
        assert len(chunks) > 1

    def test_chunk_by_paragraphs(self, test_settings: Settings):
        """Test chunking by paragraphs."""
        test_settings.chunking_strategy = "paragraphs"
        processor = DocumentProcessor(test_settings)
        processor._initialized = True

        message = DocumentUploadMessage(
            event_type="document.uploaded",
            document_id="test_paragraphs",
            workspace_id="test_ws",
            user_id="test_user",
            filename="test.txt",
            original_filename="test.txt",
            content_type="text/plain",
            size_bytes=100,
            storage_backend="local",
            storage_path="/test/path",
            timestamp=datetime.now(UTC).isoformat() + "Z",
        )

        text = "Para 1.\n\nPara 2.\n\nPara 3."
        processor.settings.max_chunk_size = 10

        chunks = processor._chunk_text(text, message)
        assert len(chunks) == 3

    def test_short_text_single_chunk(self, test_settings: Settings):
        """Test that short text creates single chunk."""
        processor = DocumentProcessor(test_settings)
        processor._initialized = True

        message = DocumentUploadMessage(
            event_type="document.uploaded",
            document_id="test_short",
            workspace_id="test_ws",
            user_id="test_user",
            filename="test.txt",
            original_filename="test.txt",
            content_type="text/plain",
            size_bytes=50,
            storage_backend="local",
            storage_path="/test/path",
            timestamp=datetime.now(UTC).isoformat() + "Z",
        )

        short_text = "This is a short text."

        chunks = processor._chunk_text(short_text, message)

        assert len(chunks) == 1
        assert chunks[0].content == short_text

    def test_empty_text_no_chunks(self, test_settings: Settings):
        """Test that empty text creates no chunks."""
        processor = DocumentProcessor(test_settings)
        processor._initialized = True

        message = DocumentUploadMessage(
            event_type="document.uploaded",
            document_id="test_empty",
            workspace_id="test_ws",
            user_id="test_user",
            filename="test.txt",
            original_filename="test.txt",
            content_type="text/plain",
            size_bytes=1,  # Must be > 0 per schema validation
            storage_backend="local",
            storage_path="/test/path",
            timestamp=datetime.now(UTC).isoformat() + "Z",
        )

        chunks = processor._chunk_text("", message)

        assert len(chunks) == 0


class TestTextExtraction:
    """Tests for text extraction from different content types."""

    @pytest.mark.asyncio
    async def test_extract_text_from_plain_text(self, test_settings: Settings):
        """Test extracting text from plain text content."""
        processor = DocumentProcessor(test_settings)
        processor._initialized = True

        message = DocumentUploadMessage(
            event_type="document.uploaded",
            document_id="test_txt",
            workspace_id="test_ws",
            user_id="test_user",
            filename="test.txt",
            original_filename="test.txt",
            content_type="text/plain",
            size_bytes=100,
            storage_backend="local",
            storage_path="/test/path",
            timestamp=datetime.now(UTC).isoformat() + "Z",
        )

        content = b"This is plain text content."

        text = await processor._extract_text(content, message)

        assert text == "This is plain text content."

    @pytest.mark.asyncio
    async def test_extract_text_from_html(self, test_settings: Settings):
        """Test extracting text from HTML content."""
        processor = DocumentProcessor(test_settings)
        processor._initialized = True

        message = DocumentUploadMessage(
            event_type="document.uploaded",
            document_id="test_html",
            workspace_id="test_ws",
            user_id="test_user",
            filename="test.html",
            original_filename="test.html",
            content_type="text/html",
            size_bytes=200,
            storage_backend="local",
            storage_path="/test/path",
            timestamp=datetime.now(UTC).isoformat() + "Z",
        )

        content = b"<html><body><h1>Title</h1><p>Paragraph text.</p></body></html>"

        text = await processor._extract_text(content, message)

        assert "Title" in text
        assert "Paragraph text" in text
        assert "<html>" not in text  # HTML tags should be stripped

    @pytest.mark.asyncio
    async def test_extract_text_from_markdown(self, test_settings: Settings):
        """Test extracting text from Markdown content."""
        processor = DocumentProcessor(test_settings)
        processor._initialized = True

        message = DocumentUploadMessage(
            event_type="document.uploaded",
            document_id="test_md",
            workspace_id="test_ws",
            user_id="test_user",
            filename="test.md",
            original_filename="test.md",
            content_type="text/markdown",
            size_bytes=100,
            storage_backend="local",
            storage_path="/test/path",
            timestamp=datetime.now(UTC).isoformat() + "Z",
        )

        content = b"# Header\n\nThis is **bold** text."

        text = await processor._extract_text(content, message)

        assert "Header" in text
        assert "bold" in text

    @pytest.mark.asyncio
    async def test_extract_text_from_json(self, test_settings: Settings):
        """Test extracting text from JSON content."""
        processor = DocumentProcessor(test_settings)
        processor._initialized = True

        message = DocumentUploadMessage(
            event_type="document.uploaded",
            document_id="test_json",
            workspace_id="test_ws",
            user_id="test_user",
            filename="test.json",
            original_filename="test.json",
            content_type="application/json",
            size_bytes=100,
            storage_backend="local",
            storage_path="/test/path",
            timestamp=datetime.now(UTC).isoformat() + "Z",
        )

        content = b'{"key": "value"}'

        text = await processor._extract_text(content, message)

        assert '"key": "value"' in text


class TestProcessorWithMocks:
    """Tests for processor with mocked services."""

    @pytest.mark.asyncio
    async def test_process_message_full_flow_mocked(
        self,
        test_settings: Settings,
        sample_upload_message: dict,
    ):
        """Test full processing flow with mocked services."""
        processor = DocumentProcessor(test_settings)
        processor._initialized = True

        # Mock storage service to return test content
        mock_storage = MagicMock()
        mock_storage.read_file = MagicMock(return_value=b"Test document content for processing.")
        mock_storage.read_file_from_url = MagicMock(
            return_value=b"Test document content for processing."
        )
        processor.storage_service = mock_storage

        # Mock database service
        mock_db = MagicMock()
        mock_db.store_processed_document = AsyncMock(return_value=1)
        processor.db_service = mock_db

        # Mock weaviate service (optional)
        mock_weaviate = MagicMock()
        mock_weaviate.add_document_chunks = AsyncMock()
        mock_weaviate.store_chunks_with_tenant = AsyncMock(return_value=1)
        processor.weaviate_service = mock_weaviate

        # Mock tenant manager
        mock_tenant_manager = MagicMock()
        mock_tenant_manager.ensure_workspace_ready = AsyncMock(return_value=1)
        mock_tenant_manager.update_workspace_stats = AsyncMock()
        processor.tenant_manager = mock_tenant_manager

        result = await processor.process_message(sample_upload_message)

        assert result.success is True
        assert result.document_id == sample_upload_message["document_id"]
        assert result.chunks_created >= 1

        # Verify storage was called (either read_file or read_file_from_url)
        assert mock_storage.read_file.called or mock_storage.read_file_from_url.called

        # Verify database was called
        mock_db.store_processed_document.assert_called_once()

        # Verify tenant manager called
        mock_tenant_manager.ensure_workspace_ready.assert_called_once()

    @pytest.mark.asyncio
    async def test_process_message_storage_failure(
        self,
        test_settings: Settings,
        sample_upload_message: dict,
    ):
        """Test processing handles storage fetch failure."""
        processor = DocumentProcessor(test_settings)
        processor._initialized = True

        # Mock storage service to return None (fetch failed)
        mock_storage = MagicMock()
        mock_storage.read_file = MagicMock(return_value=None)
        mock_storage.read_file_from_url = MagicMock(return_value=None)
        processor.storage_service = mock_storage

        result = await processor.process_message(sample_upload_message)

        assert result.success is False
        assert "Failed to fetch document" in result.error

    @pytest.mark.asyncio
    async def test_process_message_continues_without_weaviate(
        self,
        test_settings: Settings,
        sample_upload_message: dict,
    ):
        """Test processing continues if Weaviate is unavailable."""
        processor = DocumentProcessor(test_settings)
        processor._initialized = True

        # Mock storage
        mock_storage = MagicMock()
        mock_storage.read_file = MagicMock(return_value=b"Test content.")
        mock_storage.read_file_from_url = MagicMock(return_value=b"Test content.")
        processor.storage_service = mock_storage

        # Mock database
        mock_db = MagicMock()
        mock_db.store_processed_document = AsyncMock(return_value=1)
        processor.db_service = mock_db

        # No weaviate service
        processor.weaviate_service = None

        # Mock tenant manager
        mock_tenant_manager = MagicMock()
        mock_tenant_manager.ensure_workspace_ready = AsyncMock(return_value=1)
        processor.tenant_manager = mock_tenant_manager

        result = await processor.process_message(sample_upload_message)

        # Should still succeed even without Weaviate
        assert result.success is True


class TestStoreDocumentFencedOutLogsLoudly:
    """(#110 follow-up review item 5) _store_document's workflow_run_id is a
    throwaway uuid4() (this legacy path has no real Temporal run concept) --
    which can NEVER match a prior claim, so store_processed_document is
    guaranteed to return None (fenced out) on a re-index of any document a
    real workflow run has already claimed. Discarding that None and logging
    "Stored in PostgreSQL" unconditionally would be a textbook
    silent-no-op-reporting-success landmine sitting in code someone may one
    day revive -- this pins that it fails loudly (an ERROR log naming the
    document_id) instead, by patching the module's logger directly rather
    than depending on structlog's global processor/handler wiring."""

    @pytest.mark.asyncio
    async def test_none_return_is_not_logged_as_success(self, test_settings: Settings):
        from unittest.mock import patch

        processor = DocumentProcessor(test_settings)
        processor._initialized = True

        mock_db = MagicMock()
        mock_db.store_processed_document = AsyncMock(return_value=None)  # fenced out
        processor.db_service = mock_db
        processor.weaviate_service = None  # isolate the PostgreSQL branch

        message = DocumentUploadMessage(
            event_type="document.uploaded",
            document_id="doc_fenced_out",
            workspace_id="ws_1",
            user_id="user_1",
            filename="f.txt",
            original_filename="f.txt",
            content_type="text/plain",
            size_bytes=10,
            storage_backend="local",
            storage_path="p",
            timestamp=datetime.now(UTC).isoformat(),
        )

        with patch("src.services.processor.logger") as mock_logger:
            await processor._store_document(
                message=message, chunks=[], text_length=0, processing_time_ms=1
            )

        # The misleading success log must never fire on a fenced-out write.
        success_calls = [
            c
            for c in mock_logger.info.call_args_list
            if c.args and c.args[0] == "Stored in PostgreSQL"
        ]
        assert success_calls == [], f"logged success on a fenced-out (None) write: {success_calls}"

        # It must instead be reported as a real error, naming the document.
        error_calls = [
            c
            for c in mock_logger.error.call_args_list
            if c.args and c.args[0] == "Failed to store in PostgreSQL"
        ]
        assert len(error_calls) == 1
        assert error_calls[0].kwargs["document_id"] == "doc_fenced_out"


class TestProcessorFetch:
    """Tests for document fetching."""

    @pytest.mark.asyncio
    async def test_fetch_local_with_url(self, test_settings: Settings):
        """Test fetching local document with URL."""
        processor = DocumentProcessor(test_settings)
        processor._initialized = True

        mock_storage = MagicMock()
        mock_storage.read_file_from_url = MagicMock(return_value=b"content")
        processor.storage_service = mock_storage

        message = DocumentUploadMessage(
            event_type="document.uploaded",
            document_id="doc1",
            workspace_id="ws1",
            user_id="u1",
            filename="file.txt",
            original_filename="file.txt",
            content_type="text/plain",
            size_bytes=10,
            storage_backend="local",
            storage_path="path",
            storage_url="http://url",
            timestamp=datetime.now(UTC).isoformat() + "Z",
        )

        content = await processor._fetch_document(message)
        assert content == b"content"
        mock_storage.read_file_from_url.assert_called_with("http://url")

    @pytest.mark.asyncio
    async def test_fetch_gcs(self, test_settings: Settings):
        """Test fetching GCS document."""
        processor = DocumentProcessor(test_settings)
        processor._initialized = True

        mock_storage = MagicMock()
        mock_storage.read_file = MagicMock(return_value=b"gcs content")
        processor.storage_service = mock_storage

        message = DocumentUploadMessage(
            event_type="document.uploaded",
            document_id="doc1",
            workspace_id="ws1",
            user_id="u1",
            filename="file.txt",
            original_filename="file.txt",
            content_type="text/plain",
            size_bytes=10,
            storage_backend="gcs",
            storage_path="path",
            storage_bucket="bucket",
            timestamp=datetime.now(UTC).isoformat() + "Z",
        )

        content = await processor._fetch_document(message)
        assert content == b"gcs content"
        mock_storage.read_file.assert_called_with(path="path", backend="gcs", bucket="bucket")

    @pytest.mark.asyncio
    async def test_fetch_unknown_backend(self, test_settings: Settings):
        """Test fetching with unknown backend."""
        processor = DocumentProcessor(test_settings)
        processor._initialized = True
        processor.storage_service = MagicMock()

        message = DocumentUploadMessage(
            event_type="document.uploaded",
            document_id="doc1",
            workspace_id="ws1",
            user_id="u1",
            filename="file.txt",
            original_filename="file.txt",
            content_type="text/plain",
            size_bytes=10,
            storage_backend="s3",  # S3 is valid in enum but not implemented fully in processor logic test
            storage_path="path",
            timestamp=datetime.now(UTC).isoformat() + "Z",
        )

        # Override enum validation for test
        message.storage_backend = "unknown"  # type: ignore

        content = await processor._fetch_document(message)
        assert content is None


class TestProcessorInternalError:
    """Tests for internal error handling."""

    @pytest.mark.asyncio
    async def test_process_message_validation_error(self, test_settings: Settings):
        """Test validation error handling."""
        processor = DocumentProcessor(test_settings)

        # Missing required fields
        result = await processor.process_message({})
        assert result.success is False
        assert "Invalid message format" in result.error

    @pytest.mark.asyncio
    async def test_process_message_exception(self, test_settings: Settings):
        """Test general exception handling during initialization."""
        processor = DocumentProcessor(test_settings)
        processor.initialize = MagicMock(side_effect=Exception("Init failed"))

        # Initialize happens outside try/except block in process_message
        with pytest.raises(Exception, match="Init failed"):
            await processor.process_message({"document_id": "doc1"})


class TestProcessorIntegration:
    """Integration tests for processor with real PostgreSQL."""

    @pytest.mark.asyncio
    async def test_process_and_verify_in_database(
        self,
        test_settings: Settings,
        db_service,
        sample_upload_message: dict,
    ):
        """Test processing and verifying data in real database."""
        processor = DocumentProcessor(test_settings)

        # Use real database service
        processor.db_service = db_service
        processor.weaviate_service = None
        processor._initialized = True

        # Mock tenant manager to use real DB service but skip Weaviate
        from src.services.tenant_manager import TenantManager

        tenant_manager = TenantManager(test_settings, db_service=db_service, weaviate_service=None)
        processor.tenant_manager = tenant_manager

        # Mock storage to return content
        mock_storage = MagicMock()
        mock_storage.read_file = MagicMock(
            return_value=b"This is test content for the integration test. " * 10
        )
        mock_storage.read_file_from_url = MagicMock(
            return_value=b"This is test content for the integration test. " * 10
        )
        processor.storage_service = mock_storage

        # Process message
        result = await processor.process_message(sample_upload_message)

        assert result.success is True

        # Verify in database
        doc = await db_service.get_document_status(sample_upload_message["document_id"])

        assert doc is not None
        assert doc["document_id"] == sample_upload_message["document_id"]
        assert doc["status"] == "processed"
        assert doc["chunk_count"] > 0

        # Verify chunks exist
        chunks = await db_service.get_document_chunks(sample_upload_message["document_id"])
        assert len(chunks) == doc["chunk_count"]

        # Verify foreign key relationship
        for chunk in chunks:
            assert chunk["processed_document_id"] == doc["id"]
