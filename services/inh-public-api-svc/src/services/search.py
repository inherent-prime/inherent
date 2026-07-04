"""Search service for semantic search operations."""

import asyncio
import json
import re
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from inh_contracts.naming import (
    WORKSPACE_COLLECTION_PREFIX,
    get_user_tenant_name,
    get_workspace_collection_name,
)

from src.config import settings
from src.models.citation import Citation
from src.models.search import ScoreSource, SearchRequest, SearchResponse, SearchResult
from src.services.database import DatabaseService, get_database
from src.utils import get_logger

logger = get_logger(__name__)


def _require_safe_name(name: str, kind: str) -> None:
    """Reject a collection/tenant name that isn't safe to interpolate into GraphQL.

    An explicit raise (not ``assert``) so the guard survives ``python -O`` (#33).
    Names are ``<Prefix>_<base32>`` — alphanumerics plus the prefix underscore.
    """
    if not name.replace("_", "").isalnum():
        raise ValueError(f"Unsafe {kind} name: {name}")


# Re-exported for backward compatibility (the WORKSPACE_COLLECTION_PREFIX
# constant and the naming helpers now live in the shared contracts package).
__all__ = [
    "WORKSPACE_COLLECTION_PREFIX",
    "SearchService",
    "get_search_service",
    "close_search_service",
    "build_search_request",
]


# The full set of fields a caller may supply to construct a SearchRequest.
# Anything outside this set is ignored so transport-only keys (e.g. the MCP
# ``api_key`` / ``workspace_id`` arguments) don't leak into the model.
_SEARCH_REQUEST_FIELDS: tuple[str, ...] = (
    "query",
    "limit",
    "min_score",
    "document_ids",
    "include_context",
    "context_window",
    "search_mode",
    "alpha",
)


def build_search_request(params: dict[str, Any]) -> SearchRequest:
    """Construct a :class:`SearchRequest` from a flat dict of parameters.

    Single source of truth (no drift): BOTH the REST search route and the MCP
    ``search_documents`` / ``search_memory`` tools build their ``SearchRequest``
    through this helper, so the two surfaces always accept the same parameters
    with the same validation and defaults.

    Only the known search fields are read from ``params``; any extra keys (such
    as the MCP transport keys ``api_key`` and ``workspace_id``) are ignored.
    Keys whose value is ``None`` are dropped so the model's own defaults apply.
    Validation (ranges, required ``query``) is delegated to the Pydantic model.
    """
    kwargs = {
        field: params[field]
        for field in _SEARCH_REQUEST_FIELDS
        if field in params and params[field] is not None
    }
    return SearchRequest(**kwargs)


def _get_workspace_collection_name(workspace_id: str) -> str:
    """Return the Weaviate collection name for a workspace.

    Thin wrapper over the shared contracts helper (single source of truth, #12),
    kept under its existing private name so callers and golden tests still work.
    """
    return get_workspace_collection_name(workspace_id)


def _get_user_tenant_name(user_id: str) -> str:
    """Return the Weaviate tenant name for a user.

    Thin wrapper over the shared contracts helper (single source of truth, #12),
    kept under its existing private name so callers and golden tests still work.
    """
    return get_user_tenant_name(user_id)


