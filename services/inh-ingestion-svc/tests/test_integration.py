"""End-to-end integration tests for the ingestion service.

These tests verify the complete flow from message receipt to database storage.
Requires running PostgreSQL and optionally Weaviate.

Run with: make docker-up && make test-ingest
"""

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from src.config.settings import Settings
from src.services.database import DatabaseService, DocumentStatus
from src.services.processor import DocumentProcessor


class TestEndToEndFlow:
    """End-to-end integration tests."""

    @pytest.mark.asyncio
    async def test_complete_document_processing_flow(
        self,
        test_settings: Settings,
        db_service: DatabaseService,
    ):
        """Test complete flow: message -> process -> store -> retrieve."""
        # Create unique test identifiers
        test_id = f"e2e_test_{datetime.now(UTC).strftime('%Y%m%d%H%M%S%f')}"

        message_data = {
            "event_type": "document.uploaded",
            "document_id": test_id,
            "workspace_id": "test_e2e_workspace",
            "user_id": "test_e2e_user",
            "filename": f"{test_id}.txt",
            "original_filename": "end_to_end_test.txt",
            "content_type": "text/plain",
            "size_bytes": 500,
            "storage_backend": "local",
            "storage_path": f"workspaces/test_e2e_workspace/{test_id}.txt",
            "storage_bucket": None,
            "storage_url": None,
            "timestamp": datetime.now(UTC).isoformat() + "Z",
        }

        # Create processor with real DB
        processor = DocumentProcessor(test_settings)
        processor.db_service = db_service
        processor.weaviate_service = None
        processor._initialized = True

        # Mock storage to return realistic content
        test_content = """
        # Document Title

        This is the introduction paragraph. It contains important information
        about the document's purpose and scope.

        ## Section 1: Overview

        The overview section provides a high-level summary of the content.
        We discuss various topics including technology, processes, and outcomes.

        ## Section 2: Details

        This section goes into more detail about specific aspects.
        Each paragraph contains relevant information that should be captured.

        The document processor should extract this text, chunk it appropriately,
        and store it in the database for later retrieval and search.

        ## Conclusion

        In conclusion, this test document demonstrates the end-to-end flow
        of the ingestion service from message receipt to database storage.
        """

        mock_storage = MagicMock()
        mock_storage.read_file = MagicMock(return_value=test_content.encode())
        mock_storage.read_file_from_url = MagicMock(return_value=test_content.encode())
        processor.storage_service = mock_storage

        # Step 1: Process the message
        result = await processor.process_message(message_data)

        # Verify processing success
        assert result.success is True, f"Processing failed: {result.error}"
        assert result.chunks_created > 0

        # Step 2: Verify document stored in database
        doc = await db_service.get_document_status(test_id)

        assert doc is not None
        assert doc["document_id"] == test_id
        assert doc["workspace_id"] == "test_e2e_workspace"
        assert doc["user_id"] == "test_e2e_user"
        assert doc["status"] == DocumentStatus.PROCESSED.value
        assert doc["chunk_count"] == result.chunks_created
        assert doc["text_length"] > 0
        assert doc["processing_time_ms"] >= 0  # Can be 0 if processing is very fast

        # Step 3: Verify chunks stored with FK relationship
        chunks = await db_service.get_document_chunks(test_id)

        assert len(chunks) == doc["chunk_count"]

        for i, chunk in enumerate(chunks):
            # Verify FK relationship
            assert chunk["processed_document_id"] == doc["id"]
            # Verify chunk ordering
            assert chunk["chunk_index"] == i
            # Verify workspace denormalization
            assert chunk["workspace_id"] == "test_e2e_workspace"
            # Verify content is not empty
            assert len(chunk["content"]) > 0

        # Step 4: Verify workspace query works
        workspace_docs = await db_service.get_documents_by_workspace("test_e2e_workspace")

        assert any(d["document_id"] == test_id for d in workspace_docs)

        # Step 5: Verify statistics
        stats = await db_service.get_processing_stats(workspace_id="test_e2e_workspace")

        assert stats["total_documents"] >= 1
        assert stats["processed_documents"] >= 1
        assert stats["total_chunks"] >= result.chunks_created

    @pytest.mark.asyncio
    async def test_reprocessing_updates_document(
        self,
        test_settings: Settings,
        db_service: DatabaseService,
    ):
        """Test that reprocessing same document updates instead of duplicating."""
        test_id = f"reprocess_test_{datetime.now(UTC).strftime('%Y%m%d%H%M%S%f')}"

        message_data = {
            "event_type": "document.uploaded",
            "document_id": test_id,
            "workspace_id": "test_reprocess_workspace",
            "user_id": "test_user",
            "filename": f"{test_id}.txt",
            "original_filename": "reprocess_test.txt",
            "content_type": "text/plain",
            "size_bytes": 100,
            "storage_backend": "local",
            "storage_path": f"workspaces/test/{test_id}.txt",
            "storage_bucket": None,
            "storage_url": None,
            "timestamp": datetime.now(UTC).isoformat() + "Z",
        }

        processor = DocumentProcessor(test_settings)
        processor.db_service = db_service
        processor.weaviate_service = None
        processor._initialized = True

        # First processing
        mock_storage = MagicMock()
        mock_storage.read_file = MagicMock(return_value=b"First version of content.")
        mock_storage.read_file_from_url = MagicMock(return_value=b"First version of content.")
        processor.storage_service = mock_storage

        result1 = await processor.process_message(message_data)
        assert result1.success is True

        doc1 = await db_service.get_document_status(test_id)
        first_id = doc1["id"]

        # Second processing with different content
        mock_storage.read_file = MagicMock(return_value=b"Second version with more content. " * 20)
        mock_storage.read_file_from_url = MagicMock(
            return_value=b"Second version with more content. " * 20
        )

        result2 = await processor.process_message(message_data)
        assert result2.success is True

        doc2 = await db_service.get_document_status(test_id)

        # Should update, not create new document
        assert doc2["id"] == first_id
        # Chunk count may differ due to different content
        assert doc2["chunk_count"] >= 1

        # Verify old chunks were replaced
        chunks = await db_service.get_document_chunks(test_id)
        assert len(chunks) == doc2["chunk_count"]

        # All chunks should have FK to same document
        for chunk in chunks:
            assert chunk["processed_document_id"] == first_id

    @pytest.mark.asyncio
    async def test_cascade_delete_removes_chunks(
        self,
        test_settings: Settings,
        db_service: DatabaseService,
    ):
        """Test that deleting document cascades to remove chunks."""
        test_id = f"cascade_test_{datetime.now(UTC).strftime('%Y%m%d%H%M%S%f')}"

        message_data = {
            "event_type": "document.uploaded",
            "document_id": test_id,
            "workspace_id": "test_cascade_workspace",
            "user_id": "test_user",
            "filename": f"{test_id}.txt",
            "original_filename": "cascade_test.txt",
            "content_type": "text/plain",
            "size_bytes": 200,
            "storage_backend": "local",
            "storage_path": f"workspaces/test/{test_id}.txt",
            "storage_bucket": None,
            "storage_url": None,
            "timestamp": datetime.now(UTC).isoformat() + "Z",
        }

        processor = DocumentProcessor(test_settings)
        processor.db_service = db_service
        processor.weaviate_service = None
        processor._initialized = True

        mock_storage = MagicMock()
        mock_storage.read_file = MagicMock(return_value=b"Content for cascade delete test. " * 10)
        mock_storage.read_file_from_url = MagicMock(
            return_value=b"Content for cascade delete test. " * 10
        )
        processor.storage_service = mock_storage

        # Process document
        result = await processor.process_message(message_data)
        assert result.success is True

        # Verify document and chunks exist
        doc = await db_service.get_document_status(test_id)
        chunks_before = await db_service.get_document_chunks(test_id)

        assert doc is not None
        assert len(chunks_before) > 0

        doc_id = doc["id"]

        # Delete document (workspace_id is required, #177 review -- see
        # DatabaseService.delete_document's docstring)
        deleted = await db_service.delete_document(
            test_id, workspace_id=message_data["workspace_id"]
        )
        assert deleted is True

        # Verify document is gone
        doc_after = await db_service.get_document_status(test_id)
        assert doc_after is None

        # Verify chunks are cascade deleted
        chunks_after = await db_service.get_document_chunks(test_id)
        assert len(chunks_after) == 0

        # Verify via raw SQL that FK cascaded
        with db_service.get_session() as session:
            from sqlalchemy import text

            result = session.execute(
                text("SELECT COUNT(*) FROM document_chunks WHERE processed_document_id = :id"),
                {"id": doc_id},
            ).scalar()
            assert result == 0


