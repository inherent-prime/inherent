"""Search-related models."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from src.models.citation import Citation

ScoreSource = Literal["bm25", "vector", "hybrid"]


class ContextChunk(BaseModel):
    """A neighbouring chunk supplied as context for a search result."""

    chunk_id: str
    chunk_index: int
    content: str
    token_count: int = 0


class SearchRequest(BaseModel):
    """Request model for semantic search."""

    query: str = Field(..., min_length=1, max_length=1000, description="Search query")
    limit: int = Field(default=10, ge=1, le=100, description="Maximum results to return")
    min_score: float = Field(default=0.0, ge=0.0, le=1.0, description="Minimum similarity score")
    document_ids: list[str] | None = Field(
        default=None, description="Filter to specific document IDs"
    )

    # Context window (PM-S019)
    include_context: bool = Field(
        default=False,
        description="If true, each result includes surrounding chunks in context_before/after",
    )
    context_window: int = Field(
        default=2,
        ge=0,
        le=5,
        description="Number of chunks before AND after each match (ignored when include_context=false)",
    )

    # Search mode (PM-S018)
    #
    # These are the STANDARD, production retrieval modes (semantic / hybrid /
    # keyword). Advanced retrieval methods (cross-encoder rerank, GraphRAG-style
    # graph index, hierarchical index) are NOT modes here: they are EXPERIMENTAL,
    # OFF BY DEFAULT, and gated by the enable_reranker / enable_graphrag_index /
    # enable_hierarchy_index settings flags + the eval-gate policy (#47; see
    # docs/advanced-indexes.md). Do NOT add them to this Literal until a method
    # is implemented and clears the eval gate.
    search_mode: Literal["semantic", "hybrid", "keyword"] = Field(
        default="semantic",
        description="Retrieval strategy: semantic (nearText), hybrid (BM25+vector), or keyword (BM25)",
    )
    alpha: float = Field(
        default=0.7,
        ge=0.0,
        le=1.0,
        description="Hybrid fusion weight (1.0=vector-heavy, 0.0=keyword-heavy); ignored unless search_mode=hybrid",
    )


class SearchResult(BaseModel):
    """A single search result.

    Score provenance (#45)
    ----------------------
    ``score`` is the normalised relevance value used for ranking. The optional
    provenance fields explain *where* that score came from so clients can build
    a measurable retrieval baseline:

    - ``score_source``      — which signal produced ``score``:
                              ``"bm25"`` (keyword), ``"vector"`` (semantic), or
                              ``"hybrid"`` (Weaviate fusion).
    - ``bm25_score``        — raw BM25 score when available (keyword/hybrid).
    - ``vector_similarity`` — raw vector similarity (certainty, or the
                              distance→similarity conversion) for semantic mode.
    - ``alpha``             — the hybrid fusion weight echoed back for hybrid mode.

    All provenance fields are optional and backward-compatible: existing clients
    that ignore them are unaffected.
    """

    chunk_id: str
    document_id: str
    document_name: str
    content: str
    score: float
    metadata: dict | None = None
    context_before: list[ContextChunk] | None = None
    context_after: list[ContextChunk] | None = None

    # Score provenance (#45) — optional, backward-compatible.
    score_source: ScoreSource | None = None
    bm25_score: float | None = None
    vector_similarity: float | None = None
    alpha: float | None = None

    # Chunk provenance (#41) — optional, backward-compatible. Promoted from the
    # chunk's metadata passthrough so returned evidence is auditable:
    #   content_hash — sha256 hex digest of the chunk's content
    #   source_uri   — where the chunk's source bytes live
    content_hash: str | None = None
    source_uri: str | None = None

    # Freshness (#42) — optional, backward-compatible. Promoted from the chunk
    # so callers can age returned evidence:
    #   ingested_at — when the chunk was (re)ingested (None if unknown)
    #   is_stale    — True when ingested_at < now - freshness_max_age_days.
    # Stale-evidence policy: stale results are NOT dropped; they are returned
    # with is_stale=True so the caller decides how to treat aged sources (and
    # can hit POST /v1/documents/{id}/refresh to re-ingest).
    ingested_at: datetime | None = None
    is_stale: bool = False

    # RAG-poisoning / prompt-injection risk (#44) — optional, backward-compatible.
    # Promoted from the chunk so callers can see (and down-weight) evidence that
    # was tagged at ingest time. This is a NON-BLOCKING signal: risky chunks are
    # still returned, only flagged.
    #   content_risk         — "none" | "low" | "medium" | "high" (None if unknown)
    #   content_risk_reasons — matched heuristic reason codes (None if unknown)
    content_risk: str | None = None
    content_risk_reasons: list[str] | None = None

    # Claim-level citation (#39) — optional, backward-compatible. Built from this
    # result's own fields (chunk_id + spans + score + provenance + freshness) so
    # the evidence is citable without a second lookup.
    citation: "Citation | None" = None


class QualityVerdict(BaseModel):
    """Adaptive retrieval quality gate verdict (#43).

    Computed from existing retrieval signals (result count, top score) after a
    search, so the API can decide whether the evidence is good enough or whether
    to attempt a single bounded fallback.

    - ``verdict``     — overall judgement:
        * ``"sufficient"``           — evidence looks good enough to answer.
        * ``"insufficient_evidence"`` — too little/no evidence to answer well.
        * ``"low_confidence"``       — there is evidence but it ranks weakly.
    - ``reason_code`` — machine-readable reason (e.g. ``"no_results"``,
                        ``"top_score_below_threshold"``, ``"low_result_count"``,
                        ``"ok"``).
    - ``confidence``  — coarse [0, 1] confidence proxy derived from the top score.
    """

    verdict: Literal["sufficient", "insufficient_evidence", "low_confidence"]
    reason_code: str
    confidence: float = Field(ge=0.0, le=1.0)


class SearchResponse(BaseModel):
    """Response model for search endpoint."""

    results: list[SearchResult]
    query: str
    total_results: int
    processing_time_ms: float
    search_mode: Literal["semantic", "hybrid", "keyword"]
    total_tokens: int = 0

    # Adaptive retrieval quality gate (#43) — optional, backward-compatible.
    #   quality_verdict   — the gate's judgement on the (possibly post-fallback)
    #                       results; None when not evaluated.
    #   performed_fallback — True when exactly one bounded fallback retry ran.
    #   fallback_strategy  — which fallback ran (e.g. "keyword_retry",
    #                        "broadened_query"); None when no fallback ran.
    quality_verdict: QualityVerdict | None = None
    performed_fallback: bool = False
    fallback_strategy: str | None = None

    # Evals v1 — optional, backward-compatible. The capture event id for this
    # search; the handle for POST /v1/evals/feedback and the join key for
    # external (answer-level) eval stacks. None when capture is disabled or
    # the search was multi-workspace.
    event_id: str | None = None


# Resolve the forward reference to Citation (#39). Imported here (not at module
# top) to avoid a circular import: citation.py imports ScoreSource from this
# module. The rebuild lets ``SearchResult.citation`` validate the real type.
from src.models.citation import Citation  # noqa: E402

SearchResult.model_rebuild()
