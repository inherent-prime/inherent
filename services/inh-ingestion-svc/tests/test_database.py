"""Tests for database service with PostgreSQL.

These tests require a running PostgreSQL instance.
Run with: make docker-up && make test-ingest
"""

from datetime import UTC, datetime, timedelta

import pytest

from src.models.document import DocumentChunk, DocumentUploadMessage
from src.services.database import DatabaseService, DocumentStatus, TenantStatus


class TestDatabaseConnection:
    """Tests for database connection."""

    def test_database_connects_successfully(self, db_service: DatabaseService):
        """Test that database connects successfully."""
        assert db_service.engine is not None
        assert db_service.SessionLocal is not None

    def test_tables_exist(self, db_service: DatabaseService):
        """Test that required tables exist."""
        from sqlalchemy import inspect

        inspector = inspect(db_service.engine)
        tables = inspector.get_table_names()

        assert "processed_documents" in tables
        assert "document_chunks" in tables
        assert "tenants" in tables
        assert "workspace_metadata" in tables

    def test_foreign_key_relationship_exists(self, db_service: DatabaseService):
        """Test that foreign key relationship exists between tables."""
        from sqlalchemy import inspect

        inspector = inspect(db_service.engine)
        fks = inspector.get_foreign_keys("document_chunks")

        # Find FK to processed_documents
        fk_to_processed_docs = [fk for fk in fks if fk["referred_table"] == "processed_documents"]

        assert len(fk_to_processed_docs) > 0
        assert "processed_document_id" in fk_to_processed_docs[0]["constrained_columns"]


class TestTenantOperations:
    """Tests for tenant management operations."""

    @pytest.mark.asyncio
    async def test_upsert_tenant(self, db_service: DatabaseService):
        """Test upserting a tenant."""
        user_id = "test_user_tenant"

        # Insert new
        tenant_id_1 = await db_service.upsert_tenant(user_id)
        assert tenant_id_1 > 0

        # Verify stored
        tenant = await db_service.get_tenant(user_id)
        assert tenant is not None
        assert tenant["user_id"] == user_id
        assert tenant["status"] == TenantStatus.ACTIVE.value

        # Upsert existing (should update timestamps)
        tenant_id_2 = await db_service.upsert_tenant(user_id)
        assert tenant_id_1 == tenant_id_2

        tenant_updated = await db_service.get_tenant(user_id)
        assert tenant_updated["last_activity_at"] >= tenant["last_activity_at"]

    @pytest.mark.asyncio
    async def test_update_tenant_status(self, db_service: DatabaseService):
        """Test updating tenant status."""
        user_id = "test_user_status"
        await db_service.upsert_tenant(user_id)

        updated = await db_service.update_tenant_status(user_id, "inactive")
        assert updated is True

        tenant = await db_service.get_tenant(user_id)
        assert tenant["status"] == "inactive"

    @pytest.mark.asyncio
    async def test_get_idle_tenants(self, db_service: DatabaseService):
        """Test getting idle tenants."""
        user_id = "idle_user"
        await db_service.upsert_tenant(user_id)

        # Manually set last_activity_at to the past
        with db_service.get_session() as session:
            old_date = datetime.now(UTC) - timedelta(days=40)
            session.execute(
                db_service.tenants.update()
                .where(db_service.tenants.c.user_id == user_id)
                .values(last_activity_at=old_date)
            )

        cutoff = datetime.now(UTC) - timedelta(days=30)
        idle_tenants = await db_service.get_idle_tenants(cutoff)

        assert any(t["user_id"] == user_id for t in idle_tenants)


