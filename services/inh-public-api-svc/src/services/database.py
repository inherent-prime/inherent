"""Read-only database service for PostgreSQL access.

Supports two connection modes:
1. Direct connection via DATABASE_URL (local development)
2. Cloud SQL Python Connector (production on Cloud Run)

The connection mode is determined by the USE_CLOUD_SQL_CONNECTOR setting.
"""

import hashlib
import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, AsyncGenerator

from sqlalchemy import and_, bindparam, column, or_, select, table, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.config import settings
from src.models.api_key import APIKeyInfo
from src.models.document import Document, DocumentChunk
from src.utils import get_logger

if TYPE_CHECKING:
    from google.cloud.sql.connector import Connector

logger = get_logger(__name__)


def _merge_chunk_provenance(row) -> dict:
    """Fold the document_chunks provenance/freshness COLUMNS into the chunk's
    metadata dict so consumers that read metadata (e.g. explain_lineage, #40)
    see the real values stored by #41/#42. Existing metadata keys win.
    """
    meta = dict(row.metadata or {})
    if getattr(row, "content_hash", None) is not None:
        meta.setdefault("content_hash", row.content_hash)
    if getattr(row, "source_uri", None) is not None:
        meta.setdefault("source_uri", row.source_uri)
    ingested = getattr(row, "ingested_at", None)
    if ingested is not None:
        meta.setdefault(
            "ingested_at",
            ingested.isoformat() if hasattr(ingested, "isoformat") else ingested,
        )
    return meta


