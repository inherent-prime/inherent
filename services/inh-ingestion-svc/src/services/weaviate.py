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
from inh_contracts.embedding.identity import (
    EmbeddingIdentityMismatchError,
    decode_identity,
    encode_identity,
    resolve_identity,
)
from inh_contracts.naming import (
    WORKSPACE_COLLECTION_PREFIX,
    get_user_tenant_name,
    get_workspace_collection_name,
)
from temporalio import activity
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


def _heartbeat_embed_progress(completed_batches: int, total_batches: int) -> None:
    """Heartbeat with real embedding progress (#298).

    Passed as ``embed_texts_with_progress``'s ``on_batch_done`` callback in
    ``store_chunks_with_tenant`` below. Only takes effect inside a Temporal
    activity -- ``store_chunks_with_tenant`` is also called directly by
    ``processor.py`` and ``reindex_from_postgres.py`` outside any activity,
    where ``activity.heartbeat()`` raises "not in an activity". Progress is
    a genuine batch-completion count, not a timer, so the heartbeat_timeout
    set on the store_in_weaviate activity (see
    ``weaviate_store_budget.weaviate_store_heartbeat_timeout``) still
    detects a worker that has actually stopped making progress rather than
    one that is merely slow.
    """
    if activity.in_activity():
        activity.heartbeat(
            {"chunk_batches_done": completed_batches, "chunk_batches_total": total_batches}
        )


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
                collection = self.client.collections.get(DOCUMENT_CHUNKS_COLLECTION)
                self._check_or_stamp_collection_identity(collection, DOCUMENT_CHUNKS_COLLECTION)
                return

            from src.services.embedder import get_active_embedding_identity

            current_identity = get_active_embedding_identity()
            self.client.collections.create(
                name=DOCUMENT_CHUNKS_COLLECTION,
                properties=self._get_chunk_properties(),
                vectorizer_config=Configure.Vectorizer.none(),
                # Stamp the active embedding identity at creation time (#311
                # item 4) so a mismatch is caught the moment a different
                # model/provider is later pointed at this same collection.
                description=encode_identity(current_identity),
            )
            logger.info("Created legacy collection", collection=DOCUMENT_CHUNKS_COLLECTION)
        except EmbeddingIdentityMismatchError:
            # Always a hard error (#311 item 4) -- never swallow into the
            # best-effort warning below, which exists for genuine
            # connectivity/schema failures only.
            raise
        except Exception as e:
            logger.warning("Failed to create legacy collection", error=str(e))

    def _collection_is_empty(self, collection: Any) -> bool:
        """Best-effort, CONSERVATIVE "does this collection hold any data" check.

        Used only to gate the legacy-adopt policy (#311 PR #314 review
        finding 3): adopting an unstamped collection is safe when there is
        nothing yet that could be wrong -- an empty collection cannot hold
        vectors written by a different model. Errs toward "NOT empty" (the
        conservative branch, which routes through the
        ``EMBEDDING_ADOPT_UNSTAMPED_COLLECTIONS`` opt-in instead of adopting
        silently) on anything this cannot definitively prove empty:

        - Non-multi-tenant collections (e.g. the legacy
          ``DOCUMENT_CHUNKS_COLLECTION``): a plain aggregate object count.
        - Multi-tenant collections (per-workspace, #12): Weaviate's aggregate
          endpoint needs a tenant to scope to, and checking "does ANY of
          potentially many tenants hold an object" is not one cheap call.
          The proxy used instead is "does at least one tenant exist" --
          every write requires a tenant to exist first, so zero tenants is a
          safe, EXACT "empty". Any tenant existing is conservatively treated
          as "not proven empty", even if that specific tenant holds nothing.
        - Anything that raises while checking (schema/tenant-list call
          failure) is treated as NOT proven empty -- fail closed, same
          direction as the two cases above.
        """
        try:
            config = collection.config.get()
            mt_config = config.multi_tenancy_config
            if mt_config is not None and mt_config.enabled:
                tenants = collection.tenants.get()
                return not tenants
            result = collection.aggregate.over_all(total_count=True)
            return (result.total_count or 0) == 0
        except Exception as exc:  # noqa: BLE001 -- best-effort, see docstring
            logger.warning("embedding_identity_emptiness_check_failed", error=str(exc))
            return False

    def _check_or_stamp_collection_identity(self, collection: Any, collection_name: str) -> None:
        """Assert (or adopt) a collection's persisted embedding identity (#311 item 4).

        The active provider's (model_id, dimension) is persisted as the
        collection's Weaviate ``description`` (see
        ``inh_contracts.embedding.identity`` for the encode/decode format and
        the full policy write-up). Outcomes:

        - No persisted identity, collection is EMPTY -> ADOPT silently: stamp
          the collection with the active identity now via ``config.update``.
          Nothing to be wrong about yet. This is what keeps a FRESH
          deployment working with zero manual migration.
        - No persisted identity, collection is NOT empty -> refuse by
          default (PR #314 review finding 3: adopting here would silently
          CERTIFY whatever model wrote the existing vectors as the current
          one, e.g. an operator upgrading and switching providers in the
          same deploy). Raises ``EmbeddingIdentityAdoptionRequiredError`` (a
          subclass of ``EmbeddingIdentityMismatchError`` -- see its
          docstring for why) UNLESS the operator opted in via
          ``EMBEDDING_ADOPT_UNSTAMPED_COLLECTIONS=true``, in which case it
          adopts anyway and logs loudly that it did.
        - Persisted identity matches -> return, nothing to do.
        - Persisted identity does NOT match -> raise
          ``EmbeddingIdentityMismatchError``. ALWAYS a hard error, never a
          warning -- callers must not swallow it (see the two call sites
          below, both of which are inside broad best-effort ``except``
          blocks that explicitly re-raise this one exception type -- the
          adoption-required error above is caught by the same guards, being
          a subclass).
        """
        from src.services.embedder import get_active_embedding_identity

        current = get_active_embedding_identity()
        persisted = decode_identity(collection.config.get().description)
        is_empty: bool | None = None
        allow_adopt = self.settings.embedding_adopt_unstamped_collections
        if persisted is None:
            is_empty = self._collection_is_empty(collection)
            if not is_empty and allow_adopt:
                logger.warning(
                    "embedding_identity_adopted_unstamped_nonempty_collection",
                    collection=collection_name,
                    model_id=current.model_id,
                    dimension=current.dimension,
                    reason="EMBEDDING_ADOPT_UNSTAMPED_COLLECTIONS=true operator opt-in -- "
                    "existing vectors were NOT verified to match the active provider",
                )
        resolved = resolve_identity(
            persisted=persisted,
            current=current,
            collection_name=collection_name,
            is_empty=is_empty,
            allow_adopt_unstamped=allow_adopt,
        )
        if persisted is None:
            collection.config.update(description=encode_identity(resolved))
            logger.info(
                "embedding_identity_stamped",
                collection=collection_name,
                model_id=resolved.model_id,
                dimension=resolved.dimension,
                was_empty=is_empty,
            )

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
            # Format-aware chunking attribution (#129): which strategy
            # ("rows" | "sections" | "prose_header" | "sentences" |
            # "paragraphs" | "tokens") actually produced this chunk, so the
            # #34 eval suite can measure retrieval quality per strategy, not
            # just per file type. Same promote-from-chunk-metadata pattern
            # as content_risk above. `index_searchable=False` (#129 follow-up
            # item 11): this is a closed enum of internal strategy names, not
            # prose meant to be keyword-matched -- without this it silently
            # enters the BM25 index (Weaviate's TEXT default is
            # searchable=True) and a literal query for e.g. "tokens" would
            # start matching on internal metadata nobody selects today (#196
            # -- the field isn't even surfaced in search results yet).
            # `index_filterable` stays at its default (True): filtering
            # ("only rows-strategy chunks") is a legitimate, cheap use this
            # field should keep supporting once #196 wires it through.
            Property(name="chunking_strategy", data_type=DataType.TEXT, index_searchable=False),
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
                # #311 item 4: assert (or adopt, for a legacy pre-#311
                # collection) the persisted embedding identity BEFORE this
                # collection is cached as usable -- a mismatch here must
                # raise, never get cached over.
                collection = self.client.collections.get(collection_name)
                self._check_or_stamp_collection_identity(collection, collection_name)
                self._collection_cache.add(collection_name)
                logger.debug("Workspace collection exists", collection=collection_name)
                return collection_name

            from src.services.embedder import get_active_embedding_identity

            current_identity = get_active_embedding_identity()
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
                # Stamp the active embedding identity at creation time (#311
                # item 4) so a later mismatched provider/model is caught
                # instead of silently writing into the same vector space.
                description=encode_identity(current_identity),
            )

            self._collection_cache.add(collection_name)
            logger.info(
                "Created workspace collection with multi-tenancy",
                collection=collection_name,
                workspace_id=workspace_id,
            )
            return collection_name

        except EmbeddingIdentityMismatchError:
            # Always a hard error (#311 item 4) -- must not be caught by the
            # generic "already exists" race handling or the catch-all log+
            # raise below (which still re-raises, but this makes the intent
            # explicit and skips the "already exists" string-match entirely).
            raise
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

            # Compute embeddings in batches (much faster than per-chunk). Each
            # batch's blocking TEI HTTP call is offloaded to a thread so it
            # never stalls the event loop (#19) -- but the batch *loop* itself
            # runs here, on this coroutine, so store_in_weaviate can heartbeat
            # with real per-batch progress instead of going dark for the whole
            # document's embed (#298: a 60k-chunk document could otherwise
            # never finish inside any activity budget short enough to also
            # catch a genuinely wedged worker fast).
            from src.services.embedder import embed_texts_with_progress

            chunk_texts = [c.content for c in chunks]
            vectors = await embed_texts_with_progress(
                chunk_texts, on_batch_done=_heartbeat_embed_progress
            )

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
                    # Format-aware chunking attribution (#129): same
                    # promote-from-metadata pattern as content_risk above.
                    # Empty string (not None) for a chunk staged before this
                    # field existed, so the property is always a real TEXT
                    # value Weaviate can filter/aggregate on.
                    chunking_strategy = chunk_meta.get("chunking_strategy") or ""

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
                        "chunking_strategy": chunking_strategy,
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
        """Update a single chunk's content, hash, timestamp, AND embedding (#137).

        Uses the same deterministic UUID as store_chunks_with_tenant so we
        can update in place.

        Chunk vectors are supplied explicitly at store time -- this
        collection has no server-side vectorizer (Configure.Vectorizer.none()
        in _get_chunk_properties()/ensure_workspace_collection), so Weaviate
        never re-embeds on its own. A ``data.update`` that only sets the
        ``content`` property therefore leaves the OLD vector attached to the
        NEW text: semantic search keeps matching on stale content while
        get_document/list_chunks (which read PG) already show the edit. We
        re-embed here so the stored vector and the stored text never diverge.

        Also advances ``content_hash`` and ``ingested_at`` alongside
        ``content`` -- mirroring exactly what update_chunk_postgresql already
        does for the PG row (#9). Before this, an edit updated content here
        but left the OLD content_hash in place: the public API's
        content_hash contract (services/inh-public-api-svc/src/models/
        search.py) is "sha256 of the *returned* content", so a legitimately
        edited chunk would read back as tampered evidence, and a stale
        ingested_at could keep reporting is_stale=true right after a fresh
        edit.
        """
        if not self.client:
            raise RuntimeError("Weaviate not connected")

        collection_name = get_workspace_collection_name(workspace_id)
        tenant_name = get_user_tenant_name(user_id)

        chunk_uuid = uuid.uuid5(
            uuid.NAMESPACE_DNS,
            f"{workspace_id}:{user_id}:{document_id}:{chunk_index}",
        )

        # #311 item 4: fetch the collection and assert (or adopt) its
        # persisted embedding identity BEFORE re-embedding -- a mismatch
        # raises here, failing fast without wasting a network round-trip on
        # an embed call whose result could never be safely written anyway.
        # store_chunks_with_tenant already did this check via
        # ensure_workspace_collection when the document was first ingested;
        # re-checking here (cheaply short-circuited by _collection_cache) is
        # what protects a standalone edit reaching a fresh WeaviateService
        # instance that never called ensure_workspace_collection.
        collection = self.client.collections.get(collection_name)
        if collection_name not in self._collection_cache:
            self._check_or_stamp_collection_identity(collection, collection_name)
            self._collection_cache.add(collection_name)

        # Re-embed the new content. embed_text does blocking HTTP to the TEI
        # sidecar, so offload it to a thread -- same reasoning as the batch
        # embed in store_chunks_with_tenant (#19): otherwise this stalls the
        # event loop for the whole embedding round-trip.
        from src.services.embedder import embed_text

        vector = await asyncio.to_thread(embed_text, content)

        tenant_collection = collection.with_tenant(tenant_name)

        tenant_collection.data.update(
            uuid=chunk_uuid,
            properties={
                "content": content,
                # Same hash formula as the store path / update_chunk_postgresql
                # (#41/#9) -- keeps the evidence hash consistent with the new
                # content instead of flagging a legitimate edit as tampered.
                "content_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                # Freshness (#42): bump so a just-edited chunk isn't reported
                # stale using the pre-edit ingest time.
                "ingested_at": datetime.now(UTC),
            },
            vector=vector,
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