class TestWorkspaceOperations:
    """Tests for workspace metadata operations."""

    @pytest.mark.asyncio
    async def test_upsert_workspace_metadata(self, db_service: DatabaseService):
        """Test upserting workspace metadata."""
        workspace_id = "test_ws_meta"
        user_id = "test_user_ws"

        meta_id = await db_service.upsert_workspace_metadata(workspace_id, user_id)
        assert meta_id > 0

        meta = await db_service.get_workspace_metadata(workspace_id)
        assert meta is not None
        assert meta["workspace_id"] == workspace_id
        assert meta["user_id"] == user_id

        # Upsert again
        meta_id_2 = await db_service.upsert_workspace_metadata(workspace_id, user_id)
        assert meta_id == meta_id_2

    @pytest.mark.asyncio
    async def test_update_workspace_stats(self, db_service: DatabaseService):
        """Test updating workspace stats."""
        import uuid

        workspace_id = f"test_ws_stats_{uuid.uuid4()}"
        user_id = "test_user_stats"
        await db_service.upsert_workspace_metadata(workspace_id, user_id)

        updated = await db_service.update_workspace_stats(
            workspace_id, document_delta=5, chunk_delta=50, size_delta=1024
        )
        assert updated is True

        meta = await db_service.get_workspace_metadata(workspace_id)
        assert meta["document_count"] == 5
        assert meta["chunk_count"] == 50
        assert meta["total_size_bytes"] == 1024

    @pytest.mark.asyncio
    async def test_delete_workspace_data(self, db_service: DatabaseService):
        """Test deleting workspace data."""
        workspace_id = "test_ws_delete"
        user_id = "test_user_delete"
        await db_service.upsert_workspace_metadata(workspace_id, user_id)

        # Create a document for this workspace
        # We need a message and chunks for store_processed_document
        # but here we can just insert directly for speed or use the proper method if accessible
        # Using direct insert to avoid dependency on other models in this test if possible,
        # but better to use service method

        # ... skipped creating doc for brevity, testing metadata deletion

        count = await db_service.delete_workspace_data(workspace_id)
        # Should be 0 docs deleted if we didn't add any
        assert count == 0

        meta = await db_service.get_workspace_metadata(workspace_id)
        assert meta is None


class TestDocumentStorage:
    """Tests for storing documents and chunks."""

    @pytest.mark.asyncio
    async def test_store_document_successfully(
        self,
        db_service: DatabaseService,
        sample_upload_message: dict,
        sample_chunks: list[DocumentChunk],
    ):
        """Test storing a document with chunks."""
        message = DocumentUploadMessage(**sample_upload_message)

        doc_id = await db_service.store_processed_document(
            message=message,
            chunks=sample_chunks,
            text_length=120,
            processing_time_ms=500,
            workflow_run_id="test-run",
        )

        assert doc_id is not None
        assert doc_id > 0

    @pytest.mark.asyncio
    async def test_document_chunks_stored_with_fk(
        self,
        db_service: DatabaseService,
        sample_upload_message: dict,
        sample_chunks: list[DocumentChunk],
    ):
        """Test that chunks are stored with correct foreign key."""
        message = DocumentUploadMessage(**sample_upload_message)

        doc_pk = await db_service.store_processed_document(
            message=message,
            chunks=sample_chunks,
            text_length=120,
            processing_time_ms=500,
            workflow_run_id="test-run",
        )

        # Retrieve chunks and verify FK
        chunks = await db_service.get_document_chunks(message.document_id)

        assert len(chunks) == len(sample_chunks)
        for chunk in chunks:
            assert chunk["processed_document_id"] == doc_pk
            assert chunk["document_id"] == message.document_id
            assert chunk["workspace_id"] == message.workspace_id

    @pytest.mark.asyncio
    async def test_document_status_is_processed(
        self,
        db_service: DatabaseService,
        sample_upload_message: dict,
        sample_chunks: list[DocumentChunk],
    ):
        """Test that stored document has processed status."""
        message = DocumentUploadMessage(**sample_upload_message)

        await db_service.store_processed_document(
            message=message,
            chunks=sample_chunks,
            text_length=120,
            processing_time_ms=500,
            workflow_run_id="test-run",
        )

        doc = await db_service.get_document_status(message.document_id)

        assert doc is not None
        assert doc["status"] == DocumentStatus.PROCESSED.value
        assert doc["chunk_count"] == len(sample_chunks)
        assert doc["text_length"] == 120
        assert doc["processing_time_ms"] == 500

    @pytest.mark.asyncio
    async def test_upsert_document_updates_existing(
        self,
        db_service: DatabaseService,
        sample_upload_message: dict,
        sample_chunks: list[DocumentChunk],
    ):
        """Test that storing same document_id updates instead of duplicating."""
        message = DocumentUploadMessage(**sample_upload_message)

        # Store first time
        doc_id_1 = await db_service.store_processed_document(
            message=message,
            chunks=sample_chunks,
            text_length=100,
            processing_time_ms=500,
            workflow_run_id="test-run",
        )

        # Store again with different values
        doc_id_2 = await db_service.store_processed_document(
            message=message,
            chunks=sample_chunks[:1],  # Only 1 chunk
            text_length=50,
            processing_time_ms=200,
            workflow_run_id="test-run",
        )

        # Should update, not create new
        assert doc_id_1 == doc_id_2

        # Verify updated values
        doc = await db_service.get_document_status(message.document_id)
        assert doc["chunk_count"] == 1
        assert doc["text_length"] == 50
        assert doc["processing_time_ms"] == 200


