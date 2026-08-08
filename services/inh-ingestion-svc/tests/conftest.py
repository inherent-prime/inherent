"""Pytest configuration and fixtures for integration tests."""

import asyncio
import os
from collections.abc import Generator
from datetime import UTC, datetime

import pytest

from src.config.settings import Settings
from src.models.document import DocumentChunk
from src.services.database import DatabaseService

# Use local Docker PostgreSQL for testing
TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/knowledge_base"
)
TEST_WEAVIATE_URL = os.getenv("TEST_WEAVIATE_URL", "http://localhost:8080")


@pytest.fixture(scope="session")
def event_loop():
    """Create event loop for async tests."""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(scope="session")
def test_settings() -> Settings:
    """Create test settings pointing to local Docker services."""
    return Settings(
        DATABASE_URL=TEST_DATABASE_URL,
        WEAVIATE_URL=TEST_WEAVIATE_URL,
        WEAVIATE_API_KEY="",
        GCP_PROJECT_ID="test-project",
        STORAGE_BUCKET="test-bucket",
        PUBSUB_SUBSCRIPTION="projects/test-project/subscriptions/test-sub",
        STORAGE_BACKEND="local",
        INTG_SERVICE_URL="http://localhost:4000",
        MAX_CHUNK_SIZE=500,
        CHUNK_OVERLAP=50,
    )


@pytest.fixture(scope="function")
def db_service(test_settings: Settings) -> Generator[DatabaseService, None, None]:
    """Create and connect database service for each test.

    Skips the test if PostgreSQL is not available (e.g. local dev without Docker).
    """
    service = DatabaseService(test_settings)
    try:
        service.connect()
        service.ensure_schema()
    except Exception:
        pytest.skip("PostgreSQL not available")
    yield service
    service.disconnect()


@pytest.fixture
def sample_upload_message() -> dict:
    """Create a sample document upload message for plain text."""
    return {
        "event_type": "document.uploaded",
        "document_id": "test_doc_12345",
        "workspace_id": "test_workspace_001",
        "user_id": "test_user_001",
        "filename": "1234567890-abc12345-test.txt",
        "original_filename": "test_document.txt",
        "content_type": "text/plain",  # Changed to text/plain for easier testing
        "size_bytes": 1024,
        "storage_backend": "local",
        "storage_path": "workspaces/test_workspace_001/test.txt",
        "storage_bucket": None,
        "storage_url": "http://localhost:4000/api/v1/storage/documents/test.txt",
        "timestamp": datetime.now(UTC).isoformat(),
    }


@pytest.fixture
def sample_upload_message_connector_sourced() -> dict:
    """Upload message from a connector sync (inherent-systems/prime#187): source + connection/sync IDs."""
    return {
        "event_type": "document.uploaded",
        "document_id": "test_doc_connector_001",
        "workspace_id": "test_workspace_001",
        "user_id": "test_user_001",
        "filename": "1234567890-abc12345-notion-page.txt",
        "original_filename": "notion_page.txt",
        "content_type": "text/plain",
        "size_bytes": 1024,
        "storage_backend": "local",
        "storage_path": "workspaces/test_workspace_001/notion-page.txt",
        "storage_bucket": None,
        "storage_url": "http://localhost:4000/api/v1/storage/documents/notion-page.txt",
        "timestamp": datetime.now(UTC).isoformat(),
        "source": "connector:notion",
        "connection_id": "conn_123",
        "sync_id": "sync_456",
    }


@pytest.fixture
def sample_upload_message_public_api() -> dict:
    """Upload message from the public API (inherent-systems/prime#187): source only, no connector IDs."""
    return {
        "event_type": "document.uploaded",
        "document_id": "test_doc_public_api_001",
        "workspace_id": "test_workspace_001",
        "user_id": "test_user_001",
        "filename": "1234567890-abc12345-report.pdf",
        "original_filename": "report.pdf",
        "content_type": "application/pdf",
        "size_bytes": 4096,
        "storage_backend": "local",
        "storage_path": "workspaces/test_workspace_001/report.pdf",
        "storage_bucket": None,
        "storage_url": "http://localhost:4000/api/v1/storage/documents/report.pdf",
        "timestamp": datetime.now(UTC).isoformat(),
        "source": "public-api",
    }


@pytest.fixture
def sample_upload_message_avro_wrapped() -> dict:
    """Create a sample message with Avro-wrapped union types."""
    return {
        "event_type": "document.uploaded",
        "document_id": "test_doc_avro_001",
        "workspace_id": "test_workspace_avro",
        "user_id": "test_user_avro",
        "filename": "avro-test.txt",
        "original_filename": "avro_test.txt",
        "content_type": "text/plain",
        "size_bytes": 2048,
        "storage_backend": "local",
        "storage_path": "workspaces/test_workspace_avro/avro-test.txt",
        "storage_bucket": {"string": "test-bucket"},
        "storage_url": {"string": "http://localhost:4000/api/v1/storage/documents/avro-test.txt"},
        "timestamp": datetime.now(UTC).isoformat(),
    }


@pytest.fixture
def sample_chunks() -> list[DocumentChunk]:
    """Create sample document chunks."""
    return [
        DocumentChunk(
            document_id="test_doc_12345",
            content="This is the first chunk of the document.",
            chunk_index=0,
            start_char=0,
            end_char=42,
            metadata={"page": 1},
        ),
        DocumentChunk(
            document_id="test_doc_12345",
            content="This is the second chunk with more content.",
            chunk_index=1,
            start_char=42,
            end_char=87,
            metadata={"page": 1},
        ),
        DocumentChunk(
            document_id="test_doc_12345",
            content="Final chunk of the test document.",
            chunk_index=2,
            start_char=87,
            end_char=120,
            metadata={"page": 2},
        ),
    ]


@pytest.fixture(autouse=True)
async def cleanup_test_data(db_service: DatabaseService):
    """Clean up test data after each test."""
    yield
    # Clean up test documents created during tests
    try:
        with db_service.get_session() as session:
            # Clean up documents
            session.execute(
                db_service.processed_documents.delete().where(
                    db_service.processed_documents.c.document_id.like("test_%")
                )
            )
            # Clean up workspace metadata
            session.execute(
                db_service.workspace_metadata.delete().where(
                    db_service.workspace_metadata.c.workspace_id.like("test_%")
                )
            )
            # Clean up tenants
            session.execute(
                db_service.tenants.delete().where(db_service.tenants.c.user_id.like("test_%"))
            )
            # Clean up idle_user from test_get_idle_tenants
            session.execute(
                db_service.tenants.delete().where(db_service.tenants.c.user_id == "idle_user")
            )
            # Clean up ingestion events
            session.execute(
                db_service.ingestion_events.delete().where(
                    db_service.ingestion_events.c.document_id.like("test_%")
                )
            )
    except Exception:
        pass  # Ignore cleanup errors