class TestContentTypes:
    """Test processing different content types."""

    @pytest.mark.asyncio
    async def test_process_pdf_content_type(
        self,
        test_settings: Settings,
        db_service: DatabaseService,
    ):
        """Test processing PDF content type (mocked content)."""
        test_id = f"pdf_test_{datetime.now(UTC).strftime('%Y%m%d%H%M%S%f')}"

        message_data = {
            "event_type": "document.uploaded",
            "document_id": test_id,
            "workspace_id": "test_pdf_workspace",
            "user_id": "test_user",
            "filename": f"{test_id}.pdf",
            "original_filename": "test_document.pdf",
            "content_type": "application/pdf",
            "size_bytes": 1024,
            "storage_backend": "local",
            "storage_path": f"workspaces/test/{test_id}.pdf",
            "storage_bucket": None,
            "storage_url": None,
            "timestamp": datetime.now(UTC).isoformat() + "Z",
        }

        processor = DocumentProcessor(test_settings)
        processor.db_service = db_service
        processor.weaviate_service = None
        processor._initialized = True

        # For PDF, we'd need real PDF bytes or skip actual extraction
        # Here we test that the flow handles PDF content type
        mock_storage = MagicMock()
        # Return plain text (simulating extracted PDF content)
        mock_storage.read_file = MagicMock(return_value=b"Extracted PDF content for testing.")
        mock_storage.read_file_from_url = MagicMock(
            return_value=b"Extracted PDF content for testing."
        )
        processor.storage_service = mock_storage

        await processor.process_message(message_data)

        # Even without real PDF extraction, the flow should handle it
        # (may extract empty text or raw bytes as text)
        doc = await db_service.get_document_status(test_id)
        assert doc["content_type"] == "application/pdf"

    @pytest.mark.asyncio
    async def test_process_html_content_type(
        self,
        test_settings: Settings,
        db_service: DatabaseService,
    ):
        """Test processing HTML content type."""
        test_id = f"html_test_{datetime.now(UTC).strftime('%Y%m%d%H%M%S%f')}"

        message_data = {
            "event_type": "document.uploaded",
            "document_id": test_id,
            "workspace_id": "test_html_workspace",
            "user_id": "test_user",
            "filename": f"{test_id}.html",
            "original_filename": "webpage.html",
            "content_type": "text/html",
            "size_bytes": 500,
            "storage_backend": "local",
            "storage_path": f"workspaces/test/{test_id}.html",
            "storage_bucket": None,
            "storage_url": None,
            "timestamp": datetime.now(UTC).isoformat() + "Z",
        }

        processor = DocumentProcessor(test_settings)
        processor.db_service = db_service
        processor.weaviate_service = None
        processor._initialized = True

        html_content = b"""
        <!DOCTYPE html>
        <html>
        <head><title>Test Page</title></head>
        <body>
            <h1>Main Heading</h1>
            <p>This is a paragraph of text.</p>
            <p>Another paragraph with more content.</p>
        </body>
        </html>
        """

        mock_storage = MagicMock()
        mock_storage.read_file = MagicMock(return_value=html_content)
        mock_storage.read_file_from_url = MagicMock(return_value=html_content)
        processor.storage_service = mock_storage

        result = await processor.process_message(message_data)

        assert result.success is True

        # Verify HTML was processed
        doc = await db_service.get_document_status(test_id)
        assert doc is not None
        assert doc["text_length"] > 0

        # Verify text was extracted (not raw HTML)
        chunks = await db_service.get_document_chunks(test_id)
        if chunks:
            # Content should not contain HTML tags
            combined_content = " ".join(c["content"] for c in chunks)
            assert "<html>" not in combined_content
            assert "Main Heading" in combined_content or "paragraph" in combined_content


