"""Weaviate v4 service for vector storage and search with multi-tenancy support.

Multi-tenancy Design:
- Each Workspace becomes a Weaviate Collection (e.g., Workspace_6953c161551d)
- Each User becomes a Tenant within that collection (e.g., User_6952cca0ac4118d)
- This enables efficient per-user data isolation within workspace-level organization
"""

import asyncio
import hashlib
import uuid
from datetime import UTC, datetime
from typing import Any

import structlog
import weaviate

# Weaviate naming now lives in the shared contracts package (single source of
# truth, #12). Re-exported here so existing imports keep working:
#   from src.services.weaviate import get_workspace_collection_name
from inh_contracts.naming import (
    WORKSPACE_COLLECTION_PREFIX,
    get_user_tenant_name,
    get_workspace_collection_name,
)
from weaviate.classes.config import Configure, DataType, Property
from weaviate.classes.init import Auth
from weaviate.classes.query import Filter, MetadataQuery
from weaviate.classes.tenants import Tenant, TenantActivityStatus

from src.config.settings import Settings
from src.models.document import DocumentChunk

__all__ = [
    "WeaviateService",
    "DOCUMENT_CHUNKS_COLLECTION",
    "WORKSPACE_COLLECTION_PREFIX",
    "get_workspace_collection_name",
    "get_user_tenant_name",
]

logger = structlog.get_logger(__name__)

# Legacy collection name (kept for backward compatibility)
DOCUMENT_CHUNKS_COLLECTION = "DocumentChunk"