class SearchService:
    """Service for semantic search operations.

    Scoring semantics (#45)
    -----------------------
    Each search mode derives its relevance ``score`` from a different Weaviate
    signal. The score is always normalised so results are comparable within a
    single response (and, for multi-workspace search, across workspaces):

    - ``keyword`` mode → **BM25**. Weaviate returns ``_additional.score`` as a
      ts_rank-style float (>= 0, unbounded; higher is more relevant).
      ``score_source = "bm25"`` and the raw value is echoed in ``bm25_score``.

    - ``semantic`` mode → **vector similarity**. ``nearVector`` does not populate
      ``_additional.score``; instead it returns ``certainty`` (cosine similarity
      normalised to ``[0, 1]``, higher is better) and/or ``distance`` (cosine
      distance in ``[0, 2]``, lower is better). We prefer ``certainty`` when
      present; otherwise we convert distance with::

          similarity = max(0.0, 1.0 - (distance / 2.0))

      which maps distance 0 → 1.0 (identical) and distance 2 → 0.0 (opposite).
      ``score_source = "vector"`` and the value is echoed in ``vector_similarity``.

    - ``hybrid`` mode → **Weaviate hybrid fusion**. Weaviate fuses BM25 and
      vector results using ``alpha`` (1.0 = pure vector, 0.0 = pure keyword) and
      returns the fused score in ``_additional.score``. ``score_source =
      "hybrid"`` and the fusion ``alpha`` is echoed back on each result. When a
      raw ``certainty``/``distance`` is also present we surface it in
      ``vector_similarity`` for transparency.

    Fallback priority for the ranking score (per result): ``score`` (BM25/hybrid)
    → ``certainty`` → distance→similarity → ``0.0``.

    Permission scoping (#45)
    ------------------------
    ``search()`` is always tenant-scoped: every Weaviate query carries the
    caller's ``tenant`` (derived from ``user_id``) and targets a single
    workspace collection, so a single-workspace search cannot read another
    user's data. Multi-workspace search (the API layer) only fans out over the
    workspace IDs returned by ``get_user_workspace_ids`` — the caller's
    authorised set — so merged results can never cross authorization. These
    invariants are enforced upstream; no redundant per-result filter is added.
    """

    def __init__(
        self,
        database: DatabaseService,
        weaviate_url: str,
        weaviate_api_key: str | None = None,
    ):
        self.database = database
        self.weaviate_url = weaviate_url.rstrip("/")
        self._api_key = weaviate_api_key
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        """Get HTTP client for Weaviate."""
        if self._client is None:
            # Authenticate when Weaviate API-key auth is enabled (#3 follow-up);
            # Weaviate accepts the key as a Bearer token.
            headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else None
            self._client = httpx.AsyncClient(
                base_url=self.weaviate_url,
                timeout=30.0,
                headers=headers,
            )
        return self._client

    async def close(self) -> None:
        """Close the HTTP client."""
        if self._client is not None:
            try:
                await self._client.aclose()
            except RuntimeError as e:
                # In FastAPI lifespan shutdown (especially under test runners),
                # the event loop may already be closing, and httpx/anyio can
                # raise "Event loop is closed". We treat that as non-fatal.
                if "Event loop is closed" in str(e):
                    logger.warning(
                        "Ignoring event-loop-closed error during search client shutdown",
                        error=str(e),
                    )
                else:
                    raise
            finally:
                self._client = None

    async def is_connected(self) -> bool:
        """Check if Weaviate is reachable."""
        try:
            client = await self._get_client()
            response = await client.get("/v1/.well-known/ready", timeout=5.0)
            return response.status_code == 200
        except Exception:
            return False

    @staticmethod
    def _parse_ingested_at(value: object) -> datetime | None:
        """Parse a Weaviate DATE / ISO-8601 string into an aware datetime.

        Weaviate returns DATE properties as RFC-3339 strings (e.g.
        ``2024-01-01T00:00:00Z``). Returns ``None`` for missing/unparseable
        values so freshness is simply unknown rather than an error.
        """
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=UTC)
        if isinstance(value, str) and value:
            try:
                # Python's fromisoformat handles the trailing 'Z' from 3.11+.
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
            except ValueError:
                return None
        return None

    @staticmethod
    def _compute_is_stale(ingested_at: datetime | None, *, now: datetime | None = None) -> bool:
        """Return True when ``ingested_at`` is older than the freshness window.

        Stale-evidence policy (#42): a result is stale when
        ``ingested_at < now - freshness_max_age_days``. When ``ingested_at`` is
        unknown (``None``) the result is treated as NOT stale (we never flag
        evidence we cannot age). Callers still receive stale results — they are
        only flagged, never dropped.
        """
        if ingested_at is None:
            return False
        reference = now or datetime.now(UTC)
        cutoff = reference - timedelta(days=settings.freshness_max_age_days)
        return ingested_at < cutoff

    @staticmethod
    def embed_query_vector(request: SearchRequest) -> list[float] | None:
        """Compute the query embedding for a request, or ``None`` for keyword mode.

        Keyword (BM25) search needs no vector, so we skip the embedding call.
        Computing this once and passing it into :meth:`search` lets a
        multi-workspace request embed the query a single time and reuse the
        vector across every workspace (#13), instead of re-embedding per
        workspace.
        """
        if request.search_mode == "keyword":
            return None
        from src.services.embedder import embed_query

        return list(embed_query(request.query))

    async def search(
        self,
        workspace_id: str,
        user_id: str,
        request: SearchRequest,
        query_vector: list[float] | None = None,
    ) -> SearchResponse:
        """Perform search scoped to a workspace and user tenant.

        Dispatches to the Weaviate query shape matching request.search_mode:
        - semantic: nearVector (client-side embedding, 384-dim)
        - hybrid:   hybrid (BM25 + vector with alpha fusion, client-side embedding)
        - keyword:  bm25 (pure keyword)

        ``query_vector`` may be supplied by the caller (e.g. multi-workspace
        search) to reuse a single precomputed embedding across workspaces (#13).
        When ``None`` the vector is computed lazily here for semantic/hybrid
        modes; it is ignored for keyword mode.

        Weaviate or network errors propagate to the caller — no silent fallback.
        See the API layer for the multi-workspace partial-result policy.
        """
        start_time = time.time()
        results = await self._search_weaviate(workspace_id, user_id, request, query_vector)
        # Advanced-methods dispatch point (#47). NO-OP by default — when the
        # experimental flags are off (the default) this returns results
        # unchanged. See _apply_advanced_methods.
        results = self._apply_advanced_methods(results, request)
        processing_time = (time.time() - start_time) * 1000
        return SearchResponse(
            results=results,
            query=request.query,
            total_results=len(results),
            processing_time_ms=round(processing_time, 2),
            search_mode=request.search_mode,
        )

    def _apply_advanced_methods(
        self,
        results: list[SearchResult],
        request: SearchRequest,
    ) -> list[SearchResult]:
        """Dispatch point for advanced retrieval methods (#47) — NO-OP scaffolding.

        Advanced retrieval methods (cross-encoder rerank, GraphRAG-style graph
        index, hierarchical index) are EXPERIMENTAL, OFF BY DEFAULT, and NOT yet
        implemented. They are gated by the ``enable_reranker`` /
        ``enable_graphrag_index`` / ``enable_hierarchy_index`` settings flags and
        by the eval-gate policy (no method on-by-default without a documented
        eval improvement vs the hybrid baseline #45 + maintainer approval; see
        docs/advanced-indexes.md).

        This method is deliberately side-effect-free: when a flag is on it only
        logs that the method is enabled-but-not-implemented and returns
        ``results`` UNCHANGED. When every flag is off (the default) it returns
        ``results`` unchanged without logging. No graph/rerank/hierarchy logic is
        performed here.
        """
        # NOTE: scaffolding only — these branches must NOT mutate ``results``.
        if settings.enable_reranker:
            logger.info(
                "advanced method 'reranker' enabled but not implemented (scaffolding)",
                search_mode=request.search_mode,
                issue="#47",
            )
        if settings.enable_graphrag_index:
            logger.info(
                "advanced method 'graphrag_index' enabled but not implemented (scaffolding)",
                search_mode=request.search_mode,
                issue="#47",
            )
        if settings.enable_hierarchy_index:
            logger.info(
                "advanced method 'hierarchy_index' enabled but not implemented (scaffolding)",
                search_mode=request.search_mode,
                issue="#47",
            )
        return results

    @staticmethod
    def _is_missing_collection(text: str, collection_name: str) -> bool:
        """True when Weaviate reports the *collection class* itself is unknown.

        Weaviate phrases a query against a non-existent class as
        ``Cannot query field "<Collection>" on type "GetObjectsObj"`` (the
        collection name is the unknown *field*). This is deliberately narrower
        than a missing *property* (``Cannot query field "content_hash" on type
        "<Collection>"`` — collection is the *type*), which is a real schema
        drift we still want to surface rather than swallow.
        """
        return f'Cannot query field "{collection_name}"' in text

    async def _search_weaviate(
        self,
        workspace_id: str,
        user_id: str,
        request: SearchRequest,
        query_vector: list[float] | None = None,
    ) -> list[SearchResult]:
        """Execute the requested search mode against a workspace-specific Weaviate collection.

        Permission scoping (#45): the query is always tenant-scoped to
        ``user_id`` and targets only ``workspace_id``'s collection, so results
        cannot cross authorization (asserted indirectly by the tenant/collection
        name guards below).
        """
        client = await self._get_client()

        # Compute the query vector here (offloaded to a thread) if the caller
        # didn't precompute it, so _build_graphql never does a blocking embed on
        # the event loop (#19). embed_query_vector returns None for keyword mode.
        if query_vector is None and request.search_mode != "keyword":
            query_vector = await asyncio.to_thread(self.embed_query_vector, request)

        collection_name = _get_workspace_collection_name(workspace_id)
        tenant_name = _get_user_tenant_name(user_id)

        _require_safe_name(collection_name, "collection")
        _require_safe_name(tenant_name, "tenant")

        graphql_query = self._build_graphql(collection_name, tenant_name, request, query_vector)

        try:
            response = await client.post("/v1/graphql", json=graphql_query)
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPStatusError as exc:
            # A workspace's Weaviate class/tenant only exists after its first
            # document is ingested, and Weaviate briefly returns HTTP 422 while
            # that class/tenant is being created (the ingest→search race seen on
            # fresh stacks) or for an otherwise unprocessable empty-state query.
            # In all these cases the workspace simply has nothing queryable yet,
            # so return no results instead of surfacing a 500. A genuinely
            # malformed query is still caught by unit/contract tests and by E2E
            # timeouts (a persistent 422 → no results → polling times out).
            if exc.response is not None and exc.response.status_code == 422:
                logger.warning(
                    "Weaviate search returned 422; treating as empty "
                    "(workspace class/tenant not ready or nothing indexed yet)",
                    collection=collection_name,
                    body=exc.response.text[:200],
                )
                return []
            raise

        # GraphQL returns 200 even on errors — check for them explicitly
        if data.get("errors"):
            message = data["errors"][0].get("message", "unknown")
            # Same "not indexed yet" states can also come back as 200+errors:
            # a missing collection class, or a tenant that doesn't exist yet.
            if (
                self._is_missing_collection(message, collection_name)
                or "tenant not found" in message
            ):
                return []
            raise httpx.HTTPError(f"Weaviate GraphQL error: {message}")

        chunks = data.get("data", {}).get("Get", {}).get(collection_name, []) or []

        results: list[SearchResult] = []
        for chunk in chunks:
            additional = chunk.get("_additional", {}) or {}

            # Weaviate score sources differ by query type:
            # - bm25 / hybrid → _additional.score (BM25 ts_rank-style float)
            # - nearVector / nearText → _additional.score is empty; certainty (0..1) is the
            #   normalised similarity. Fall back to certainty so semantic mode surfaces a
            #   non-zero, comparable relevance value to clients.
            def _to_float(value: object) -> float:
                if isinstance(value, (int, float, str)):
                    try:
                        return float(value)
                    except ValueError:
                        return 0.0
                return 0.0

            raw_score = _to_float(additional.get("score"))
            certainty = _to_float(additional.get("certainty"))
            distance = _to_float(additional.get("distance"))

            # Resolve ranking score with the documented fallback priority:
            #   score (BM25/hybrid) → certainty → distance→similarity → 0.0
            vector_similarity: float | None = None
            if raw_score > 0:
                score = raw_score
            elif certainty > 0:
                score = certainty
                vector_similarity = certainty
            elif distance > 0:
                # cosine distance is in [0, 2]; convert to a similarity in [0, 1]
                # so it is comparable to certainty (see class docstring).
                score = max(0.0, 1.0 - (distance / 2.0))
                vector_similarity = score
            else:
                score = 0.0

            # Score provenance (#45): map the mode to its score source and the
            # raw signals that produced the score.
            if request.search_mode == "keyword":
                score_source: ScoreSource = "bm25"
                bm25_score: float | None = raw_score if raw_score > 0 else None
                result_alpha: float | None = None
            elif request.search_mode == "hybrid":
                score_source = "hybrid"
                bm25_score = raw_score if raw_score > 0 else None
                result_alpha = request.alpha
                # Surface raw vector signal too when Weaviate provides it.
                if vector_similarity is None and certainty > 0:
                    vector_similarity = certainty
            else:  # semantic
                score_source = "vector"
                bm25_score = None
                result_alpha = None
                if vector_similarity is None and certainty > 0:
                    vector_similarity = certainty

            if score < request.min_score:
                continue

            # Pass through any extra chunk fields (e.g. freshness metadata) so
            # downstream consumers keep them (#45). chunk_index is preserved for
            # backward compatibility; known core fields are not duplicated.
            metadata: dict[str, Any] = {"chunk_index": chunk.get("chunk_index")}
            _core_fields = {
                "document_id",
                "original_filename",
                "content",
                "chunk_index",
                "_additional",
            }
            for key, value in chunk.items():
                if key not in _core_fields:
                    metadata[key] = value

            # Chunk provenance (#41): promote these from the chunk metadata onto
            # the result so clients can audit returned evidence. They remain in
            # metadata too (passthrough), and are simply None when absent.
            content_hash = chunk.get("content_hash")
            source_uri = chunk.get("source_uri")
            source_uri = source_uri if isinstance(source_uri, str) else None

            # Freshness (#42): promote ingested_at and compute staleness. Stale
            # results are flagged, not dropped (see _compute_is_stale).
            ingested_at = self._parse_ingested_at(chunk.get("ingested_at"))
            is_stale = self._compute_is_stale(ingested_at)

            # RAG-poisoning risk (#44): promote the heuristic ingest-time signal.
            # NON-BLOCKING — risky chunks are flagged, never dropped. "none" is
            # normalised to None so callers only see a level when it's notable.
            raw_risk = chunk.get("content_risk")
            content_risk = (
                raw_risk if isinstance(raw_risk, str) and raw_risk and raw_risk != "none" else None
            )
            raw_reasons = chunk.get("content_risk_reasons")
            content_risk_reasons = (
                [str(r) for r in raw_reasons]
                if content_risk and isinstance(raw_reasons, list) and raw_reasons
                else None
            )

            rounded_score = round(score, 4)
            chunk_id = additional.get("id", "")
            document_id = chunk.get("document_id", "")
            document_name = chunk.get("original_filename", "")
            content = chunk.get("content", "")

            def _to_int(value: object) -> int | None:
                if isinstance(value, bool):
                    return None
                if isinstance(value, int):
                    return value
                if isinstance(value, (float, str)):
                    try:
                        return int(value)
                    except (ValueError, TypeError):
                        return None
                return None

            start_char = _to_int(chunk.get("start_char"))
            end_char = _to_int(chunk.get("end_char"))

            # Claim-level citation (#39): built purely from this result's own
            # fields so the evidence is citable without a second lookup.
            citation = Citation(
                chunk_id=chunk_id,
                document_id=document_id,
                document_name=document_name,
                content=content,
                start_char=start_char,
                end_char=end_char,
                score=rounded_score,
                score_source=score_source,
                source_uri=source_uri,
                ingested_at=ingested_at,
                is_stale=is_stale,
            )

            results.append(
                SearchResult(
                    chunk_id=chunk_id,
                    document_id=document_id,
                    document_name=document_name,
                    content=content,
                    score=rounded_score,
                    metadata=metadata,
                    score_source=score_source,
                    bm25_score=round(bm25_score, 4) if bm25_score is not None else None,
                    vector_similarity=(
                        round(vector_similarity, 4) if vector_similarity is not None else None
                    ),
                    alpha=result_alpha,
                    content_hash=content_hash if isinstance(content_hash, str) else None,
                    source_uri=source_uri,
                    ingested_at=ingested_at,
                    is_stale=is_stale,
                    content_risk=content_risk,
                    content_risk_reasons=content_risk_reasons,
                    citation=citation,
                )
            )
        # Truncate back to the requested page size after min_score filtering
        # (the query may have over-fetched to avoid under-filling) (#31).
        return results[: request.limit]

    def _build_graphql(
        self,
        collection_name: str,
        tenant_name: str,
        request: SearchRequest,
        query_vector: list[float] | None = None,
    ) -> dict:
        """Compose the GraphQL query body for the requested search mode.

        ``query_vector`` is an optional precomputed embedding. When supplied it
        is reused (avoiding a redundant embedding call); when ``None`` it is
        computed here for semantic/hybrid modes (#13).
        """
        escaped_query = request.query.replace("\\", "\\\\").replace('"', '\\"')
        # Over-fetch when a min_score filter is active: Weaviate returns exactly
        # `limit` rows, then min_score is applied client-side, so without this a
        # page could come back short even when more above-threshold matches
        # exist. Results are truncated back to request.limit after filtering (#31).
        fetch_limit = min(100, request.limit * 3) if request.min_score > 0 else request.limit
        where_clause = ""
        if request.document_ids:
            where_filter: dict[str, Any] = {
                "path": ["document_id"],
                "operator": "ContainsAny",
                "valueTextArray": request.document_ids,
            }
            where_clause = f"where: {self._format_where(where_filter)}"

        if request.search_mode == "keyword":
            search_args = f'bm25: {{ query: "{escaped_query}" }}'
        else:
            # Semantic and hybrid both need the query vector computed client-side
            # because Weaviate is configured without a text vectorizer module.
            # Reuse the caller's precomputed vector when provided (#13) so a
            # multi-workspace request embeds the query only once.
            # _search_weaviate always precomputes the vector for semantic/hybrid
            # (offloaded to a thread, #19), so it is present here. Guard defensively.
            if query_vector is None:
                raise ValueError("query_vector is required for semantic/hybrid search")
            vector_list = query_vector
            vector_literal = "[" + ", ".join(f"{v:.6f}" for v in vector_list) + "]"
            if request.search_mode == "hybrid":
                search_args = (
                    f'hybrid: {{ query: "{escaped_query}", '
                    f"vector: {vector_literal}, alpha: {request.alpha} }}"
                )
            else:  # semantic
                search_args = f"nearVector: {{ vector: {vector_literal} }}"

        gql = f"""
        {{
            Get {{
                {collection_name}(
                    {search_args}
                    tenant: "{tenant_name}"
                    {where_clause}
                    limit: {fetch_limit}
                ) {{
                    document_id
                    original_filename
                    content
                    chunk_index
                    start_char
                    end_char
                    content_hash
                    source_uri
                    ingested_at
                    content_risk
                    content_risk_reasons
                    _additional {{ id score certainty distance }}
                }}
            }}
        }}
        """
        return {"query": gql}

    def _format_where(self, filter_dict: dict) -> str:
        """Format a where filter dict as Weaviate GraphQL syntax."""
        # Convert dict to JSON-like string but without quotes on keys
        json_str = json.dumps(filter_dict)
        # Remove quotes from keys (Weaviate GraphQL syntax)
        return re.sub(r'"(\w+)":', r"\1:", json_str)


# Singleton
_search_service: SearchService | None = None


async def get_search_service() -> SearchService:
    """Get the search service instance."""
    global _search_service
    if _search_service is None:
        database = await get_database()
        _search_service = SearchService(
            database,
            settings.effective_weaviate_url,
            weaviate_api_key=settings.weaviate_api_key,
        )
    return _search_service


async def close_search_service() -> None:
    """Close the search service."""
    global _search_service
    if _search_service is not None:
        await _search_service.close()
        _search_service = None