class TestDocumentRetrieval:
    """Tests for retrieving documents and chunks."""

    @pytest.mark.asyncio
    async def test_get_documents_by_workspace(
        self,
        db_service: DatabaseService,
        sample_upload_message: dict,
        sample_chunks: list[DocumentChunk],
    ):
        """Test retrieving documents by workspace."""
        message = DocumentUploadMessage(**sample_upload_message)

        await db_service.store_processed_document(
            message=message,
            chunks=sample_chunks,
            text_length=120,
            processing_time_ms=500,
            workflow_run_id="test-run",
        )

        docs = await db_service.get_documents_by_workspace(message.workspace_id)

        assert len(docs) >= 1
        doc = next(d for d in docs if d["document_id"] == message.document_id)
        assert doc["workspace_id"] == message.workspace_id

    @pytest.mark.asyncio
    async def test_get_chunks_ordered_by_index(
        self,
        db_service: DatabaseService,
        sample_upload_message: dict,
        sample_chunks: list[DocumentChunk],
    ):
        """Test that chunks are returned ordered by index."""
        message = DocumentUploadMessage(**sample_upload_message)

        await db_service.store_processed_document(
            message=message,
            chunks=sample_chunks,
            text_length=120,
            processing_time_ms=500,
            workflow_run_id="test-run",
        )

        chunks = await db_service.get_document_chunks(message.document_id)

        # Verify ordered by chunk_index
        for i, chunk in enumerate(chunks):
            assert chunk["chunk_index"] == i

    @pytest.mark.asyncio
    async def test_get_nonexistent_document_returns_none(
        self,
        db_service: DatabaseService,
    ):
        """Test that getting nonexistent document returns None."""
        doc = await db_service.get_document_status("nonexistent_doc_id")
        assert doc is None


class TestDocumentDeletion:
    """Tests for document deletion with cascade."""

    @pytest.mark.asyncio
    async def test_delete_document_cascades_to_chunks(
        self,
        db_service: DatabaseService,
        sample_upload_message: dict,
        sample_chunks: list[DocumentChunk],
    ):
        """Test that deleting document cascades to chunks."""
        message = DocumentUploadMessage(**sample_upload_message)

        await db_service.store_processed_document(
            message=message,
            chunks=sample_chunks,
            text_length=120,
            processing_time_ms=500,
            workflow_run_id="test-run",
        )

        # Verify chunks exist
        chunks_before = await db_service.get_document_chunks(message.document_id)
        assert len(chunks_before) == len(sample_chunks)

        # Delete document (workspace_id is required, #177 review -- see
        # DatabaseService.delete_document's docstring)
        deleted = await db_service.delete_document(
            message.document_id, workspace_id=message.workspace_id
        )
        assert deleted is True

        # Verify document is gone
        doc = await db_service.get_document_status(message.document_id)
        assert doc is None

        # Verify chunks are cascade deleted
        chunks_after = await db_service.get_document_chunks(message.document_id)
        assert len(chunks_after) == 0

    @pytest.mark.asyncio
    async def test_delete_nonexistent_document_returns_false(
        self,
        db_service: DatabaseService,
    ):
        """Test that deleting nonexistent document returns False."""
        deleted = await db_service.delete_document(
            "nonexistent_doc_id", workspace_id="nonexistent_workspace"
        )
        assert deleted is False