class TestErrorHandling:
    """Test error handling in processing flow."""

    @pytest.mark.asyncio
    async def test_storage_fetch_failure_handled(
        self,
        test_settings: Settings,
        db_service: DatabaseService,
    ):
        """Test that storage fetch failure is handled gracefully."""
        test_id = f"fetch_fail_{datetime.now(UTC).strftime('%Y%m%d%H%M%S%f')}"

        message_data = {
            "event_type": "document.uploaded",
            "document_id": test_id,
            "workspace_id": "test_workspace",
            "user_id": "test_user",
            "filename": "test.txt",
            "original_filename": "test.txt",
            "content_type": "text/plain",
            "size_bytes": 100,
            "storage_backend": "local",
            "storage_path": "/nonexistent/path",
            "storage_bucket": None,
            "storage_url": None,
            "timestamp": datetime.now(UTC).isoformat() + "Z",
        }

        processor = DocumentProcessor(test_settings)
        processor.db_service = db_service
        processor.weaviate_service = None
        processor._initialized = True

        # Mock storage to return None (fetch failed)
        mock_storage = MagicMock()
        mock_storage.read_file = MagicMock(return_value=None)
        mock_storage.read_file_from_url = MagicMock(return_value=None)
        processor.storage_service = mock_storage

        result = await processor.process_message(message_data)

        assert result.success is False
        assert "Failed to fetch" in result.error

    @pytest.mark.asyncio
    async def test_invalid_message_handled(
        self,
        test_settings: Settings,
        db_service: DatabaseService,
    ):
        """Test that invalid message is handled gracefully."""
        processor = DocumentProcessor(test_settings)
        processor.db_service = db_service
        processor._initialized = True

        invalid_message = {
            "event_type": "wrong.event",
            "some_field": "value",
        }

        result = await processor.process_message(invalid_message)

        assert result.success is False
        assert "Invalid message format" in result.error