class DatabaseService:
    """Read-only database service for PostgreSQL.

    This service provides read-only access to the PostgreSQL database
    for API key validation and document/chunk queries.

    Connection Modes:
        - Direct: Uses DATABASE_URL with asyncpg driver
        - Cloud SQL: Uses Google Cloud SQL Python Connector with asyncpg driver

    The Cloud SQL connector provides:
        - Automatic SSL/TLS encryption
        - IAM-based authentication (no passwords in connection string)
        - Secure connection without exposing database ports
    """

    # Pool configuration - can be overridden via environment if needed
    POOL_SIZE = 5
    MAX_OVERFLOW = 10
    POOL_TIMEOUT = 30
    POOL_RECYCLE = 1800

    def __init__(self) -> None:
        """Initialize database service.

        Engine creation is deferred to initialize() to support async context.
        """
        self.engine = None
        self.session_factory = None
        self._initialized = False
        self._cloud_sql_connector: "Connector | None" = None

    def _create_direct_engine(self):
        """Create SQLAlchemy engine for direct PostgreSQL connection.

        Used for local development with DATABASE_URL.
        """
        database_url = settings.database_url

        # Convert postgresql:// to postgresql+asyncpg:// for async support
        if database_url.startswith("postgresql://"):
            database_url = database_url.replace("postgresql://", "postgresql+asyncpg://", 1)

        logger.info(
            "Creating direct database connection",
            url=database_url.split("@")[-1] if "@" in database_url else "***",
        )

        return create_async_engine(
            database_url,
            pool_size=self.POOL_SIZE,
            max_overflow=self.MAX_OVERFLOW,
            pool_timeout=self.POOL_TIMEOUT,
            pool_recycle=self.POOL_RECYCLE,
            echo=False,
        )

    async def _create_cloud_sql_engine(self):
        """Create SQLAlchemy async engine using Cloud SQL Python Connector.

        Used for production deployment on Cloud Run.

        The connector provides:
            - Automatic SSL/TLS encryption
            - Support for both IAM and password authentication
            - Secure connection without exposing ports

        Authentication modes:
            - IAM auth (default): Uses service account, requires roles/cloudsql.client
            - Password auth: Uses CLOUD_SQL_PASSWORD when CLOUD_SQL_USE_IAM_AUTH=false

        Note: Uses create_async_connector() which properly integrates with the
        current thread's running event loop, avoiding event loop mismatch errors.
        """
        try:
            from google.cloud.sql.connector import create_async_connector
        except ImportError as e:
            raise ImportError(
                "cloud-sql-python-connector is required for Cloud SQL connections. "
                "Install with: pip install 'cloud-sql-python-connector[asyncpg]'"
            ) from e

        if not settings.cloud_sql_instance:
            raise ValueError(
                "CLOUD_SQL_INSTANCE must be set when USE_CLOUD_SQL_CONNECTOR=true. "
                "Format: project:region:instance"
            )

        # Use create_async_connector() which uses the current thread's running event loop
        # This avoids the "event loop does not match" error that occurs with Connector()
        self._cloud_sql_connector = await create_async_connector()

        use_iam_auth = settings.cloud_sql_use_iam_auth
        password = settings.cloud_sql_password

        # Determine authentication mode
        if password and not use_iam_auth:
            logger.info(
                "Using Cloud SQL with password authentication (async)",
                instance=settings.cloud_sql_instance,
                database=settings.cloud_sql_database,
                user=settings.cloud_sql_user,
            )
            auth_mode = "password"
        else:
            logger.info(
                "Using Cloud SQL with IAM authentication (async)",
                instance=settings.cloud_sql_instance,
                database=settings.cloud_sql_database,
                user=settings.cloud_sql_user,
            )
            auth_mode = "iam"

        # Store these for use in the async creator
        instance = settings.cloud_sql_instance
        database = settings.cloud_sql_database
        user = settings.cloud_sql_user
        connector = self._cloud_sql_connector

        async def getconn() -> Any:
            """Async connection factory for Cloud SQL connector."""
            connect_kwargs: dict[str, Any] = {
                "user": user,
                "db": database,
            }

            if auth_mode == "password":
                connect_kwargs["password"] = password
            else:
                connect_kwargs["enable_iam_auth"] = True

            conn = await connector.connect_async(
                instance,
                "asyncpg",
                **connect_kwargs,
            )
            return conn

        return create_async_engine(
            "postgresql+asyncpg://",
            async_creator=getconn,
            pool_size=self.POOL_SIZE,
            max_overflow=self.MAX_OVERFLOW,
            pool_timeout=self.POOL_TIMEOUT,
            pool_recycle=self.POOL_RECYCLE,
            echo=False,
        )

    async def initialize(self) -> None:
        """Initialize the database connection.

        Creates the appropriate engine based on configuration:
        - Cloud SQL connector when USE_CLOUD_SQL_CONNECTOR=true
        - Direct asyncpg connection otherwise
        """
        if self._initialized:
            return

        try:
            # Select connection mode based on settings
            if settings.use_cloud_sql_connector:
                self.engine = await self._create_cloud_sql_engine()
            else:
                self.engine = self._create_direct_engine()

            self.session_factory = async_sessionmaker(
                self.engine,
                class_=AsyncSession,
                expire_on_commit=False,
            )

            # Verify connection
            async with self.engine.connect() as conn:
                await conn.execute(text("SELECT 1"))

            self._initialized = True
            logger.info(
                "Database connection established",
                mode="cloud_sql" if settings.use_cloud_sql_connector else "direct",
            )
        except Exception as e:
            logger.error("Failed to connect to database", error=str(e))
            raise

    async def close(self) -> None:
        """Close the database connection and cleanup resources."""
        if self.engine:
            await self.engine.dispose()
            self.engine = None

        # Cleanup Cloud SQL connector if used (use close_async for async connector)
        if self._cloud_sql_connector:
            await self._cloud_sql_connector.close_async()
            self._cloud_sql_connector = None

        self.session_factory = None
        self._initialized = False
        logger.info("Database connection closed")

    @asynccontextmanager
    async def session(self) -> AsyncGenerator[AsyncSession, None]:
        """Get a database session."""
        async with self.session_factory() as session:
            try:
                yield session
            finally:
                await session.close()

    # API Key validation
    async def validate_api_key(self, api_key: str) -> APIKeyInfo | None:
        """Validate an API key and return key info if valid."""
        if not api_key or not api_key.startswith("ink_"):
            return None

        key_hash = hashlib.sha256(api_key.encode()).hexdigest()

        async with self.session() as session:
            result = await session.execute(
                text(
                    """
                    SELECT key_id, user_id, workspace_id, permissions, rate_limit,
                           expires_at, status
                    FROM api_keys
                    WHERE key_hash = :key_hash AND status = 'active'
                """
                ),
                {"key_hash": key_hash},
            )
            row = result.fetchone()

            if not row:
                return None

            # Check expiration
            if row.expires_at and datetime.now(timezone.utc) > row.expires_at:
                return None

            # Update last_used_at (fire and forget style, don't block)
            await session.execute(
                text("UPDATE api_keys SET last_used_at = NOW() WHERE key_hash = :key_hash"),
                {"key_hash": key_hash},
            )
            await session.commit()

            return APIKeyInfo(
                key_id=row.key_id,
                user_id=row.user_id,
                workspace_id=row.workspace_id,
                permissions=row.permissions if isinstance(row.permissions, list) else [],
                rate_limit=row.rate_limit,
                expires_at=row.expires_at,
                status=row.status,
            )

    # Document writes (upload lifecycle)
    async def get_document_id_by_filename(
        self,
        workspace_id: str,
        original_filename: str,
    ) -> str | None:
        """Return an existing document_id for (workspace_id, original_filename).

        Used for dedup: re-uploading a file with the same name into the same
        workspace should reuse the existing document_id so ingestion treats it
        as a reindex rather than creating a brand-new (duplicate) document.

        Returns the most recently created matching document_id, or None.
        """
        async with self.session() as session:
            result = await session.execute(
                text(
                    """
                    SELECT document_id
                    FROM processed_documents
                    WHERE workspace_id = :workspace_id
                      AND original_filename = :original_filename
                    ORDER BY created_at DESC
                    LIMIT 1
                    """
                ),
                {"workspace_id": workspace_id, "original_filename": original_filename},
            )
            row = result.fetchone()
            return str(row.document_id) if row else None

    async def get_document_id_by_content_hash(
        self,
        workspace_id: str,
        content_hash: str,
    ) -> str | None:
        """Return an existing document_id for (workspace_id, content_hash).

        Content-based dedup (#75): re-uploading the SAME content into a workspace
        — even under a different filename — should reuse the existing document_id
        so ingestion reindexes it rather than creating a duplicate document that
        floods search results. This is checked BEFORE filename dedup so a verbatim
        copy uploaded as ``guide-copy.md`` collapses onto the original ``guide.md``.

        Returns the most recently created matching document_id, or None.
        """
        async with self.session() as session:
            result = await session.execute(
                text(
                    """
                    SELECT document_id
                    FROM processed_documents
                    WHERE workspace_id = :workspace_id
                      AND content_hash = :content_hash
                    ORDER BY created_at DESC
                    LIMIT 1
                    """
                ),
                {"workspace_id": workspace_id, "content_hash": content_hash},
            )
            row = result.fetchone()
            return str(row.document_id) if row else None

    async def create_or_reset_pending_document(
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
        content_hash: str | None = None,
        metadata: dict | None = None,
    ) -> None:
        """Persist (or reset) a 'pending' row in processed_documents.

        This is written at upload time — BEFORE the MQ publish — so that a
        GET /v1/documents/{id} immediately after upload returns the document
        with status='pending' instead of 404ing until ingestion completes.

        On conflict (same document_id, e.g. a reindex/re-upload) the row is
        reset to a clean pending state: status='pending', error_message=NULL,
        chunk_count=0, and the latest file metadata is applied. tenant_id is
        left NULL here; the ingestion service backfills it.

        ``content_hash`` is the document-level dedup key (#75). When None (e.g. a
        refresh that re-publishes a stored row without re-reading bytes) the
        existing stored hash is preserved via COALESCE rather than being wiped.
        """
        import json

        async with self.session() as session:
            await session.execute(
                text(
                    """
                    INSERT INTO processed_documents (
                        document_id, workspace_id, user_id,
                        filename, original_filename, content_type, size_bytes,
                        storage_backend, storage_path, storage_bucket, storage_url,
                        content_hash, status, error_message, chunk_count, metadata
                    ) VALUES (
                        :document_id, :workspace_id, :user_id,
                        :filename, :original_filename, :content_type, :size_bytes,
                        :storage_backend, :storage_path, :storage_bucket, :storage_url,
                        :content_hash, 'pending', NULL, 0, CAST(:metadata AS JSONB)
                    )
                    ON CONFLICT (document_id) DO UPDATE SET
                        workspace_id = EXCLUDED.workspace_id,
                        user_id = EXCLUDED.user_id,
                        filename = EXCLUDED.filename,
                        original_filename = EXCLUDED.original_filename,
                        content_type = EXCLUDED.content_type,
                        size_bytes = EXCLUDED.size_bytes,
                        storage_backend = EXCLUDED.storage_backend,
                        storage_path = EXCLUDED.storage_path,
                        storage_bucket = EXCLUDED.storage_bucket,
                        storage_url = EXCLUDED.storage_url,
                        content_hash = COALESCE(
                            EXCLUDED.content_hash, processed_documents.content_hash
                        ),
                        status = 'pending',
                        error_message = NULL,
                        chunk_count = 0,
                        metadata = EXCLUDED.metadata,
                        updated_at = NOW()
                    """
                ),
                {
                    "document_id": document_id,
                    "workspace_id": workspace_id,
                    "user_id": user_id,
                    "filename": filename,
                    "original_filename": original_filename,
                    "content_type": content_type,
                    "size_bytes": size_bytes,
                    "storage_backend": storage_backend,
                    "storage_path": storage_path,
                    "storage_bucket": storage_bucket,
                    "storage_url": storage_url,
                    "content_hash": content_hash,
                    "metadata": json.dumps(metadata) if metadata is not None else None,
                },
            )
            await session.commit()

    async def mark_document_failed(
        self,
        document_id: str,
        workspace_id: str,
        error_message: str,
    ) -> None:
        """Mark a pending document as 'failed' with an error message.

        Called when the durable handoff fails (e.g. MQ enqueue failed) so the
        persisted row reflects reality instead of staying stuck at 'pending'.
        """
        async with self.session() as session:
            await session.execute(
                text(
                    """
                    UPDATE processed_documents
                    SET status = 'failed',
                        error_message = :error_message,
                        updated_at = NOW()
                    WHERE document_id = :document_id
                      AND workspace_id = :workspace_id
                    """
                ),
                {
                    "document_id": document_id,
                    "workspace_id": workspace_id,
                    "error_message": error_message,
                },
            )
            await session.commit()

    # Document queries
    async def get_documents(
        self,
        workspace_id: str,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[Document], int]:
        """Get documents for a workspace."""
        offset = (page - 1) * page_size

        async with self.session() as session:
            # Get total count
            count_result = await session.execute(
                text(
                    """
                    SELECT COUNT(*) FROM processed_documents
                    WHERE workspace_id = :workspace_id
                """
                ),
                {"workspace_id": workspace_id},
            )
            total = count_result.scalar() or 0

            # Get documents
            result = await session.execute(
                text(
                    """
                    SELECT document_id, original_filename, workspace_id,
                           storage_backend, content_type,
                           size_bytes, chunk_count, status,
                           created_at, updated_at, metadata
                    FROM processed_documents
                    WHERE workspace_id = :workspace_id
                    ORDER BY created_at DESC
                    LIMIT :limit OFFSET :offset
                """
                ),
                {"workspace_id": workspace_id, "limit": page_size, "offset": offset},
            )
            rows = result.fetchall()

            documents = [
                Document(
                    id=str(row.document_id),
                    name=row.original_filename,
                    workspace_id=str(row.workspace_id),
                    source_type=row.storage_backend,
                    mime_type=row.content_type,
                    size_bytes=row.size_bytes or 0,
                    chunk_count=row.chunk_count or 0,
                    status=row.status,
                    created_at=row.created_at,
                    updated_at=row.updated_at,
                    metadata=row.metadata,
                )
                for row in rows
            ]

            return documents, total

    async def get_document(self, document_id: str, workspace_id: str) -> Document | None:
        """Get a single document."""
        async with self.session() as session:
            result = await session.execute(
                text(
                    """
                    SELECT document_id, original_filename, workspace_id,
                           storage_backend, content_type,
                           size_bytes, chunk_count, status,
                           created_at, updated_at, metadata
                    FROM processed_documents
                    WHERE document_id = :document_id AND workspace_id = :workspace_id
                """
                ),
                {"document_id": document_id, "workspace_id": workspace_id},
            )
            row = result.fetchone()

            if not row:
                return None

            return Document(
                id=str(row.document_id),
                name=row.original_filename,
                workspace_id=str(row.workspace_id),
                source_type=row.storage_backend,
                mime_type=row.content_type,
                size_bytes=row.size_bytes or 0,
                chunk_count=row.chunk_count or 0,
                status=row.status,
                created_at=row.created_at,
                updated_at=row.updated_at,
                metadata=row.metadata,
            )

    async def get_document_upload_fields(self, document_id: str, workspace_id: str) -> dict | None:
        """Return the stored fields needed to rebuild an upload event (#42 refresh).

        Reads the durable ``processed_documents`` row for ``(document_id,
        workspace_id)`` and returns the storage + identity fields that the
        original ``document.uploaded`` MQ message carried. Returns ``None`` when
        the row does not exist (or is not in this workspace), so the caller can
        404 without leaking cross-workspace existence.
        """
        async with self.session() as session:
            result = await session.execute(
                text(
                    """
                    SELECT document_id, workspace_id, user_id,
                           filename, original_filename, content_type, size_bytes,
                           storage_backend, storage_path, storage_bucket, storage_url
                    FROM processed_documents
                    WHERE document_id = :document_id AND workspace_id = :workspace_id
                """
                ),
                {"document_id": document_id, "workspace_id": workspace_id},
            )
            row = result.fetchone()
            if not row:
                return None
            return dict(row._mapping)

    async def delete_document(self, document_id: str, workspace_id: str) -> dict | None:
        """Delete a document row and its chunks, workspace-scoped (#87).

        Transactional: chunks, the document row, and the workspace stat
        decrement commit together (or not at all). Keyed on ``(document_id,
        workspace_id)`` so a caller can never delete another workspace's
        document — a cross-workspace id reads as not-found.

        Returns the deleted row's ``{document_id, chunk_count, size_bytes}``
        for reporting, or ``None`` when the document is not visible in this
        workspace. Vector-store / object-storage cleanup is the caller's job
        (see ``src/services/deletion.py``).
        """
        async with self.session() as session:
            result = await session.execute(
                text(
                    """
                    SELECT document_id, chunk_count, size_bytes
                    FROM processed_documents
                    WHERE document_id = :document_id AND workspace_id = :workspace_id
                """
                ),
                {"document_id": document_id, "workspace_id": workspace_id},
            )
            row = result.fetchone()
            if not row:
                return None

            chunk_count = row.chunk_count or 0
            size_bytes = row.size_bytes or 0

            await session.execute(
                text("DELETE FROM document_chunks WHERE document_id = :document_id"),
                {"document_id": document_id},
            )
            await session.execute(
                text(
                    """
                    DELETE FROM processed_documents
                    WHERE document_id = :document_id AND workspace_id = :workspace_id
                """
                ),
                {"document_id": document_id, "workspace_id": workspace_id},
            )
            # Keep the workspace counters truthful (ingestion incremented them);
            # clamp at zero so a drifted counter can't go negative.
            await session.execute(
                text(
                    """
                    UPDATE workspace_metadata
                    SET document_count = GREATEST(document_count - 1, 0),
                        chunk_count = GREATEST(chunk_count - :chunk_count, 0),
                        total_size_bytes = GREATEST(total_size_bytes - :size_bytes, 0),
                        updated_at = NOW()
                    WHERE workspace_id = :workspace_id
                """
                ),
                {
                    "chunk_count": chunk_count,
                    "size_bytes": size_bytes,
                    "workspace_id": workspace_id,
                },
            )
            await session.commit()

            logger.info(
                "Document deleted from database",
                document_id=document_id,
                workspace_id=workspace_id,
                chunks_deleted=chunk_count,
            )
            return {
                "document_id": document_id,
                "chunk_count": chunk_count,
                "size_bytes": size_bytes,
            }

    async def get_document_chunks(self, document_id: str, workspace_id: str) -> list[DocumentChunk]:
        """Get all chunks for a document."""
        async with self.session() as session:
            # First verify document belongs to workspace
            doc_result = await session.execute(
                text(
                    """
                    SELECT document_id FROM processed_documents
                    WHERE document_id = :document_id AND workspace_id = :workspace_id
                """
                ),
                {"document_id": document_id, "workspace_id": workspace_id},
            )
            if not doc_result.fetchone():
                return []

            result = await session.execute(
                text(
                    """
                    SELECT id, document_id, content, chunk_index, token_count, metadata,
                           content_hash, source_uri, ingested_at
                    FROM document_chunks
                    WHERE document_id = :document_id
                    ORDER BY chunk_index ASC
                """
                ),
                {"document_id": document_id},
            )
            rows = result.fetchall()

            return [
                DocumentChunk(
                    id=str(row.id),
                    document_id=str(row.document_id),
                    content=row.content,
                    chunk_index=row.chunk_index,
                    token_count=row.token_count or 0,
                    # Surface provenance/freshness columns (#41/#42) into metadata
                    # so consumers like explain_lineage (#40) read real values.
                    metadata=_merge_chunk_provenance(row),
                )
                for row in rows
            ]

    # User workspace queries
    async def get_user_workspace_ids(self, user_id: str) -> list[str]:
        """Get all workspace IDs the user has access to.

        Truth source is the MongoDB ``workspaces`` collection (control plane,
        owned by intg-svc). The previous implementation queried PostgreSQL's
        ``processed_documents`` table — but that only knows about workspaces
        that already have at least one ingested document. A brand-new
        workspace (zero docs) was invisible to this check, producing a 403
        on the user's first upload — a chicken-and-egg auth bug.

        We also union with the PG-side workspaces (any workspace the user has
        ever ingested into) as a defensive fallback for legacy data created
        before the workspaces collection was canonical.
        """
        from src.services.mongo_client import get_mongo_client

        ws_ids: set[str] = set()

        # Primary: Mongo workspaces.user_id ownership (canonical).
        # Mongoose stores ObjectId-typed refs as bson.ObjectId, not strings.
        # We OR both shapes so the lookup is robust to either schema.
        try:
            from bson import ObjectId
            from bson.errors import InvalidId

            user_id_filters: list[dict[str, Any]] = [{"user_id": user_id}]
            try:
                user_id_filters.append({"user_id": ObjectId(user_id)})
            except (InvalidId, TypeError, ValueError):
                pass  # caller passed a non-ObjectId-shaped string; string match only

            client = get_mongo_client()
            db = client[settings.mongodb_db_name]
            cursor = db["workspaces"].find({"$or": user_id_filters}, {"_id": 1})
            async for doc in cursor:
                ws_ids.add(str(doc["_id"]))
        except Exception as exc:
            logger.warning(
                "mongo_workspace_lookup_failed",
                user_id=user_id,
                error=str(exc),
            )

        # Fallback: any workspace the user has uploaded to in PG. Catches
        # legacy data + cushions a transient Mongo outage.
        try:
            async with self.session() as session:
                result = await session.execute(
                    text(
                        """
                        SELECT DISTINCT workspace_id
                        FROM processed_documents
                        WHERE user_id = :user_id
                        """
                    ),
                    {"user_id": user_id},
                )
                for row in result.fetchall():
                    ws_ids.add(str(row.workspace_id))
        except Exception as exc:
            logger.warning(
                "pg_workspace_fallback_lookup_failed",
                user_id=user_id,
                error=str(exc),
            )

        return list(ws_ids)

    # Multi-workspace document queries
    async def get_documents_multi_workspace(
        self,
        workspace_ids: list[str],
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[Document], int]:
        """Get documents across multiple workspaces."""
        if not workspace_ids:
            return [], 0

        offset = (page - 1) * page_size

        async with self.session() as session:
            # Get total count
            count_result = await session.execute(
                text(
                    """
                    SELECT COUNT(*) FROM processed_documents
                    WHERE workspace_id = ANY(:workspace_ids)
                """
                ),
                {"workspace_ids": workspace_ids},
            )
            total = count_result.scalar() or 0

            # Get documents
            result = await session.execute(
                text(
                    """
                    SELECT document_id, original_filename, workspace_id,
                           storage_backend, content_type,
                           size_bytes, chunk_count, status,
                           created_at, updated_at, metadata
                    FROM processed_documents
                    WHERE workspace_id = ANY(:workspace_ids)
                    ORDER BY created_at DESC
                    LIMIT :limit OFFSET :offset
                """
                ),
                {"workspace_ids": workspace_ids, "limit": page_size, "offset": offset},
            )
            rows = result.fetchall()

            documents = [
                Document(
                    id=str(row.document_id),
                    name=row.original_filename,
                    workspace_id=str(row.workspace_id),
                    source_type=row.storage_backend,
                    mime_type=row.content_type,
                    size_bytes=row.size_bytes or 0,
                    chunk_count=row.chunk_count or 0,
                    status=row.status,
                    created_at=row.created_at,
                    updated_at=row.updated_at,
                    metadata=row.metadata,
                )
                for row in rows
            ]

            return documents, total

    async def get_document_by_id(self, document_id: str) -> Document | None:
        """Get a document by ID without workspace restriction (for user-scoped keys)."""
        async with self.session() as session:
            result = await session.execute(
                text(
                    """
                    SELECT document_id, original_filename, workspace_id,
                           storage_backend, content_type,
                           size_bytes, chunk_count, status,
                           created_at, updated_at, metadata
                    FROM processed_documents
                    WHERE document_id = :document_id
                """
                ),
                {"document_id": document_id},
            )
            row = result.fetchone()

            if not row:
                return None

            return Document(
                id=str(row.document_id),
                name=row.original_filename,
                workspace_id=str(row.workspace_id),
                source_type=row.storage_backend,
                mime_type=row.content_type,
                size_bytes=row.size_bytes or 0,
                chunk_count=row.chunk_count or 0,
                status=row.status,
                created_at=row.created_at,
                updated_at=row.updated_at,
                metadata=row.metadata,
            )

    async def get_document_chunks_by_doc_id(self, document_id: str) -> list[DocumentChunk]:
        """Get all chunks for a document by document ID only (for user-scoped keys)."""
        async with self.session() as session:
            result = await session.execute(
                text(
                    """
                    SELECT id, document_id, content, chunk_index, token_count, metadata,
                           content_hash, source_uri, ingested_at
                    FROM document_chunks
                    WHERE document_id = :document_id
                    ORDER BY chunk_index ASC
                """
                ),
                {"document_id": document_id},
            )
            rows = result.fetchall()

            return [
                DocumentChunk(
                    id=str(row.id),
                    document_id=str(row.document_id),
                    content=row.content,
                    chunk_index=row.chunk_index,
                    token_count=row.token_count or 0,
                    # Surface provenance/freshness columns (#41/#42) into metadata
                    # so consumers like explain_lineage (#40) read real values.
                    metadata=_merge_chunk_provenance(row),
                )
                for row in rows
            ]

    async def get_context_chunks(
        self,
        workspace_id: str,
        user_id: str,
        ranges: list[tuple[str, int, int]],
    ) -> list[DocumentChunk]:
        """Fetch chunks whose (document_id, chunk_index) lies in any of the given ranges.

        One batched query: one round-trip regardless of how many ranges are passed.
        Indexed by uq_document_chunks_doc_idx constraint.
        Empty ranges short-circuits — returns [] without a DB call.

        Cross-tenant safety (#41): neighbour chunks are scoped to BOTH
        ``workspace_id`` AND the requesting ``user_id``. ``document_chunks`` has
        no ``user_id`` column, so ownership is enforced via a join to
        ``processed_documents.user_id`` (which is the per-user owner of each
        document). Without this join, a workspace shared by multiple Weaviate
        tenants could leak another user's neighbour chunks during context
        expansion.
        """
        if not ranges:
            return []

        document_chunks = table(
            "document_chunks",
            column("id"),
            column("document_id"),
            column("chunk_index"),
            column("content"),
            column("token_count"),
            column("metadata"),
            column("workspace_id"),
        )
        processed_documents = table(
            "processed_documents",
            column("document_id"),
            column("workspace_id"),
            column("user_id"),
        )

        clauses = []
        params: dict[str, object] = {"workspace_id": workspace_id, "user_id": user_id}
        for i, (doc_id, lo, hi) in enumerate(ranges):
            doc_param = f"doc_{i}"
            lo_param = f"lo_{i}"
            hi_param = f"hi_{i}"
            clauses.append(
                and_(
                    document_chunks.c.document_id == bindparam(doc_param),
                    document_chunks.c.chunk_index.between(
                        bindparam(lo_param),
                        bindparam(hi_param),
                    ),
                )
            )
            params[doc_param] = doc_id
            params[lo_param] = lo
            params[hi_param] = hi

        # Join document_chunks → processed_documents on (document_id,
        # workspace_id) and require the parent document to be owned by the
        # requesting user. This guarantees every returned neighbour belongs to
        # the caller, not just to the (multi-user) workspace.
        query = (
            select(
                document_chunks.c.id,
                document_chunks.c.document_id,
                document_chunks.c.chunk_index,
                document_chunks.c.content,
                document_chunks.c.token_count,
                document_chunks.c.metadata,
            )
            .select_from(
                document_chunks.join(
                    processed_documents,
                    and_(
                        document_chunks.c.document_id == processed_documents.c.document_id,
                        document_chunks.c.workspace_id == processed_documents.c.workspace_id,
                    ),
                )
            )
            .where(document_chunks.c.workspace_id == bindparam("workspace_id"))
            .where(processed_documents.c.user_id == bindparam("user_id"))
            .where(or_(*clauses))
            .order_by(document_chunks.c.document_id, document_chunks.c.chunk_index)
        )

        async with self.session() as session:
            result = await session.execute(query, params)
            rows = result.fetchall()

        return [
            DocumentChunk(
                id=str(row.id),
                document_id=str(row.document_id),
                chunk_index=row.chunk_index,
                content=row.content,
                token_count=row.token_count or 0,
                metadata=row.metadata,
            )
            for row in rows
        ]

    # ------------------------------------------------------------------
    # Evals v1 (design spec: evals-v1) — capture, feedback, cases, runs.
    # Raw SQL like the rest of this service; every statement filters
    # workspace scope (tenancy) and is safe under concurrent writers.
    # ------------------------------------------------------------------

    async def insert_eval_event(
        self,
        *,
        event_id: str,
        workspace_id: str,
        user_id: str | None,
        query_text: str,
        search_mode: str,
        result_doc_ids: list[str],
        result_chunk_ids: list[str],
        top_score: float | None,
        quality_verdict: str | None,
        latency_ms: float,
    ) -> None:
        """Record one captured search event (called from the capture background task)."""
        async with self.session() as session:
            await session.execute(
                text(
                    """
                    INSERT INTO eval_query_events (
                        event_id, workspace_id, user_id, query_text, search_mode,
                        result_doc_ids, result_chunk_ids, top_score, quality_verdict, latency_ms
                    ) VALUES (
                        :event_id, :workspace_id, :user_id, :query_text, :search_mode,
                        CAST(:result_doc_ids AS jsonb), CAST(:result_chunk_ids AS jsonb),
                        :top_score, :quality_verdict, :latency_ms
                    ) ON CONFLICT (event_id) DO NOTHING
                    """
                ),
                {
                    "event_id": event_id,
                    "workspace_id": workspace_id,
                    "user_id": user_id,
                    "query_text": query_text,
                    "search_mode": search_mode,
                    "result_doc_ids": json.dumps(result_doc_ids),
                    "result_chunk_ids": json.dumps(result_chunk_ids),
                    "top_score": top_score,
                    "quality_verdict": quality_verdict,
                    "latency_ms": latency_ms,
                },
            )
            await session.commit()

    async def purge_expired_eval_events(self, *, workspace_id: str, retention_days: int) -> int:
        """Delete raw events past the retention window; returns rows deleted."""
        async with self.session() as session:
            result = await session.execute(
                text(
                    """
                    DELETE FROM eval_query_events
                    WHERE workspace_id = :workspace_id
                      AND created_at < NOW() - make_interval(days => :days)
                    """
                ),
                {"workspace_id": workspace_id, "days": retention_days},
            )
            await session.commit()
            return result.rowcount or 0

    async def delete_eval_events(self, *, workspace_id: str) -> int:
        """Delete all captured events for a workspace (DELETE /v1/evals/events); returns rows deleted."""
        async with self.session() as session:
            result = await session.execute(
                text("DELETE FROM eval_query_events WHERE workspace_id = :workspace_id"),
                {"workspace_id": workspace_id},
            )
            await session.commit()
            return result.rowcount or 0

    async def get_eval_event(self, *, event_id: str, workspace_ids: list[str]) -> dict | None:
        """Fetch one captured event, scoped to the caller's workspaces.

        Returns None when the event does not exist or belongs to a foreign
        workspace, so callers can 404 without leaking cross-workspace existence.
        """
        async with self.session() as session:
            result = await session.execute(
                text(
                    """
                    SELECT event_id, workspace_id, query_text, search_mode,
                           result_doc_ids, result_chunk_ids
                    FROM eval_query_events
                    WHERE event_id = :event_id AND workspace_id = ANY(:workspace_ids)
                    """
                ),
                {"event_id": event_id, "workspace_ids": workspace_ids},
            )
            row = result.fetchone()
            if not row:
                return None
            data = dict(row._mapping)
            # asyncpg/SQLAlchemy returns JSONB as Python lists already; normalize
            # defensively in case the driver ever hands back a different type.
            data["result_doc_ids"] = list(data["result_doc_ids"])
            data["result_chunk_ids"] = list(data["result_chunk_ids"])
            return data

    async def upsert_eval_feedback(
        self,
        *,
        event_id: str,
        workspace_id: str,
        verdict: str,
        useful_chunk_ids: list[str],
        query_text: str,
        note: str | None,
    ) -> None:
        """Record (or replace) the verdict on one event; one verdict per event, last write wins."""
        async with self.session() as session:
            await session.execute(
                text(
                    """
                    INSERT INTO eval_feedback (
                        event_id, workspace_id, verdict, useful_chunk_ids, query_text, note
                    ) VALUES (
                        :event_id, :workspace_id, :verdict,
                        CAST(:useful_chunk_ids AS jsonb), :query_text, :note
                    )
                    ON CONFLICT (event_id) DO UPDATE SET
                        verdict = EXCLUDED.verdict,
                        useful_chunk_ids = EXCLUDED.useful_chunk_ids,
                        note = EXCLUDED.note,
                        updated_at = NOW()
                    """
                ),
                {
                    "event_id": event_id,
                    "workspace_id": workspace_id,
                    "verdict": verdict,
                    "useful_chunk_ids": json.dumps(useful_chunk_ids),
                    "query_text": query_text,
                    "note": note,
                },
            )
            await session.commit()

    async def upsert_eval_case(
        self,
        *,
        case_id: str,
        workspace_id: str,
        query_text: str,
        expected_doc_ids: list[str],
        relevance_grade: int,
        source_event_id: str,
    ) -> str:
        """Insert or update the case for this (workspace, normalized query).

        Re-feedback on the same query merges evidence: expected ids are unioned
        and the grade takes the max, so a later 'partial' can't downgrade an
        'answered'. Returns the surviving case_id (existing row wins).
        """
        async with self.session() as session:
            result = await session.execute(
                text(
                    """
                    INSERT INTO eval_cases (
                        case_id, workspace_id, query_text, expected_doc_ids,
                        relevance_grade, source_event_id
                    ) VALUES (
                        :case_id, :workspace_id, :query_text,
                        CAST(:expected_doc_ids AS jsonb), :relevance_grade, :source_event_id
                    )
                    ON CONFLICT (workspace_id, md5(lower(query_text))) DO UPDATE SET
                        expected_doc_ids = (
                            SELECT jsonb_agg(DISTINCT x) FROM jsonb_array_elements_text(
                                eval_cases.expected_doc_ids || EXCLUDED.expected_doc_ids
                            ) AS t(x)
                        ),
                        relevance_grade = GREATEST(eval_cases.relevance_grade, EXCLUDED.relevance_grade),
                        active = TRUE,
                        updated_at = NOW()
                    RETURNING case_id
                    """
                ),
                {
                    "case_id": case_id,
                    "workspace_id": workspace_id,
                    "query_text": query_text,
                    "expected_doc_ids": json.dumps(expected_doc_ids),
                    "relevance_grade": relevance_grade,
                    "source_event_id": source_event_id,
                },
            )
            await session.commit()
            return result.scalar_one()

    async def list_eval_cases(self, *, workspace_id: str, limit: int, offset: int) -> list[dict]:
        """Page through all eval cases (active and inactive) for a workspace, newest first."""
        async with self.session() as session:
            result = await session.execute(
                text(
                    """
                    SELECT * FROM eval_cases
                    WHERE workspace_id = :workspace_id
                    ORDER BY created_at DESC
                    LIMIT :limit OFFSET :offset
                    """
                ),
                {"workspace_id": workspace_id, "limit": limit, "offset": offset},
            )
            rows = result.fetchall()
            return [dict(row._mapping) for row in rows]

    async def set_eval_case_active(self, *, workspace_id: str, case_id: str, active: bool) -> bool:
        """Enable/disable a case (soft delete); returns True if a row was updated."""
        async with self.session() as session:
            result = await session.execute(
                text(
                    """
                    UPDATE eval_cases SET active = :active, updated_at = NOW()
                    WHERE workspace_id = :workspace_id AND case_id = :case_id
                    """
                ),
                {"workspace_id": workspace_id, "case_id": case_id, "active": active},
            )
            await session.commit()
            return result.rowcount > 0

    async def get_active_eval_cases(self, *, workspace_id: str) -> list[dict]:
        """Fetch the active cases used as the replay set for eval runs."""
        async with self.session() as session:
            result = await session.execute(
                text(
                    """
                    SELECT case_id, query_text, expected_doc_ids, relevance_grade
                    FROM eval_cases
                    WHERE workspace_id = :workspace_id AND active
                    ORDER BY created_at
                    """
                ),
                {"workspace_id": workspace_id},
            )
            rows = result.fetchall()
            return [dict(row._mapping) for row in rows]

    async def eval_scorecard_counts(self, *, workspace_id: str, window_days: int) -> dict:
        """Assemble the raw counts behind the operator scorecard for the trailing window."""
        async with self.session() as session:
            events_result = await session.execute(
                text(
                    """
                    SELECT COUNT(*) AS count, quality_verdict
                    FROM eval_query_events
                    WHERE workspace_id = :workspace_id
                      AND created_at > NOW() - make_interval(days => :days)
                    GROUP BY quality_verdict
                    """
                ),
                {"workspace_id": workspace_id, "days": window_days},
            )
            events_rows = events_result.fetchall()

            feedback_result = await session.execute(
                text(
                    """
                    SELECT COUNT(*) AS count, verdict
                    FROM eval_feedback
                    WHERE workspace_id = :workspace_id
                      AND created_at > NOW() - make_interval(days => :days)
                    GROUP BY verdict
                    """
                ),
                {"workspace_id": workspace_id, "days": window_days},
            )
            feedback_rows = feedback_result.fetchall()

            case_count_result = await session.execute(
                text(
                    "SELECT COUNT(*) FROM eval_cases WHERE workspace_id = :workspace_id AND active"
                ),
                {"workspace_id": workspace_id},
            )
            eval_case_count = case_count_result.scalar_one()

            gaps_result = await session.execute(
                text(
                    """
                    SELECT query_text FROM eval_feedback
                    WHERE workspace_id = :workspace_id AND verdict = 'not_relevant'
                    ORDER BY created_at DESC LIMIT 5
                    """
                ),
                {"workspace_id": workspace_id},
            )
            corpus_gaps = [row.query_text for row in gaps_result.fetchall()]

        captured_events = sum(row.count for row in events_rows)
        return {
            "captured_events": captured_events,
            "verdict_distribution": {
                row.quality_verdict: row.count
                for row in events_rows
                if row.quality_verdict is not None
            },
            "feedback_distribution": {row.verdict: row.count for row in feedback_rows},
            "eval_case_count": eval_case_count,
            "corpus_gaps": corpus_gaps,
        }

    async def insert_eval_run(
        self, *, run_id: str, workspace_id: str, case_count: int, k: int
    ) -> None:
        """Create the run row (status='running' by default) before replaying cases."""
        async with self.session() as session:
            await session.execute(
                text(
                    """
                    INSERT INTO eval_runs (run_id, workspace_id, case_count, k)
                    VALUES (:run_id, :workspace_id, :case_count, :k)
                    """
                ),
                {"run_id": run_id, "workspace_id": workspace_id, "case_count": case_count, "k": k},
            )
            await session.commit()

    async def finish_eval_run(
        self, *, run_id: str, status: str, aggregates: dict, error: str | None
    ) -> None:
        """Mark a run completed/failed and store its aggregate metrics."""
        async with self.session() as session:
            await session.execute(
                text(
                    """
                    UPDATE eval_runs
                    SET status = :status, aggregates = CAST(:aggregates AS jsonb),
                        error = :error, finished_at = NOW()
                    WHERE run_id = :run_id
                    """
                ),
                {
                    "run_id": run_id,
                    "status": status,
                    "aggregates": json.dumps(aggregates),
                    "error": error,
                },
            )
            await session.commit()

    async def insert_eval_run_results(self, *, run_id: str, rows: list[dict]) -> None:
        """Bulk-insert per-case, per-mode metrics for a run."""
        async with self.session() as session:
            await session.execute(
                text(
                    """
                    INSERT INTO eval_run_results (
                        run_id, case_id, query_text, mode, recall_at_k, mrr, ndcg_at_k
                    ) VALUES (
                        :run_id, :case_id, :query_text, :mode, :recall_at_k, :mrr, :ndcg_at_k
                    )
                    """
                ),
                [
                    {
                        "run_id": run_id,
                        "case_id": row["case_id"],
                        "query_text": row["query_text"],
                        "mode": row["mode"],
                        "recall_at_k": row["recall_at_k"],
                        "mrr": row["mrr"],
                        "ndcg_at_k": row["ndcg_at_k"],
                    }
                    for row in rows
                ],
            )
            await session.commit()

    async def get_eval_run(self, *, workspace_id: str, run_id: str) -> dict | None:
        """Fetch one run's metadata + aggregates, scoped to the workspace."""
        async with self.session() as session:
            result = await session.execute(
                text(
                    """
                    SELECT * FROM eval_runs
                    WHERE run_id = :run_id AND workspace_id = :workspace_id
                    """
                ),
                {"run_id": run_id, "workspace_id": workspace_id},
            )
            row = result.fetchone()
            return dict(row._mapping) if row else None

    async def get_eval_run_results(self, *, run_id: str) -> list[dict]:
        """Fetch per-case, per-mode results for a run (no workspace filter: the
        run_id already resolved through get_eval_run, which is workspace-scoped).
        """
        async with self.session() as session:
            result = await session.execute(
                text(
                    """
                    SELECT * FROM eval_run_results
                    WHERE run_id = :run_id
                    ORDER BY case_id, mode
                    """
                ),
                {"run_id": run_id},
            )
            rows = result.fetchall()
            return [dict(row._mapping) for row in rows]

    async def get_last_eval_run(self, *, workspace_id: str) -> dict | None:
        """Fetch the most recent run for a workspace (used by the scorecard)."""
        async with self.session() as session:
            result = await session.execute(
                text(
                    """
                    SELECT * FROM eval_runs
                    WHERE workspace_id = :workspace_id
                    ORDER BY created_at DESC LIMIT 1
                    """
                ),
                {"workspace_id": workspace_id},
            )
            row = result.fetchone()
            return dict(row._mapping) if row else None


# Singleton instance management
_database: DatabaseService | None = None


async def get_database() -> DatabaseService:
    """Get the database service singleton instance.

    Creates and initializes the database service on first call.
    Subsequent calls return the same instance.

    Returns:
        Initialized DatabaseService instance

    Raises:
        Exception: If database connection fails
    """
    global _database
    if _database is None:
        _database = DatabaseService()
        await _database.initialize()
    return _database


async def close_database() -> None:
    """Close the database connection and cleanup resources.

    Safe to call even if database was never initialized.
    """
    global _database
    if _database is not None:
        await _database.close()
        _database = None