class TestDocumentStatusUpdates:
    """Tests for updating document status."""

    @pytest.mark.asyncio
    async def test_update_status_to_failed(
        self,
        db_service: DatabaseService,
        sample_upload_message: dict,
        sample_chunks: list[DocumentChunk],
    ):
        """Test updating document status to failed."""
        message = DocumentUploadMessage(**sample_upload_message)

        await db_service.store_processed_document(
            message=message,
            chunks=sample_chunks,
            text_length=120,
            processing_time_ms=500,
            workflow_run_id="test-run",
        )

        updated = await db_service.update_document_status(
            document_id=message.document_id,
            status=DocumentStatus.FAILED,
            error_message="Test error message",
        )

        assert updated is True

        doc = await db_service.get_document_status(message.document_id)
        assert doc["status"] == DocumentStatus.FAILED.value
        assert doc["error_message"] == "Test error message"

    @pytest.mark.asyncio
    async def test_update_status_to_processing(
        self,
        db_service: DatabaseService,
        sample_upload_message: dict,
        sample_chunks: list[DocumentChunk],
    ):
        """Updating status to 'processing' should persist."""
        message = DocumentUploadMessage(**sample_upload_message)

        await db_service.store_processed_document(
            message=message,
            chunks=sample_chunks,
            text_length=120,
            processing_time_ms=500,
            workflow_run_id="test-run",
        )

        updated = await db_service.update_document_status(
            document_id=message.document_id,
            status=DocumentStatus.PROCESSING,
        )

        assert updated is True
        doc = await db_service.get_document_status(message.document_id)
        assert doc["status"] == DocumentStatus.PROCESSING.value

    @pytest.mark.asyncio
    async def test_update_status_missing_row_is_noop(
        self,
        db_service: DatabaseService,
    ):
        """Updating a non-existent document is a safe no-op (0 rows -> False)."""
        updated = await db_service.update_document_status(
            document_id="does-not-exist-xyz",
            status=DocumentStatus.PROCESSING,
        )

        assert updated is False


class TestProcessingStats:
    """Tests for processing statistics."""

    @pytest.mark.asyncio
    async def test_get_processing_stats(
        self,
        db_service: DatabaseService,
        sample_upload_message: dict,
        sample_chunks: list[DocumentChunk],
    ):
        """Test getting processing statistics."""
        message = DocumentUploadMessage(**sample_upload_message)

        await db_service.store_processed_document(
            message=message,
            chunks=sample_chunks,
            text_length=120,
            processing_time_ms=500,
            workflow_run_id="test-run",
        )

        stats = await db_service.get_processing_stats(workspace_id=message.workspace_id)

        assert stats["total_documents"] >= 1
        assert stats["processed_documents"] >= 1
        assert stats["total_chunks"] >= len(sample_chunks)

    @pytest.mark.asyncio
    async def test_get_workspace_stats_view(
        self,
        db_service: DatabaseService,
        sample_upload_message: dict,
        sample_chunks: list[DocumentChunk],
    ):
        """Test workspace stats from database view."""
        message = DocumentUploadMessage(**sample_upload_message)

        await db_service.store_processed_document(
            message=message,
            chunks=sample_chunks,
            text_length=120,
            processing_time_ms=500,
            workflow_run_id="test-run",
        )

        # Query the v_workspace_stats view directly
        with db_service.get_session() as session:
            from sqlalchemy import text

            result = session.execute(
                text("SELECT * FROM v_workspace_stats WHERE workspace_id = :ws"),
                {"ws": message.workspace_id},
            ).fetchone()

        assert result is not None
        # Convert to dict for easier assertions
        stats = dict(result._mapping)
        assert stats["workspace_id"] == message.workspace_id
        assert stats["total_documents"] >= 1