class WeaviateService:
    """Weaviate v4 service for vector storage and search with multi-tenancy."""

    def __init__(self, settings: Settings):
        """Initialize Weaviate service."""
        self.settings = settings
        self.client: weaviate.WeaviateClient | None = None
        self._collection_cache: set[str] = set()
        self._tenant_cache: dict[str, set[str]] = {}  # collection -> set of tenants

    def connect(self) -> None:
        """Connect to Weaviate using v4 client."""
        try:
            # Parse URL to get host and port
            url = self.settings.weaviate_url

            # Remove protocol prefix
            if url.startswith("http://"):
                host = url[7:]
                use_https = False
            elif url.startswith("https://"):
                host = url[8:]
                use_https = True
            else:
                host = url
                use_https = False

            # Extract port if present
            if ":" in host:
                host_parts = host.split(":")
                hostname = host_parts[0]
                port = int(host_parts[1].split("/")[0])
            else:
                hostname = host.split("/")[0]
                port = 443 if use_https else 8080

            # Connect with or without API key
            if self.settings.weaviate_api_key:
                self.client = weaviate.connect_to_custom(
                    http_host=hostname,
                    http_port=port,
                    http_secure=use_https,
                    grpc_host=hostname,
                    grpc_port=50051,
                    grpc_secure=use_https,
                    auth_credentials=Auth.api_key(self.settings.weaviate_api_key),
                )
            else:
                self.client = weaviate.connect_to_custom(
                    http_host=hostname,
                    http_port=port,
                    http_secure=use_https,
                    grpc_host=hostname,
                    grpc_port=50051,
                    grpc_secure=use_https,
                )

            # Test connection
            if not self.client.is_ready():
                raise RuntimeError("Weaviate client is not ready")

            # Ensure legacy collection exists for backward compatibility
            self._ensure_legacy_collection_exists()

            logger.info("Connected to Weaviate", url=self.settings.weaviate_url)
        except Exception as e:
            logger.error("Failed to connect to Weaviate", error=str(e), exc_info=True)
            raise

    def _ensure_legacy_collection_exists(self) -> None:
        """Create the legacy DocumentChunk collection if it doesn't exist."""
        if not self.client:
            return

        try:
            if self.client.collections.exists(DOCUMENT_CHUNKS_COLLECTION):
                logger.debug("Legacy collection exists", collection=DOCUMENT_CHUNKS_COLLECTION)
                return

            self.client.collections.create(
                name=DOCUMENT_CHUNKS_COLLECTION,
                properties=self._get_chunk_properties(),
                vectorizer_config=Configure.Vectorizer.none(),
            )
            logger.info("Created legacy collection", collection=DOCUMENT_CHUNKS_COLLECTION)
        except Exception as e:
            logger.warning("Failed to create legacy collection", error=str(e))

    def _get_chunk_properties(self) -> list[Property]:
        """Get the standard properties for chunk collections."""
        return [
            Property(name="document_id", data_type=DataType.TEXT),
            Property(name="workspace_id", data_type=DataType.TEXT),
            Property(name="user_id", data_type=DataType.TEXT),
            Property(name="content", data_type=DataType.TEXT),
            Property(name="chunk_index", data_type=DataType.INT),
            Property(name="start_char", data_type=DataType.INT),
            Property(name="end_char", data_type=DataType.INT),
            Property(name="original_filename", data_type=DataType.TEXT),
            Property(name="content_type", data_type=DataType.TEXT),
            Property(name="created_at", data_type=DataType.DATE),
            # Provenance (#41): auditable evidence trail for returned chunks.
            Property(name="content_hash", data_type=DataType.TEXT),
            Property(name="source_uri", data_type=DataType.TEXT),
            # Freshness (#42): when the chunk was (re)ingested, so returned
            # evidence can be aged/flagged stale by the public API.
            Property(name="ingested_at", data_type=DataType.DATE),
            # RAG-poisoning / prompt-injection risk signal (#44): a heuristic,
            # NON-BLOCKING tag so search can surface and audit can count
            # suspicious evidence. content_risk is the level ("none".."high");
            # content_risk_reasons holds the matched reason codes.
            Property(name="content_risk", data_type=DataType.TEXT),
            Property(name="content_risk_reasons", data_type=DataType.TEXT_ARRAY),
        ]

    def _reconcile_collection_properties(self, collection_name: str) -> None:
        """Add any missing chunk properties to an EXISTING collection.

        Collections created before a property was introduced (e.g. the
        provenance/freshness/risk fields from #41/#42/#44) lack it. Because the
        public-API search GraphQL-selects those fields, a missing property makes
        the whole query fail with "Cannot query field ... on type ...". Weaviate
        supports adding properties to an existing class, so we reconcile the live
        schema against _get_chunk_properties() and add whatever is missing.
        Idempotent and best-effort (logged, never raises).
        """
        if not self.client:
            return
        try:
            collection = self.client.collections.get(collection_name)
            existing = {p.name for p in collection.config.get().properties}
            for prop in self._get_chunk_properties():
                if prop.name not in existing:
                    collection.config.add_property(prop)
                    logger.info(
                        "Added missing Weaviate property to existing collection",
                        collection=collection_name,
                        property_name=prop.name,
                    )
        except Exception as e:
            logger.warning(
                "Failed to reconcile collection properties; "
                "search selecting new fields may fail until fixed",
                collection=collection_name,
                error=str(e),
            )

    def disconnect(self) -> None:
        """Disconnect from Weaviate."""
        if self.client:
            try:
                self.client.close()
            except Exception as e:
                logger.warning("Error closing Weaviate client", error=str(e))
            self.client = None
        self._collection_cache.clear()
        self._tenant_cache.clear()
        logger.info("Disconnected from Weaviate")

    def is_connected(self) -> bool:
        """Check if connected to Weaviate."""
        return self.client is not None and self.client.is_ready()

    # =========================================================================
    # Multi-Tenancy Methods
    # =========================================================================

    async def ensure_workspace_collection(self, workspace_id: str) -> str:
        """Create a workspace-specific collection if it doesn't exist.

        Args:
            workspace_id: The workspace identifier

        Returns:
            The collection name that was created or already exists
        """
        if not self.client:
            raise RuntimeError("Weaviate not connected")

        collection_name = get_workspace_collection_name(workspace_id)

        # Check cache first
        if collection_name in self._collection_cache:
            return collection_name

        try:
            if self.client.collections.exists(collection_name):
                # Reconcile schema so collections created before newer chunk
                # properties (provenance/freshness/risk) gain them; otherwise a
                # search selecting those fields fails on the old schema.
                self._reconcile_collection_properties(collection_name)
                self._collection_cache.add(collection_name)
                logger.debug("Workspace collection exists", collection=collection_name)
                return collection_name

            # Create collection with multi-tenancy enabled
            self.client.collections.create(
                name=collection_name,
                properties=self._get_chunk_properties(),
                vectorizer_config=Configure.Vectorizer.none(),
                # Enable multi-tenancy for user isolation
                multi_tenancy_config=Configure.multi_tenancy(
                    enabled=True,
                    auto_tenant_creation=False,  # We manage tenant creation explicitly
                    auto_tenant_activation=True,  # Auto-activate on access
                ),
            )

            self._collection_cache.add(collection_name)
            logger.info(
                "Created workspace collection with multi-tenancy",
                collection=collection_name,
                workspace_id=workspace_id,
            )
            return collection_name

        except Exception as e:
            # Handle race condition - collection might have been created by another process
            if "already exists" in str(e).lower():
                self._collection_cache.add(collection_name)
                return collection_name
            logger.error(
                "Failed to create workspace collection",
                collection=collection_name,
                error=str(e),
                exc_info=True,
            )
            raise

    async def ensure_user_tenant(self, workspace_id: str, user_id: str) -> str:
        """Create or activate a user tenant within a workspace collection.

        Args:
            workspace_id: The workspace identifier
            user_id: The user identifier

        Returns:
            The tenant name
        """
        if not self.client:
            raise RuntimeError("Weaviate not connected")

        collection_name = get_workspace_collection_name(workspace_id)
        tenant_name = get_user_tenant_name(user_id)

        # Check cache first
        if collection_name in self._tenant_cache:
            if tenant_name in self._tenant_cache[collection_name]:
                return tenant_name

        try:
            collection = self.client.collections.get(collection_name)

            # Get existing tenants
            existing_tenants = collection.tenants.get()
            existing_tenant_names = (
                {t.name for t in existing_tenants.values()} if existing_tenants else set()
            )

            if tenant_name in existing_tenant_names:
                # Tenant exists, ensure it's active
                tenant_obj = existing_tenants.get(tenant_name)
                if tenant_obj and tenant_obj.activity_status != TenantActivityStatus.ACTIVE:
                    collection.tenants.update(
                        [Tenant(name=tenant_name, activity_status=TenantActivityStatus.ACTIVE)]
                    )
                    logger.info(
                        "Activated user tenant", tenant=tenant_name, collection=collection_name
                    )
            else:
                # Create new tenant
                collection.tenants.create(
                    [Tenant(name=tenant_name, activity_status=TenantActivityStatus.ACTIVE)]
                )
                logger.info(
                    "Created user tenant",
                    tenant=tenant_name,
                    collection=collection_name,
                    user_id=user_id,
                )

            # Update cache
            if collection_name not in self._tenant_cache:
                self._tenant_cache[collection_name] = set()
            self._tenant_cache[collection_name].add(tenant_name)

            return tenant_name

        except Exception as e:
            # Handle race condition
            if "already exists" in str(e).lower():
                if collection_name not in self._tenant_cache:
                    self._tenant_cache[collection_name] = set()
                self._tenant_cache[collection_name].add(tenant_name)
                return tenant_name
            logger.error(
                "Failed to ensure user tenant",
                tenant=tenant_name,
                collection=collection_name,
                error=str(e),
                exc_info=True,
            )
            raise

    async def deactivate_user_tenant(self, workspace_id: str, user_id: str) -> bool:
        """Deactivate a user tenant for cost optimization.

        Args:
            workspace_id: The workspace identifier
            user_id: The user identifier

        Returns:
            True if deactivated successfully
        """
        if not self.client:
            raise RuntimeError("Weaviate not connected")

        collection_name = get_workspace_collection_name(workspace_id)
        tenant_name = get_user_tenant_name(user_id)

        try:
            collection = self.client.collections.get(collection_name)
            collection.tenants.update(
                [Tenant(name=tenant_name, activity_status=TenantActivityStatus.INACTIVE)]
            )

            # Remove from cache
            if collection_name in self._tenant_cache:
                self._tenant_cache[collection_name].discard(tenant_name)

            logger.info(
                "Deactivated user tenant",
                tenant=tenant_name,
                collection=collection_name,
            )
            return True

        except Exception as e:
            logger.error(
                "Failed to deactivate user tenant",
                tenant=tenant_name,
                error=str(e),
            )
            return False

    async def delete_workspace_collection(self, workspace_id: str) -> bool:
        """Delete an entire workspace collection.

        Args:
            workspace_id: The workspace identifier

        Returns:
            True if deleted successfully
        """
        if not self.client:
            raise RuntimeError("Weaviate not connected")

        collection_name = get_workspace_collection_name(workspace_id)

        try:
            if self.client.collections.exists(collection_name):
                self.client.collections.delete(collection_name)
                self._collection_cache.discard(collection_name)
                self._tenant_cache.pop(collection_name, None)
                logger.info(
                    "Deleted workspace collection",
                    collection=collection_name,
                    workspace_id=workspace_id,
                )
                return True
            return False

        except Exception as e:
            logger.error(
                "Failed to delete workspace collection",
                collection=collection_name,
                error=str(e),
            )
            return False

    # =========================================================================
    # Multi-Tenant Storage Methods
    # =========================================================================

    async def store_chunks_with_tenant(
        self,
        chunks: list[DocumentChunk],
        document_id: str,
        workspace_id: str,
        user_id: str,
        original_filename: str,
        content_type: str,
        source_uri: str | None = None,
    ) -> int:
        """Store document chunks in a workspace collection with user tenant.

        Args:
            chunks: List of DocumentChunk objects
            document_id: ID of the source document
            workspace_id: Workspace ID (determines collection)
            user_id: User ID (determines tenant)
            original_filename: Original filename
            content_type: MIME type
            source_uri: Provenance (#41) — where the source bytes live
                (storage_path / storage_url). Optional/backward-compatible.

        Returns:
            Number of chunks stored
        """
        if not self.client:
            raise RuntimeError("Weaviate not connected")

        # Ensure collection and tenant exist
        collection_name = await self.ensure_workspace_collection(workspace_id)
        tenant_name = await self.ensure_user_tenant(workspace_id, user_id)

        collection = self.client.collections.get(collection_name)
        stored_count = 0

        try:
            # Use tenant-scoped operations
            tenant_collection = collection.with_tenant(tenant_name)

            # Compute embeddings in one batch (much faster than per-chunk).
            # embed_texts does blocking HTTP to the TEI sidecar, so offload it to
            # a thread — otherwise it stalls the event loop (and every other
            # coroutine) for the whole document's embedding round-trip (#19).
            from src.services.embedder import embed_texts

            chunk_texts = [c.content for c in chunks]
            vectors = await asyncio.to_thread(embed_texts, chunk_texts)

            # Single ingest timestamp for this store call (#42): all chunks of a
            # document share one ingested_at so freshness is consistent per store.
            ingest_time = datetime.now(UTC)

            with tenant_collection.batch.dynamic() as batch:
                for chunk, vector in zip(chunks, vectors):
                    # RAG-poisoning risk signal (#44): promote from chunk.metadata
                    # (set by the store activity) onto Weaviate properties so the
                    # public API can surface it. Defaults keep benign chunks clean.
                    chunk_meta = chunk.metadata or {}
                    content_risk = chunk_meta.get("content_risk") or "none"
                    content_risk_reasons = list(chunk_meta.get("content_risk_reasons") or [])

                    properties = {
                        "document_id": document_id,
                        "workspace_id": workspace_id,
                        "user_id": user_id,
                        "content": chunk.content,
                        "chunk_index": chunk.chunk_index,
                        "start_char": chunk.start_char,
                        "end_char": chunk.end_char,
                        "original_filename": original_filename,
                        "content_type": content_type,
                        "created_at": ingest_time,
                        # Provenance (#41): auditable evidence trail.
                        "content_hash": hashlib.sha256(chunk.content.encode("utf-8")).hexdigest(),
                        "source_uri": source_uri,
                        # Freshness (#42): stamp ingest time so the public API can
                        # age returned evidence. Matches the PG document_chunks
                        # ingested_at; a refresh re-stores chunks with a new value.
                        "ingested_at": ingest_time,
                        # Risk signal (#44): additive, NON-BLOCKING.
                        "content_risk": content_risk,
                        "content_risk_reasons": content_risk_reasons,
                    }

                    # Generate deterministic UUID
                    chunk_uuid = uuid.uuid5(
                        uuid.NAMESPACE_DNS,
                        f"{workspace_id}:{user_id}:{document_id}:{chunk.chunk_index}",
                    )

                    batch.add_object(
                        properties=properties,  # type: ignore[arg-type]
                        uuid=chunk_uuid,
                        vector=vector,
                    )

            # The v4 batch collects per-object errors in failed_objects instead
            # of raising, so a partial failure would otherwise be reported as a
            # full success -> Postgres/Weaviate divergence with no error (#8).
            # Raise so the store activity retries / dead-letters (see #2).
            failed = tenant_collection.batch.failed_objects
            if failed:
                first = getattr(failed[0], "message", failed[0])
                raise RuntimeError(
                    f"Weaviate batch store failed for {len(failed)}/{len(chunks)} "
                    f"chunks in document {document_id}: {first}"
                )
            stored_count = len(chunks)

            logger.info(
                "Stored chunks in Weaviate with multi-tenancy",
                document_id=document_id,
                workspace_id=workspace_id,
                user_id=user_id,
                collection=collection_name,
                tenant=tenant_name,
                chunk_count=stored_count,
            )
            return stored_count

        except Exception as e:
            logger.error(
                "Failed to store chunks with tenant",
                document_id=document_id,
                collection=collection_name,
                tenant=tenant_name,
                error=str(e),
                exc_info=True,
            )
            raise

    async def update_chunk(
        self,
        document_id: str,
        chunk_index: int,
        content: str,
        workspace_id: str,
        user_id: str,
    ) -> None:
        """Update a single chunk's content in Weaviate (re-embeds automatically).

        Uses the same deterministic UUID as store_chunks_with_tenant so we
        can update in place.
        """
        if not self.client:
            raise RuntimeError("Weaviate not connected")

        collection_name = get_workspace_collection_name(workspace_id)
        tenant_name = get_user_tenant_name(user_id)

        chunk_uuid = uuid.uuid5(
            uuid.NAMESPACE_DNS,
            f"{workspace_id}:{user_id}:{document_id}:{chunk_index}",
        )

        collection = self.client.collections.get(collection_name)
        tenant_collection = collection.with_tenant(tenant_name)

        tenant_collection.data.update(
            uuid=chunk_uuid,
            properties={"content": content},
        )

        logger.info(
            "Updated chunk in Weaviate",
            document_id=document_id,
            chunk_index=chunk_index,
            collection=collection_name,
        )

    async def delete_document_chunks_with_tenant(
        self,
        document_id: str,
        workspace_id: str,
        user_id: str,
    ) -> int:
        """Delete all chunks for a document within a tenant.

        Args:
            document_id: ID of the document
            workspace_id: Workspace ID
            user_id: User ID

        Returns:
            Number of chunks deleted
        """
        if not self.client:
            raise RuntimeError("Weaviate not connected")

        collection_name = get_workspace_collection_name(workspace_id)
        tenant_name = get_user_tenant_name(user_id)

        try:
            collection = self.client.collections.get(collection_name)
            tenant_collection = collection.with_tenant(tenant_name)

            result = tenant_collection.data.delete_many(
                where=Filter.by_property("document_id").equal(document_id)
            )

            deleted_count = result.successful if hasattr(result, "successful") else 0
            logger.info(
                "Deleted chunks from tenant",
                document_id=document_id,
                collection=collection_name,
                tenant=tenant_name,
                deleted_count=deleted_count,
            )
            return deleted_count

        except Exception as e:
            logger.error(
                "Failed to delete chunks from tenant",
                document_id=document_id,
                error=str(e),
            )
            raise

    async def delete_document_chunks_graceful(
        self,
        workspace_id: str,
        document_id: str,
        user_id: str,
    ) -> tuple[bool, int]:
        """Delete all chunks for a document from Weaviate, handling errors gracefully.

        Unlike delete_document_chunks_with_tenant(), this method never raises.
        It logs a warning if Weaviate is unavailable or the delete fails and
        returns a success flag so callers can report partial success.

        Args:
            workspace_id: Workspace ID (determines collection)
            document_id: ID of the document whose chunks should be removed
            user_id: User ID (determines tenant)

        Returns:
            Tuple of (success: bool, deleted_count: int)
        """
        try:
            if not self.client or not self.client.is_ready():
                logger.warning(
                    "Weaviate not available for chunk deletion",
                    document_id=document_id,
                    workspace_id=workspace_id,
                )
                return False, 0

            deleted = await self.delete_document_chunks_with_tenant(
                document_id=document_id,
                workspace_id=workspace_id,
                user_id=user_id,
            )
            return True, deleted

        except Exception as e:
            logger.warning(
                "Weaviate chunk deletion failed (non-fatal)",
                document_id=document_id,
                workspace_id=workspace_id,
                user_id=user_id,
                error=str(e),
            )
            return False, 0

    async def search_chunks_with_tenant(
        self,
        query: str,
        workspace_id: str,
        user_id: str,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Search for chunks within a user's tenant in a workspace.

        Args:
            query: Search query
            workspace_id: Workspace ID
            user_id: User ID
            limit: Maximum results

        Returns:
            List of matching chunks with metadata
        """
        if not self.client:
            raise RuntimeError("Weaviate not connected")

        collection_name = get_workspace_collection_name(workspace_id)
        tenant_name = get_user_tenant_name(user_id)

        try:
            collection = self.client.collections.get(collection_name)
            tenant_collection = collection.with_tenant(tenant_name)

            # BM25 search within tenant
            results = tenant_collection.query.bm25(
                query=query,
                limit=limit,
                return_metadata=MetadataQuery(score=True),
            )

            chunks = []
            for obj in results.objects:
                chunk_data = {
                    "uuid": str(obj.uuid),
                    "score": obj.metadata.score if obj.metadata else None,
                    **obj.properties,
                }
                chunks.append(chunk_data)

            return chunks

        except Exception as e:
            logger.error(
                "Search failed in tenant",
                query=query,
                collection=collection_name,
                tenant=tenant_name,
                error=str(e),
            )
            raise

    async def get_all_chunks_for_document(
        self,
        document_id: str,
        workspace_id: str,
        user_id: str,
    ) -> list[dict[str, Any]]:
        """Get all chunks for a specific document.

        Args:
            document_id: Document ID
            workspace_id: Workspace ID
            user_id: User ID

        Returns:
            List of all chunks for the document
        """
        if not self.client:
            raise RuntimeError("Weaviate not connected")

        collection_name = get_workspace_collection_name(workspace_id)
        tenant_name = get_user_tenant_name(user_id)

        try:
            collection = self.client.collections.get(collection_name)
            tenant_collection = collection.with_tenant(tenant_name)

            results = tenant_collection.query.fetch_objects(
                filters=Filter.by_property("document_id").equal(document_id),
                limit=10000,  # High limit to get all chunks
            )

            chunks = []
            for obj in results.objects:
                chunk_data = {
                    "uuid": str(obj.uuid),
                    **obj.properties,
                }
                chunks.append(chunk_data)

            # Sort by chunk_index
            chunks.sort(key=lambda x: int(x.get("chunk_index", 0)))  # type: ignore[arg-type]
            return chunks

        except Exception as e:
            logger.error(
                "Failed to get document chunks",
                document_id=document_id,
                error=str(e),
            )
            raise

    # =========================================================================
    # Legacy Methods (Backward Compatibility)
    # =========================================================================

    async def store_chunks(
        self,
        chunks: list[DocumentChunk],
        document_id: str,
        workspace_id: str,
        user_id: str,
        original_filename: str,
        content_type: str,
        source_uri: str | None = None,
    ) -> int:
        """Store document chunks - routes to multi-tenant storage.

        This method now uses multi-tenant storage by default.
        """
        return await self.store_chunks_with_tenant(
            chunks=chunks,
            document_id=document_id,
            workspace_id=workspace_id,
            user_id=user_id,
            original_filename=original_filename,
            content_type=content_type,
            source_uri=source_uri,
        )

    async def delete_document_chunks(self, document_id: str) -> int:
        """Delete all chunks for a document from legacy collection.

        Note: For multi-tenant deletion, use delete_document_chunks_with_tenant()
        """
        if not self.client:
            raise RuntimeError("Weaviate not connected")

        collection = self.client.collections.get(DOCUMENT_CHUNKS_COLLECTION)

        try:
            result = collection.data.delete_many(
                where=Filter.by_property("document_id").equal(document_id)
            )

            deleted_count = result.successful if hasattr(result, "successful") else 0
            logger.info(
                "Deleted chunks from legacy collection",
                document_id=document_id,
                deleted_count=deleted_count,
            )
            return deleted_count

        except Exception as e:
            logger.error(
                "Failed to delete chunks from legacy collection",
                document_id=document_id,
                error=str(e),
            )
            raise

    async def search_chunks(
        self,
        query: str,
        workspace_id: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Search for chunks in legacy collection.

        Note: For multi-tenant search, use search_chunks_with_tenant()
        """
        if not self.client:
            raise RuntimeError("Weaviate not connected")

        collection = self.client.collections.get(DOCUMENT_CHUNKS_COLLECTION)

        try:
            filters = None
            if workspace_id:
                filters = Filter.by_property("workspace_id").equal(workspace_id)

            results = collection.query.bm25(
                query=query,
                limit=limit,
                filters=filters,
                return_metadata=MetadataQuery(score=True),
            )

            chunks = []
            for obj in results.objects:
                chunk_data = {
                    "uuid": str(obj.uuid),
                    "score": obj.metadata.score if obj.metadata else None,
                    **obj.properties,
                }
                chunks.append(chunk_data)

            return chunks

        except Exception as e:
            logger.error("Search failed", query=query, error=str(e))
            raise

    # =========================================================================
    # Utility Methods
    # =========================================================================

    def list_workspace_collections(self) -> list[str]:
        """List all workspace collections.

        Returns:
            List of workspace collection names
        """
        if not self.client:
            raise RuntimeError("Weaviate not connected")

        try:
            all_collections = self.client.collections.list_all()
            workspace_collections = [
                name
                for name in all_collections.keys()
                if name.startswith(WORKSPACE_COLLECTION_PREFIX)
            ]
            return workspace_collections

        except Exception as e:
            logger.error("Failed to list collections", error=str(e))
            return []

    async def get_tenant_stats(self, workspace_id: str) -> dict[str, Any]:
        """Get statistics for all tenants in a workspace collection.

        Args:
            workspace_id: Workspace ID

        Returns:
            Dictionary with tenant statistics
        """
        if not self.client:
            raise RuntimeError("Weaviate not connected")

        collection_name = get_workspace_collection_name(workspace_id)

        try:
            collection = self.client.collections.get(collection_name)
            tenants = collection.tenants.get()

            stats = {
                "collection": collection_name,
                "workspace_id": workspace_id,
                "tenant_count": len(tenants) if tenants else 0,
                "tenants": [],
            }

            if tenants:
                tenant_list: list[dict[str, str]] = []
                for tenant_name, tenant_obj in tenants.items():
                    tenant_list.append(
                        {
                            "name": tenant_name,
                            "status": (
                                tenant_obj.activity_status.name
                                if tenant_obj.activity_status
                                else "UNKNOWN"
                            ),
                        }
                    )
                stats["tenants"] = tenant_list  # type: ignore[assignment]

            return stats

        except Exception as e:
            logger.error(
                "Failed to get tenant stats",
                collection=collection_name,
                error=str(e),
            )
            return {"error": str(e)}
