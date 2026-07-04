"""Database service for PostgreSQL with document storage and multi-tenancy support."""

import enum
import hashlib
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Engine,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    create_engine,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import sessionmaker

from src.config.settings import Settings
from src.models.document import DocumentChunk, DocumentUploadMessage

logger = structlog.get_logger(__name__)


class DocumentStatus(enum.StrEnum):
    """Document processing status."""

    PENDING = "pending"
    PROCESSING = "processing"
    PROCESSED = "processed"
    FAILED = "failed"
    DELETED = "deleted"


class TenantStatus(enum.StrEnum):
    """Tenant status for lifecycle management."""

    ACTIVE = "active"
    INACTIVE = "inactive"
    SUSPENDED = "suspended"


class StorageBackendType(enum.StrEnum):
    """Storage backend types."""

    LOCAL = "local"
    GCS = "gcs"
    S3 = "s3"
    AZURE = "azure"


class DatabaseService:
    """Database service for PostgreSQL operations with document storage and multi-tenancy.

    Schema Design:
    ==============

    tenants (user-level tenant registry)
    workspace_metadata (workspace-level metadata and stats)
    processed_documents (parent table)
    └── document_chunks (child table, FK → processed_documents.id)

    This schema:
    - Supports multi-tenancy with user-level tenants
    - Uses proper foreign key constraints with CASCADE delete
    - Uses integer primary keys for performance
    - Indexes on frequently queried columns
    """

    def __init__(self, settings: Settings):
        """Initialize database service."""
        self.settings = settings
        self.engine: Engine | None = None
        self.SessionLocal: sessionmaker | None = None
        self.metadata = MetaData()

        # Define tables with proper relationships
        self._define_tables()

    def _define_tables(self) -> None:
        """Define all database tables."""

        # Tenants table: User-level tenant registry
        self.tenants = Table(
            "tenants",
            self.metadata,
            Column("id", BigInteger, primary_key=True, autoincrement=True),
            Column("user_id", String(100), nullable=False, unique=True),
            Column("status", String(20), nullable=False, default="active"),
            Column(
                "created_at",
                DateTime(timezone=True),
                nullable=False,
                default=lambda: datetime.now(UTC),
            ),
            Column(
                "updated_at",
                DateTime(timezone=True),
                nullable=False,
                default=lambda: datetime.now(UTC),
            ),
            Column(
                "last_activity_at",
                DateTime(timezone=True),
                nullable=False,
                default=lambda: datetime.now(UTC),
            ),
            Column("metadata", JSONB, nullable=True, default={}),
            Index("idx_tenants_status", "status"),
            Index("idx_tenants_last_activity", "last_activity_at"),
        )

        # Workspace metadata table: Workspace-level stats and config
        self.workspace_metadata = Table(
            "workspace_metadata",
            self.metadata,
            Column("id", BigInteger, primary_key=True, autoincrement=True),
            Column("workspace_id", String(100), nullable=False, unique=True),
            Column("user_id", String(100), nullable=False),
            Column("weaviate_collection", String(200), nullable=True),
            Column("document_count", Integer, nullable=False, default=0),
            Column("chunk_count", Integer, nullable=False, default=0),
            Column("total_size_bytes", BigInteger, nullable=False, default=0),
            Column(
                "created_at",
                DateTime(timezone=True),
                nullable=False,
                default=lambda: datetime.now(UTC),
            ),
            Column(
                "updated_at",
                DateTime(timezone=True),
                nullable=False,
                default=lambda: datetime.now(UTC),
            ),
            Column("metadata", JSONB, nullable=True, default={}),
            Index("idx_workspace_metadata_user_id", "user_id"),
        )

        # Idempotency ledger for workspace stat increments (#7): each workflow
        # run applies its deltas at most once. See migration 011.
        self.workspace_stats_ledger = Table(
            "workspace_stats_ledger",
            self.metadata,
            Column("workflow_run_id", String, primary_key=True),
            Column("workspace_id", String, nullable=False),
            Column(
                "applied_at",
                DateTime(timezone=True),
                nullable=False,
                default=lambda: datetime.now(UTC),
            ),
        )

        # Parent table: processed_documents
        self.processed_documents = Table(
            "processed_documents",
            self.metadata,
            Column("id", BigInteger, primary_key=True, autoincrement=True),
            Column("document_id", String(100), nullable=False, unique=True),
            Column("workspace_id", String(100), nullable=False),
            Column("user_id", String(100), nullable=False),
            Column("tenant_id", BigInteger, nullable=True),  # New: tenant reference
            Column("filename", String(500), nullable=False),
            Column("original_filename", String(500), nullable=False),
            Column("content_type", String(100), nullable=False),
            Column("size_bytes", BigInteger, nullable=False),
            Column("storage_backend", String(20), nullable=False, default="local"),
            Column("storage_path", String(1000), nullable=False),
            Column("storage_bucket", String(255), nullable=True),
            Column("storage_url", String(2000), nullable=True),
            Column("status", String(20), nullable=False, default="pending"),
            Column("error_message", Text, nullable=True),
            Column("chunk_count", Integer, default=0),
            Column("text_length", Integer, default=0),
            Column("processing_time_ms", Integer, default=0),
            Column("metadata", JSONB, nullable=True),
            Column(
                "created_at",
                DateTime(timezone=True),
                nullable=False,
                default=lambda: datetime.now(UTC),
            ),
            Column(
                "updated_at",
                DateTime(timezone=True),
                nullable=False,
                default=lambda: datetime.now(UTC),
            ),
            Column("processed_at", DateTime(timezone=True), nullable=True),
            Index("idx_processed_documents_workspace_id", "workspace_id"),
            Index("idx_processed_documents_user_id", "user_id"),
            Index("idx_processed_documents_tenant_id", "tenant_id"),
            Index("idx_processed_documents_status", "status"),
            Index("idx_processed_documents_content_type", "content_type"),
            Index("idx_processed_documents_created_at", "created_at"),
            Index("idx_processed_documents_workspace_status", "workspace_id", "status"),
            Index("idx_processed_documents_tenant_workspace", "tenant_id", "workspace_id"),
        )

        # Child table: document_chunks
        self.document_chunks = Table(
            "document_chunks",
            self.metadata,
            Column("id", BigInteger, primary_key=True, autoincrement=True),
            Column(
                "processed_document_id",
                BigInteger,
                ForeignKey("processed_documents.id", ondelete="CASCADE"),
                nullable=False,
            ),
            Column("document_id", String(100), nullable=False),
            Column("workspace_id", String(100), nullable=False),
            Column("tenant_id", BigInteger, nullable=True),  # New: tenant reference
            Column("chunk_index", Integer, nullable=False),
            Column("content", Text, nullable=False),
            Column("token_count", Integer, nullable=True),
            Column("start_char", Integer, default=0),
            Column("end_char", Integer, default=0),
            Column("metadata", JSONB, nullable=True),
            # Provenance (#41): nullable, additive. See migration 008.
            Column("content_hash", String(64), nullable=True),
            Column("source_uri", String(2000), nullable=True),
            Column(
                "created_at",
                DateTime(timezone=True),
                nullable=False,
                default=lambda: datetime.now(UTC),
            ),
            # Freshness (#42): when this chunk was (re)ingested. Defaulted/additive
            # (see migration 009). Promoted onto SearchResult so aged evidence can
            # be flagged is_stale (the API does not drop it).
            Column(
                "ingested_at",
                DateTime(timezone=True),
                nullable=True,
                default=lambda: datetime.now(UTC),
            ),
            UniqueConstraint(
                "processed_document_id", "chunk_index", name="uq_document_chunks_doc_idx"
            ),
            Index("idx_document_chunks_document_id", "document_id"),
            Index("idx_document_chunks_workspace_id", "workspace_id"),
            Index("idx_document_chunks_tenant_id", "tenant_id"),
            Index("idx_document_chunks_processed_document_id", "processed_document_id"),
        )

        # Ingestion events table: Data lineage / audit trail for pipeline steps
        self.ingestion_events = Table(
            "ingestion_events",
            self.metadata,
            Column("id", BigInteger, primary_key=True, autoincrement=True),
            Column("workflow_run_id", String(255), nullable=False),
            Column("document_id", String(255), nullable=False),
            Column("workspace_id", String(255), nullable=True),
            Column("event_type", String(50), nullable=False),
            Column("status", String(20), nullable=False),
            Column("duration_ms", Integer, nullable=True),
            Column("metadata", JSONB, nullable=True),
            Column(
                "created_at",
                DateTime(timezone=True),
                nullable=False,
                server_default=func.now(),
            ),
            Index("idx_ingestion_events_workflow_run_id", "workflow_run_id"),
            Index("idx_ingestion_events_document_id", "document_id"),
            Index("idx_ingestion_events_event_type", "event_type"),
        )

        # API Keys table: For customer API access authentication
        # This table is written by intg-svc and read by mcp-svc
        self.api_keys = Table(
            "api_keys",
            self.metadata,
            Column("id", BigInteger, primary_key=True, autoincrement=True),
            Column("key_id", String(100), nullable=False, unique=True),  # Public identifier
            Column("key_hash", String(255), nullable=False),  # SHA-256 hash of the key
            Column("key_prefix", String(10), nullable=False),  # First 8 chars for identification
            Column("user_id", String(100), nullable=False),  # Owner's user ID
            Column(
                "workspace_id", String(100), nullable=True
            ),  # Optional: if null, key is user-scoped
            Column("name", String(255), nullable=False),  # User-friendly name
            Column("status", String(20), nullable=False, default="active"),  # active/revoked
            Column("permissions", JSONB, nullable=False, default=["read", "search"]),
            Column("rate_limit", Integer, nullable=False, default=100),  # Requests per minute
            Column("expires_at", DateTime(timezone=True), nullable=True),  # Optional expiration
            Column("last_used_at", DateTime(timezone=True), nullable=True),
            Column(
                "created_at",
                DateTime(timezone=True),
                nullable=False,
                default=lambda: datetime.now(UTC),
            ),
            Column(
                "updated_at",
                DateTime(timezone=True),
                nullable=False,
                default=lambda: datetime.now(UTC),
            ),
            Column("metadata", JSONB, nullable=True, default={}),
            Index("idx_api_keys_user_id", "user_id"),
            Index("idx_api_keys_workspace_id", "workspace_id"),
            Index("idx_api_keys_status", "status"),
            Index("idx_api_keys_key_prefix", "key_prefix"),
        )

        # Dead-letter jobs table: Failed ingestion jobs for retry/analysis
        self.dead_letter_jobs = Table(
            "dead_letter_jobs",
            self.metadata,
            Column("id", BigInteger, primary_key=True, autoincrement=True),
            Column("document_id", String(255), nullable=False),
            Column("workspace_id", String(255), nullable=False),
            Column("user_id", String(255), nullable=False),
            Column("workflow_run_id", String(255), nullable=True),
            Column("original_message", JSONB, nullable=False),
            Column("error_message", Text, nullable=False),
            Column("error_type", String(100), nullable=False),
            Column("retry_count", Integer, nullable=False, default=0),
            Column("status", String(20), nullable=False, default="pending"),
            Column(
                "created_at",
                DateTime(timezone=True),
                nullable=False,
                server_default=func.now(),
            ),
            Column(
                "updated_at",
                DateTime(timezone=True),
                nullable=False,
                server_default=func.now(),
                onupdate=func.now(),
            ),
            Column("resolved_at", DateTime(timezone=True), nullable=True),
            Index("idx_dead_letter_jobs_document_id", "document_id"),
            Index("idx_dead_letter_jobs_workspace_id", "workspace_id"),
            Index("idx_dead_letter_jobs_status", "status"),
            # Dedup record-retries per run (#24). NULL workflow_run_id rows stay
            # distinct in Postgres, so pre-workflow failures aren't deduped.
            Index(
                "ux_dead_letter_jobs_document_run",
                "document_id",
                "workflow_run_id",
                unique=True,
            ),
        )

    def connect(self) -> None:
        """Connect to PostgreSQL database via DATABASE_URL.

        Note: This no longer runs DDL (create_all). Schema is managed by
        migrations. Call ensure_schema() explicitly for first-time setup or tests.
        """
        try:
            self.engine = create_engine(
                self.settings.database_url,
                pool_pre_ping=True,
                pool_size=10,
                max_overflow=20,
            )
            logger.info("Using direct DATABASE_URL connection")

            self.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)

            logger.info("Connected to PostgreSQL database")
        except Exception as e:
            logger.error("Failed to connect to database", error=str(e), exc_info=True)
            raise

    def ensure_schema(self) -> None:
        """Create tables and views if they don't exist.

        Call this explicitly for first-time setup, tests, or migration-free
        environments. Not called automatically on connect() — production
        relies on migration scripts.
        """
        if not self.engine:
            return

        try:
            self.metadata.create_all(self.engine)
            logger.info("Database tables created/verified")

            # Create views if they don't exist
            self._create_views()
        except Exception as e:
            logger.error("Failed to create tables", error=str(e))
            raise

    def _create_views(self) -> None:
        """Create database views if they don't exist."""
        if not self.engine:
            return

        try:
            from sqlalchemy import text

            with self.engine.connect() as conn:
                # Create v_workspace_stats view
                conn.execute(
                    text(
                        """
                    CREATE OR REPLACE VIEW v_workspace_stats AS
                    SELECT
                        workspace_id,
                        COUNT(*) as total_documents,
                        SUM(CASE WHEN status = 'processed' THEN 1 ELSE 0 END) as processed_documents,
                        SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) as pending_documents,
                        SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) as failed_documents,
                        SUM(COALESCE(chunk_count, 0)) as total_chunks,
                        SUM(COALESCE(size_bytes, 0)) as total_size_bytes,
                        AVG(COALESCE(processing_time_ms, 0)) as avg_processing_time_ms
                    FROM processed_documents
                    GROUP BY workspace_id;
                    """
                    )
                )
                conn.commit()
            logger.debug("Database views created/verified")
        except Exception as e:
            logger.warning("Failed to create views (may already exist)", error=str(e))
            # Don't raise - views are optional

    def disconnect(self) -> None:
        """Disconnect from database."""
        if self.engine:
            self.engine.dispose()
            self.engine = None

        logger.info("Disconnected from database")

    @contextmanager
    def get_session(self):
        """Get a database session with context manager."""
        if not self.SessionLocal:
            raise RuntimeError("Database not connected")

        session = self.SessionLocal()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    # =========================================================================
    # Tenant Management Methods
    # =========================================================================

    async def upsert_tenant(self, user_id: str) -> int:
        """Upsert a tenant record and return the tenant_id.

        Creates a new tenant if one doesn't exist, or updates last_activity_at
        if one does exist.

        Args:
            user_id: The user identifier

        Returns:
            The tenant_id (primary key)
        """
        if not self.engine:
            raise RuntimeError("Database not connected")

        with self.get_session() as session:
            now = datetime.now(UTC)

            stmt = (
                pg_insert(self.tenants)
                .values(
                    user_id=user_id,
                    status=TenantStatus.ACTIVE.value,
                    created_at=now,
                    updated_at=now,
                    last_activity_at=now,
                )
                .on_conflict_do_update(
                    index_elements=["user_id"],
                    set_={
                        "last_activity_at": now,
                        "updated_at": now,
                        "status": TenantStatus.ACTIVE.value,  # Reactivate if was inactive
                    },
                )
                .returning(self.tenants.c.id)
            )

            result = session.execute(stmt)
            tenant_id: int = result.scalar_one()  # type: ignore[assignment]

            logger.debug("Upserted tenant", user_id=user_id, tenant_id=tenant_id)
            return tenant_id

    async def get_tenant(self, user_id: str) -> dict[str, Any] | None:
        """Get tenant information by user_id.

        Args:
            user_id: The user identifier

        Returns:
            Tenant record or None
        """
        if not self.engine:
            raise RuntimeError("Database not connected")

        with self.get_session() as session:
            result = session.execute(
                self.tenants.select().where(self.tenants.c.user_id == user_id)
            ).fetchone()

            if result:
                return dict(result._mapping)
            return None

    async def update_tenant_status(self, user_id: str, status: str) -> bool:
        """Update tenant status.

        Args:
            user_id: The user identifier
            status: New status (active/inactive/suspended)

        Returns:
            True if updated
        """
        if not self.engine:
            raise RuntimeError("Database not connected")

        with self.get_session() as session:
            result = session.execute(
                self.tenants.update()
                .where(self.tenants.c.user_id == user_id)
                .values(
                    status=status,
                    updated_at=datetime.now(UTC),
                )
            )
            return bool(result.rowcount > 0)  # type: ignore[return-value]

    async def get_idle_tenants(self, cutoff_date: datetime) -> list[dict[str, Any]]:
        """Get tenants that have been idle since before the cutoff date.

        Args:
            cutoff_date: Tenants with last_activity_at before this are considered idle

        Returns:
            List of idle tenant records
        """
        if not self.engine:
            raise RuntimeError("Database not connected")

        with self.get_session() as session:
            results = session.execute(
                self.tenants.select()
                .where(self.tenants.c.status == TenantStatus.ACTIVE.value)
                .where(self.tenants.c.last_activity_at < cutoff_date)
                .order_by(self.tenants.c.last_activity_at)
            ).fetchall()

            return [dict(row._mapping) for row in results]

    async def get_user_workspaces(self, user_id: str) -> list[dict[str, Any]]:
        """Get all workspace metadata for a user.

        Args:
            user_id: The user identifier

        Returns:
            List of workspace metadata records
        """
        if not self.engine:
            raise RuntimeError("Database not connected")

        with self.get_session() as session:
            results = session.execute(
                self.workspace_metadata.select().where(self.workspace_metadata.c.user_id == user_id)
            ).fetchall()

            return [dict(row._mapping) for row in results]

    # =========================================================================
    # Workspace Metadata Methods
    # =========================================================================

    async def upsert_workspace_metadata(
        self,
        workspace_id: str,
        user_id: str,
        weaviate_collection: str | None = None,
    ) -> int:
        """Upsert workspace metadata record.

        Args:
            workspace_id: The workspace identifier
            user_id: The owner's user identifier
            weaviate_collection: Optional Weaviate collection name

        Returns:
            The workspace metadata id
        """
        if not self.engine:
            raise RuntimeError("Database not connected")

        # Generate collection name if not provided
        if not weaviate_collection:
            from src.services.weaviate import get_workspace_collection_name

            weaviate_collection = get_workspace_collection_name(workspace_id)

        with self.get_session() as session:
            now = datetime.now(UTC)

            stmt = (
                pg_insert(self.workspace_metadata)
                .values(
                    workspace_id=workspace_id,
                    user_id=user_id,
                    weaviate_collection=weaviate_collection,
                    document_count=0,
                    chunk_count=0,
                    total_size_bytes=0,
                    created_at=now,
                    updated_at=now,
                )
                .on_conflict_do_update(
                    index_elements=["workspace_id"],
                    set_={
                        "updated_at": now,
                        "weaviate_collection": weaviate_collection,
                    },
                )
                .returning(self.workspace_metadata.c.id)
            )

            result = session.execute(stmt)
            metadata_id: int = result.scalar_one()  # type: ignore[assignment]

            logger.debug(
                "Upserted workspace metadata",
                workspace_id=workspace_id,
                user_id=user_id,
            )
            return metadata_id

    async def get_workspace_metadata(self, workspace_id: str) -> dict[str, Any] | None:
        """Get workspace metadata by workspace_id.

        Args:
            workspace_id: The workspace identifier

        Returns:
            Workspace metadata record or None
        """
        if not self.engine:
            raise RuntimeError("Database not connected")

        with self.get_session() as session:
            result = session.execute(
                self.workspace_metadata.select().where(
                    self.workspace_metadata.c.workspace_id == workspace_id
                )
            ).fetchone()

            if result:
                return dict(result._mapping)
            return None

    async def update_workspace_stats(
        self,
        workspace_id: str,
        document_delta: int = 0,
        chunk_delta: int = 0,
        size_delta: int = 0,
        workflow_run_id: str | None = None,
    ) -> bool:
        """Update workspace statistics atomically.

        Args:
            workspace_id: The workspace identifier
            document_delta: Change in document count
            chunk_delta: Change in chunk count
            size_delta: Change in total size bytes
            workflow_run_id: When provided, the increment is applied at most once
                per run (idempotency ledger, #7) so a Temporal retry or a
                dead-letter reprocess of the same document cannot double-count.

        Returns:
            True if the increment was applied; False if it was skipped as a
            duplicate for this ``workflow_run_id``.
        """
        if not self.engine:
            raise RuntimeError("Database not connected")

        with self.get_session() as session:
            # Idempotency (#7): record the run in a ledger and only apply the
            # increment the first time. The ledger insert and the UPDATE share
            # one transaction, so they commit (or roll back) together.
            if workflow_run_id is not None:
                ledger = session.execute(
                    text(
                        """
                        INSERT INTO workspace_stats_ledger (workflow_run_id, workspace_id)
                        VALUES (:run_id, :workspace_id)
                        ON CONFLICT (workflow_run_id) DO NOTHING
                        """
                    ),
                    {"run_id": workflow_run_id, "workspace_id": workspace_id},
                )
                if ledger.rowcount == 0:
                    # Already applied for this run — skip to avoid double counting.
                    return False

            # Use raw SQL for atomic update with GREATEST to prevent negative values
            result = session.execute(
                text(
                    """
                    UPDATE workspace_metadata
                    SET
                        document_count = GREATEST(0, document_count + :doc_delta),
                        chunk_count = GREATEST(0, chunk_count + :chunk_delta),
                        total_size_bytes = GREATEST(0, total_size_bytes + :size_delta),
                        updated_at = NOW()
                    WHERE workspace_id = :workspace_id
                """
                ),
                {
                    "workspace_id": workspace_id,
                    "doc_delta": document_delta,
                    "chunk_delta": chunk_delta,
                    "size_delta": size_delta,
                },
            )
            return bool(result.rowcount > 0)  # type: ignore[return-value]

    async def delete_workspace_data(self, workspace_id: str) -> int:
        """Delete all data for a workspace.

        Deletes workspace metadata and all associated documents/chunks.

        Args:
            workspace_id: The workspace identifier

        Returns:
            Number of documents deleted
        """
        if not self.engine:
            raise RuntimeError("Database not connected")

        with self.get_session() as session:
            # Delete documents (chunks cascade automatically)
            doc_result = session.execute(
                self.processed_documents.delete().where(
                    self.processed_documents.c.workspace_id == workspace_id
                )
            )
            doc_count = doc_result.rowcount

            # Delete workspace metadata
            session.execute(
                self.workspace_metadata.delete().where(
                    self.workspace_metadata.c.workspace_id == workspace_id
                )
            )

            logger.info(
                "Deleted workspace data",
                workspace_id=workspace_id,
                documents_deleted=doc_count,
            )
            return int(doc_count)  # type: ignore[arg-type]

    # =========================================================================
    # Document Storage Methods (Updated for Multi-Tenancy)
    # =========================================================================

    async def store_processed_document(
        self,
        message: DocumentUploadMessage,
        chunks: list[DocumentChunk],
        text_length: int,
        processing_time_ms: int,
        tenant_id: int | None = None,
    ) -> int:
        """Store processed document and its chunks with proper FK relationship.

        Args:
            message: The original upload message
            chunks: List of document chunks
            text_length: Total extracted text length
            processing_time_ms: Processing time in milliseconds
            tenant_id: Optional tenant_id for multi-tenancy

        Returns:
            ID of the stored document record
        """
        if not self.engine:
            raise RuntimeError("Database not connected")

        with self.get_session() as session:
            try:
                now = datetime.now(UTC)

                # Upsert document record
                stmt = pg_insert(self.processed_documents).values(
                    document_id=message.document_id,
                    workspace_id=message.workspace_id,
                    user_id=message.user_id,
                    tenant_id=tenant_id,
                    filename=message.filename,
                    original_filename=message.original_filename,
                    content_type=message.content_type,
                    size_bytes=message.size_bytes,
                    storage_backend=message.storage_backend,
                    storage_path=message.storage_path,
                    storage_bucket=message.storage_bucket,
                    storage_url=message.storage_url,
                    status=DocumentStatus.PROCESSED.value,
                    chunk_count=len(chunks),
                    text_length=text_length,
                    processing_time_ms=processing_time_ms,
                    created_at=now,
                    updated_at=now,
                    processed_at=now,
                )

                stmt = stmt.on_conflict_do_update(  # type: ignore[assignment]
                    index_elements=["document_id"],
                    set_={
                        "status": DocumentStatus.PROCESSED.value,
                        "chunk_count": len(chunks),
                        "text_length": text_length,
                        "processing_time_ms": processing_time_ms,
                        "tenant_id": tenant_id,
                        "updated_at": now,
                        "processed_at": now,
                        "error_message": None,
                    },
                ).returning(self.processed_documents.c.id)

                result = session.execute(stmt)
                doc_id: int = result.scalar_one()  # type: ignore[assignment]

                # Delete existing chunks for this document (for re-processing)
                session.execute(
                    self.document_chunks.delete().where(
                        self.document_chunks.c.processed_document_id == doc_id
                    )
                )

                # Insert new chunks with foreign key and tenant_id
                if chunks:
                    # Local import avoids a circular import (chunk activity
                    # imports modules that ultimately reference this service).
                    from src.temporal.activities.chunk import estimate_tokens

                    # Provenance (#41): source_uri points at where the chunk's
                    # source bytes live. Prefer the storage_path, fall back to a
                    # remote storage_url; NULL when neither is known.
                    source_uri = message.storage_path or message.storage_url

                    chunk_values = [
                        {
                            "processed_document_id": doc_id,
                            "document_id": message.document_id,
                            "workspace_id": message.workspace_id,
                            "tenant_id": tenant_id,
                            "chunk_index": chunk.chunk_index,
                            "content": chunk.content,
                            # Prefer the model-aware estimate computed by the
                            # chunk activity; fall back to the same estimate if
                            # an older chunk lacks it (no naive word-split).
                            "token_count": (
                                chunk.token_count
                                if chunk.token_count is not None
                                else estimate_tokens(chunk.content)
                            ),
                            "start_char": chunk.start_char,
                            "end_char": chunk.end_char,
                            "metadata": chunk.metadata,
                            # Provenance (#41): content_hash makes returned
                            # evidence auditable/verifiable; source_uri records
                            # where it came from. Both are nullable (migration 008).
                            "content_hash": hashlib.sha256(
                                chunk.content.encode("utf-8")
                            ).hexdigest(),
                            "source_uri": source_uri,
                            "created_at": now,
                            # Freshness (#42): stamp ingest time so the API can age
                            # returned evidence. On re-ingestion (refresh path) the
                            # old chunks are deleted above and re-inserted with a
                            # fresh ingested_at, so a refresh resets staleness.
                            "ingested_at": now,
                        }
                        for chunk in chunks
                    ]
                    session.execute(self.document_chunks.insert(), chunk_values)

                logger.info(
                    "Stored document in PostgreSQL",
                    document_id=message.document_id,
                    doc_pk=doc_id,
                    chunk_count=len(chunks),
                    tenant_id=tenant_id,
                )

                return doc_id

            except Exception as e:
                logger.error(
                    "Failed to store document",
                    document_id=message.document_id,
                    error=str(e),
                    exc_info=True,
                )
                raise

    async def create_pending_document(
        self,
        *,
        document_id: str,
        workspace_id: str,
        user_id: str,
        filename: str,
        original_filename: str,
        content_type: str,
        size_bytes: int,
        storage_backend: str,
        storage_path: str,
        storage_bucket: str | None = None,
        storage_url: str | None = None,
    ) -> bool:
        """Create a minimal 'processing' processed_documents row up front (#10).

        Without this, no row exists until the store step, so an early
        'processing'/'failed' status write hits 0 rows and a document that fails
        during fetch/extract/chunk is invisible ('not found') to the status API.
        No-op if the row already exists (the store step upserts the full record).

        Returns True if a row was created, False if one already existed.
        """
        if not self.engine:
            raise RuntimeError("Database not connected")

        with self.get_session() as session:
            now = datetime.now(UTC)
            stmt = (
                pg_insert(self.processed_documents)
                .values(
                    document_id=document_id,
                    workspace_id=workspace_id,
                    user_id=user_id,
                    filename=filename,
                    original_filename=original_filename,
                    content_type=content_type,
                    size_bytes=size_bytes,
                    storage_backend=storage_backend,
                    storage_path=storage_path,
                    storage_bucket=storage_bucket,
                    storage_url=storage_url,
                    status=DocumentStatus.PROCESSING.value,
                    chunk_count=0,
                    created_at=now,
                    updated_at=now,
                )
                .on_conflict_do_nothing(index_elements=["document_id"])
            )
            result = session.execute(stmt)
            return bool(result.rowcount and result.rowcount > 0)

    async def update_document_status(
        self,
        document_id: str,
        status: DocumentStatus,
        error_message: str | None = None,
    ) -> bool:
        """Update document processing status.

        Args:
            document_id: Document ID
            status: New status
            error_message: Error message if failed

        Returns:
            True if updated
        """
        if not self.engine:
            raise RuntimeError("Database not connected")

        with self.get_session() as session:
            now = datetime.now(UTC)
            result = session.execute(
                self.processed_documents.update()
                .where(self.processed_documents.c.document_id == document_id)
                .values(
                    status=status.value,
                    error_message=error_message,
                    updated_at=now,
                    processed_at=now if status == DocumentStatus.PROCESSED else None,
                )
            )
            return bool(result.rowcount > 0)  # type: ignore[return-value]

    async def get_document_status(self, document_id: str) -> dict[str, Any] | None:
        """Get the processing status of a document."""
        if not self.engine:
            raise RuntimeError("Database not connected")

        with self.get_session() as session:
            result = session.execute(
                self.processed_documents.select().where(
                    self.processed_documents.c.document_id == document_id
                )
            ).fetchone()

            if result:
                return dict(result._mapping)
            return None

    async def get_documents_by_workspace(
        self,
        workspace_id: str,
        status: DocumentStatus | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Get documents for a workspace."""
        if not self.engine:
            raise RuntimeError("Database not connected")

        with self.get_session() as session:
            query = self.processed_documents.select().where(
                self.processed_documents.c.workspace_id == workspace_id
            )

            if status:
                query = query.where(self.processed_documents.c.status == status.value)

            results = session.execute(
                query.order_by(self.processed_documents.c.created_at.desc())
                .limit(limit)
                .offset(offset)
            ).fetchall()

            return [dict(row._mapping) for row in results]

    async def get_documents_by_tenant(
        self,
        tenant_id: int,
        workspace_id: str | None = None,
        status: DocumentStatus | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Get documents for a tenant, optionally filtered by workspace.

        Args:
            tenant_id: The tenant identifier
            workspace_id: Optional workspace filter
            status: Optional status filter
            limit: Max documents to return
            offset: Offset for pagination

        Returns:
            List of document records
        """
        if not self.engine:
            raise RuntimeError("Database not connected")

        with self.get_session() as session:
            query = self.processed_documents.select().where(
                self.processed_documents.c.tenant_id == tenant_id
            )

            if workspace_id:
                query = query.where(self.processed_documents.c.workspace_id == workspace_id)

            if status:
                query = query.where(self.processed_documents.c.status == status.value)

            results = session.execute(
                query.order_by(self.processed_documents.c.created_at.desc())
                .limit(limit)
                .offset(offset)
            ).fetchall()

            return [dict(row._mapping) for row in results]

    async def get_document_chunks(
        self,
        document_id: str,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Get chunks for a document."""
        if not self.engine:
            raise RuntimeError("Database not connected")

        with self.get_session() as session:
            results = session.execute(
                self.document_chunks.select()
                .where(self.document_chunks.c.document_id == document_id)
                .order_by(self.document_chunks.c.chunk_index)
                .limit(limit)
                .offset(offset)
            ).fetchall()

            return [dict(row._mapping) for row in results]

    async def get_chunks_by_workspace(
        self,
        workspace_id: str,
        limit: int = 1000,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Get all chunks for a workspace."""
        if not self.engine:
            raise RuntimeError("Database not connected")

        with self.get_session() as session:
            results = session.execute(
                self.document_chunks.select()
                .where(self.document_chunks.c.workspace_id == workspace_id)
                .order_by(self.document_chunks.c.document_id, self.document_chunks.c.chunk_index)
                .limit(limit)
                .offset(offset)
            ).fetchall()

            return [dict(row._mapping) for row in results]

    async def search_chunks(
        self,
        workspace_id: str,
        query: str,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Search chunks by content using PostgreSQL full-text search.

        Args:
            workspace_id: The workspace to search in
            query: Search query string
            limit: Maximum results

        Returns:
            List of matching chunks with relevance score
        """
        if not self.engine:
            raise RuntimeError("Database not connected")

        with self.get_session() as session:
            # Use PostgreSQL full-text search
            results = session.execute(
                text(
                    """
                    SELECT
                        dc.*,
                        ts_rank(to_tsvector('english', dc.content), plainto_tsquery('english', :query)) as score
                    FROM document_chunks dc
                    WHERE dc.workspace_id = :workspace_id
                    AND to_tsvector('english', dc.content) @@ plainto_tsquery('english', :query)
                    ORDER BY score DESC
                    LIMIT :limit
                """
                ),
                {"workspace_id": workspace_id, "query": query, "limit": limit},
            ).fetchall()

            return [dict(row._mapping) for row in results]

    async def delete_document(self, document_id: str) -> bool:
        """Delete a document and its chunks (CASCADE)."""
        if not self.engine:
            raise RuntimeError("Database not connected")

        with self.get_session() as session:
            result = session.execute(
                self.processed_documents.delete().where(
                    self.processed_documents.c.document_id == document_id
                )
            )

            deleted = bool(result.rowcount > 0)  # type: ignore[return-value]
            if deleted:
                logger.info("Deleted document (cascade)", document_id=document_id)

            return deleted

    async def delete_workspace_documents(self, workspace_id: str) -> int:
        """Delete all documents in a workspace."""
        if not self.engine:
            raise RuntimeError("Database not connected")

        with self.get_session() as session:
            result = session.execute(
                self.processed_documents.delete().where(
                    self.processed_documents.c.workspace_id == workspace_id
                )
            )

            count = result.rowcount
            if count > 0:
                logger.info(
                    "Deleted workspace documents (cascade)",
                    workspace_id=workspace_id,
                    count=count,
                )

            return int(count)  # type: ignore[arg-type]

    async def get_processing_stats(self, workspace_id: str | None = None) -> dict[str, Any]:
        """Get processing statistics."""
        if not self.engine:
            raise RuntimeError("Database not connected")

        with self.get_session() as session:
            where_clause = ""
            params = {}
            if workspace_id:
                where_clause = "WHERE workspace_id = :workspace_id"
                params["workspace_id"] = workspace_id

            stats_query = text(
                f"""
                SELECT
                    COUNT(*) as total_documents,
                    SUM(CASE WHEN status = 'processed' THEN 1 ELSE 0 END) as processed_documents,
                    SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) as pending_documents,
                    SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) as failed_documents,
                    SUM(chunk_count) as total_chunks,
                    SUM(text_length) as total_text_length,
                    AVG(processing_time_ms) as avg_processing_time_ms,
                    SUM(size_bytes) as total_size_bytes
                FROM processed_documents
                {where_clause}
            """
            )

            result = session.execute(stats_query, params).fetchone()

            if result:
                return {
                    "total_documents": result[0] or 0,
                    "processed_documents": result[1] or 0,
                    "pending_documents": result[2] or 0,
                    "failed_documents": result[3] or 0,
                    "total_chunks": result[4] or 0,
                    "total_text_length": result[5] or 0,
                    "avg_processing_time_ms": float(result[6]) if result[6] else 0,
                    "total_size_bytes": result[7] or 0,
                }

            return {}

    async def get_tenant_stats(self, tenant_id: int) -> dict[str, Any]:
        """Get statistics for a specific tenant.

        Args:
            tenant_id: The tenant identifier

        Returns:
            Statistics dict
        """
        if not self.engine:
            raise RuntimeError("Database not connected")

        with self.get_session() as session:
            result = session.execute(
                text(
                    """
                    SELECT
                        COUNT(*) as total_documents,
                        SUM(CASE WHEN status = 'processed' THEN 1 ELSE 0 END) as processed_documents,
                        SUM(chunk_count) as total_chunks,
                        SUM(size_bytes) as total_size_bytes,
                        COUNT(DISTINCT workspace_id) as workspace_count
                    FROM processed_documents
                    WHERE tenant_id = :tenant_id
                """
                ),
                {"tenant_id": tenant_id},
            ).fetchone()

            if result:
                return {
                    "tenant_id": tenant_id,
                    "total_documents": result[0] or 0,
                    "processed_documents": result[1] or 0,
                    "total_chunks": result[2] or 0,
                    "total_size_bytes": result[3] or 0,
                    "workspace_count": result[4] or 0,
                }

            return {"tenant_id": tenant_id}

    # =========================================================================
    # Dead-Letter Job Methods
    # =========================================================================

    async def add_dead_letter_job(
        self,
        document_id: str,
        workspace_id: str,
        user_id: str,
        workflow_run_id: str | None,
        original_message: dict,
        error_message: str,
        error_type: str,
    ) -> int:
        """Insert a failed ingestion job into the dead-letter table.

        Args:
            document_id: The document identifier
            workspace_id: The workspace identifier
            user_id: The user identifier
            workflow_run_id: The Temporal workflow run ID (if available)
            original_message: The full original MQ message dict
            error_message: The final error message
            error_type: Classification of the error (e.g. 'extraction_failed')

        Returns:
            The dead-letter job id
        """
        if not self.engine:
            raise RuntimeError("Database not connected")

        with self.get_session() as session:
            now = datetime.now(UTC)
            # Upsert-do-nothing on (document_id, workflow_run_id): a record-retry
            # (insert commits then loses its ack) must not create a duplicate
            # dead-letter row, which the retry API could otherwise re-ingest twice
            # (#24). NULL workflow_run_id rows are distinct in Postgres, so
            # pre-workflow failures aren't deduped (correct — no run to key on).
            result = session.execute(
                pg_insert(self.dead_letter_jobs)
                .values(
                    document_id=document_id,
                    workspace_id=workspace_id,
                    user_id=user_id,
                    workflow_run_id=workflow_run_id,
                    original_message=original_message,
                    error_message=error_message,
                    error_type=error_type,
                    retry_count=0,
                    status="pending",
                    created_at=now,
                    updated_at=now,
                )
                .on_conflict_do_nothing(index_elements=["document_id", "workflow_run_id"])
                .returning(self.dead_letter_jobs.c.id)
            )
            job_id = result.scalar_one_or_none()
            if job_id is None:
                # Row already existed for this run — return its id (idempotent).
                job_id = session.execute(
                    self.dead_letter_jobs.select()
                    .with_only_columns(self.dead_letter_jobs.c.id)
                    .where(
                        self.dead_letter_jobs.c.document_id == document_id,
                        self.dead_letter_jobs.c.workflow_run_id == workflow_run_id,
                    )
                    .limit(1)
                ).scalar_one_or_none()

            logger.info(
                "Added dead-letter job",
                job_id=job_id,
                document_id=document_id,
                error_type=error_type,
            )
            return job_id

    async def get_dead_letter_jobs(
        self,
        workspace_id: str | None = None,
        status: str = "pending",
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Get dead-letter jobs with optional filters.

        Args:
            workspace_id: Optional workspace filter
            status: Filter by status (default 'pending')
            limit: Maximum results to return

        Returns:
            List of dead-letter job records
        """
        if not self.engine:
            raise RuntimeError("Database not connected")

        with self.get_session() as session:
            query = self.dead_letter_jobs.select()

            if workspace_id:
                query = query.where(self.dead_letter_jobs.c.workspace_id == workspace_id)

            if status:
                query = query.where(self.dead_letter_jobs.c.status == status)

            results = session.execute(
                query.order_by(self.dead_letter_jobs.c.created_at.desc()).limit(limit)
            ).fetchall()

            return [dict(row._mapping) for row in results]

    async def get_dead_letter_job(self, job_id: int) -> dict[str, Any] | None:
        """Get a single dead-letter job by ID.

        Args:
            job_id: The dead-letter job identifier

        Returns:
            Dead-letter job record or None
        """
        if not self.engine:
            raise RuntimeError("Database not connected")

        with self.get_session() as session:
            result = session.execute(
                self.dead_letter_jobs.select().where(self.dead_letter_jobs.c.id == job_id)
            ).fetchone()

            if result:
                return dict(result._mapping)
            return None

    async def update_dead_letter_status(
        self,
        job_id: int,
        status: str,
        resolved_at: datetime | None = None,
    ) -> bool:
        """Update the status of a dead-letter job.

        Args:
            job_id: The dead-letter job identifier
            status: New status ('pending', 'retrying', 'resolved', 'abandoned')
            resolved_at: Optional resolution timestamp

        Returns:
            True if updated
        """
        if not self.engine:
            raise RuntimeError("Database not connected")

        with self.get_session() as session:
            values: dict[str, Any] = {
                "status": status,
                "updated_at": datetime.now(UTC),
            }
            if resolved_at is not None:
                values["resolved_at"] = resolved_at

            result = session.execute(
                self.dead_letter_jobs.update()
                .where(self.dead_letter_jobs.c.id == job_id)
                .values(**values)
            )
            return bool(result.rowcount > 0)  # type: ignore[return-value]

    async def increment_dead_letter_retry(self, job_id: int) -> bool:
        """Increment the retry count and set status to 'retrying'.

        Args:
            job_id: The dead-letter job identifier

        Returns:
            True if updated
        """
        if not self.engine:
            raise RuntimeError("Database not connected")

        with self.get_session() as session:
            result = session.execute(
                text(
                    """
                    UPDATE dead_letter_jobs
                    SET retry_count = retry_count + 1,
                        status = 'retrying',
                        updated_at = NOW()
                    WHERE id = :job_id
                """
                ),
                {"job_id": job_id},
            )
            return bool(result.rowcount > 0)  # type: ignore[return-value]

    # =========================================================================
    # Ingestion Event (Data Lineage) Methods
    # =========================================================================

    async def record_ingestion_event(
        self,
        workflow_run_id: str,
        document_id: str,
        workspace_id: str | None,
        event_type: str,
        status: str,
        duration_ms: int | None = None,
        metadata: dict | None = None,
    ) -> int:
        """Record an ingestion pipeline event for data lineage tracking.

        Args:
            workflow_run_id: The Temporal workflow run ID
            document_id: The document identifier
            workspace_id: The workspace identifier
            event_type: Pipeline step name (e.g. 'tenant_ready', 'document_fetched')
            status: Event outcome ('started', 'succeeded', 'failed')
            duration_ms: Step duration in milliseconds
            metadata: Optional extra context (error messages, counts, etc.)

        Returns:
            The ingestion event id
        """
        if not self.engine:
            raise RuntimeError("Database not connected")

        with self.get_session() as session:
            result = session.execute(
                self.ingestion_events.insert()
                .values(
                    workflow_run_id=workflow_run_id,
                    document_id=document_id,
                    workspace_id=workspace_id,
                    event_type=event_type,
                    status=status,
                    duration_ms=duration_ms,
                    metadata=metadata,
                )
                .returning(self.ingestion_events.c.id)
            )
            event_id: int = result.scalar_one()  # type: ignore[assignment]

            logger.debug(
                "Recorded ingestion event",
                event_id=event_id,
                workflow_run_id=workflow_run_id,
                document_id=document_id,
                event_type=event_type,
                status=status,
            )
            return event_id

    async def get_ingestion_events(self, document_id: str) -> list[dict[str, Any]]:
        """Get all ingestion events for a document, ordered by created_at.

        Args:
            document_id: The document identifier

        Returns:
            List of ingestion event records
        """
        if not self.engine:
            raise RuntimeError("Database not connected")

        with self.get_session() as session:
            results = session.execute(
                self.ingestion_events.select()
                .where(self.ingestion_events.c.document_id == document_id)
                .order_by(self.ingestion_events.c.created_at)
            ).fetchall()

            return [dict(row._mapping) for row in results]

    async def get_ingestion_events_by_workflow(self, workflow_run_id: str) -> list[dict[str, Any]]:
        """Get all ingestion events for a workflow run, ordered by created_at.

        Args:
            workflow_run_id: The Temporal workflow run ID

        Returns:
            List of ingestion event records
        """
        if not self.engine:
            raise RuntimeError("Database not connected")

        with self.get_session() as session:
            results = session.execute(
                self.ingestion_events.select()
                .where(self.ingestion_events.c.workflow_run_id == workflow_run_id)
                .order_by(self.ingestion_events.c.created_at)
            ).fetchall()

            return [dict(row._mapping) for row in results]
