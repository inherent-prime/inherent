"""Extended tests for Database service."""

from datetime import UTC, datetime

import pytest

from src.services.database import DatabaseService, DocumentStatus


class TestDatabaseServiceExtended:
    """Extended tests for DatabaseService."""

    @pytest.mark.asyncio
    async def test_get_documents_by_tenant(self, db_service: DatabaseService):
        """Test getting documents by tenant."""
        # Setup data
        tenant_id = await db_service.upsert_tenant("user_tenant_test")

        # Insert a document manually or via helper
        from src.models.document import DocumentUploadMessage

        msg = DocumentUploadMessage(
            event_type="document.uploaded",
            document_id="doc_tenant_test",
            workspace_id="ws_tenant",
            user_id="user_tenant_test",
            filename="file.txt",
            original_filename="file.txt",
            content_type="text/plain",
            size_bytes=100,
            storage_backend="local",
            storage_path="path",
            timestamp=datetime.now(UTC).isoformat() + "Z",
        )

        await db_service.store_processed_document(
            message=msg,
            chunks=[],
            text_length=0,
            processing_time_ms=0,
            tenant_id=tenant_id,
            workflow_run_id="test-run",
        )

        # Test get_documents_by_tenant
        docs = await db_service.get_documents_by_tenant(tenant_id)
        assert len(docs) == 1
        assert docs[0]["document_id"] == "doc_tenant_test"

        # Test filters
        docs_ws = await db_service.get_documents_by_tenant(tenant_id, workspace_id="ws_tenant")
        assert len(docs_ws) == 1

        docs_status = await db_service.get_documents_by_tenant(
            tenant_id, status=DocumentStatus.PROCESSED
        )
        assert len(docs_status) == 1

    @pytest.mark.asyncio
    async def test_get_chunks_by_workspace(self, db_service: DatabaseService):
        """Test getting chunks by workspace."""
        # Insert doc and chunks
        from src.models.document import DocumentChunk, DocumentUploadMessage

        msg = DocumentUploadMessage(
            event_type="document.uploaded",
            document_id="doc_chunks_test",
            workspace_id="ws_chunks",
            user_id="user_chunks",
            filename="file.txt",
            original_filename="file.txt",
            content_type="text/plain",
            size_bytes=100,
            storage_backend="local",
            storage_path="path",
            timestamp=datetime.now(UTC).isoformat() + "Z",
        )

        chunks = [
            DocumentChunk(document_id="doc_chunks_test", content="c1", chunk_index=0),
            DocumentChunk(document_id="doc_chunks_test", content="c2", chunk_index=1),
        ]

        await db_service.store_processed_document(
            message=msg,
            chunks=chunks,
            text_length=4,
            processing_time_ms=0,
            workflow_run_id="test-run",
        )

        # Test get_chunks_by_workspace
        fetched_chunks = await db_service.get_chunks_by_workspace("ws_chunks")
        assert len(fetched_chunks) == 2
        assert fetched_chunks[0]["content"] == "c1"

    @pytest.mark.asyncio
    async def test_search_chunks(self, db_service: DatabaseService):
        """Test full-text search in PostgreSQL."""
        # Insert searchable chunks
        from src.models.document import DocumentChunk, DocumentUploadMessage

        msg = DocumentUploadMessage(
            event_type="document.uploaded",
            document_id="doc_search_test",
            workspace_id="ws_search",
            user_id="user_search",
            filename="file.txt",
            original_filename="file.txt",
            content_type="text/plain",
            size_bytes=100,
            storage_backend="local",
            storage_path="path",
            timestamp=datetime.now(UTC).isoformat() + "Z",
        )

        chunks = [
            DocumentChunk(
                document_id="doc_search_test", content="The quick brown fox", chunk_index=0
            ),
            DocumentChunk(
                document_id="doc_search_test", content="jumps over the lazy dog", chunk_index=1
            ),
        ]

        await db_service.store_processed_document(
            message=msg,
            chunks=chunks,
            text_length=50,
            processing_time_ms=0,
            workflow_run_id="test-run",
        )

        # Search
        results = await db_service.search_chunks("ws_search", "fox")
        assert len(results) == 1
        assert results[0]["content"] == "The quick brown fox"

    @pytest.mark.asyncio
    async def test_get_tenant_stats(self, db_service: DatabaseService):
        """Test getting tenant stats."""
        tenant_id = await db_service.upsert_tenant("user_stats_test")

        # Insert doc
        from src.models.document import DocumentUploadMessage

        msg = DocumentUploadMessage(
            event_type="document.uploaded",
            document_id="doc_stats_test",
            workspace_id="ws_stats",
            user_id="user_stats_test",
            filename="file.txt",
            original_filename="file.txt",
            content_type="text/plain",
            size_bytes=100,
            storage_backend="local",
            storage_path="path",
            timestamp=datetime.now(UTC).isoformat() + "Z",
        )

        await db_service.store_processed_document(
            message=msg,
            chunks=[],
            text_length=0,
            processing_time_ms=0,
            tenant_id=tenant_id,
            workflow_run_id="test-run",
        )

        stats = await db_service.get_tenant_stats(tenant_id)
        assert stats["total_documents"] == 1
        assert stats["processed_documents"] == 1
        assert stats["workspace_count"] == 1

    @pytest.mark.asyncio
    async def test_delete_workspace_documents(self, db_service: DatabaseService):
        """Test deleting workspace documents."""
        # Insert doc
        from src.models.document import DocumentUploadMessage

        msg = DocumentUploadMessage(
            event_type="document.uploaded",
            document_id="doc_del_ws_test",
            workspace_id="ws_del_test",
            user_id="user_del",
            filename="file.txt",
            original_filename="file.txt",
            content_type="text/plain",
            size_bytes=100,
            storage_backend="local",
            storage_path="path",
            timestamp=datetime.now(UTC).isoformat() + "Z",
        )

        await db_service.store_processed_document(
            message=msg,
            chunks=[],
            text_length=0,
            processing_time_ms=0,
            workflow_run_id="test-run",
        )

        count = await db_service.delete_workspace_documents("ws_del_test")
        assert count == 1

        doc = await db_service.get_document_status("doc_del_ws_test")
        assert doc is None
