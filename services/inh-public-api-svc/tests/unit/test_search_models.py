"""Pydantic model validation tests (PM-S018 + PM-S019)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.models.search import ContextChunk, SearchRequest, SearchResponse, SearchResult


class TestSearchRequestNewFields:
    def test_defaults(self) -> None:
        req = SearchRequest(query="hello")
        assert req.include_context is False
        assert req.context_window == 2
        assert req.search_mode == "semantic"
        # alpha defaults to None (inherent#391), not a fixed 0.7 -- so
        # SearchService.search can tell "omitted" apart from "explicitly
        # 0.7" and fall back to a per-workspace configured default. See
        # SearchService.search's alpha resolution / DEFAULT_HYBRID_ALPHA
        # for where the 0.7 fallback now actually lives.
        assert req.alpha is None

    def test_context_window_out_of_range_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SearchRequest(query="x", context_window=-1)
        with pytest.raises(ValidationError):
            SearchRequest(query="x", context_window=6)

    def test_alpha_out_of_range_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SearchRequest(query="x", alpha=-0.1)
        with pytest.raises(ValidationError):
            SearchRequest(query="x", alpha=1.1)

    def test_invalid_search_mode_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SearchRequest(query="x", search_mode="fuzzy")  # type: ignore[arg-type]


class TestSearchResultContextFields:
    def test_defaults_to_none(self) -> None:
        r = SearchResult(
            chunk_id="c1",
            document_id="d1",
            document_name="x.md",
            content="text",
            score=0.5,
        )
        assert r.context_before is None
        assert r.context_after is None

    def test_accepts_context_chunks(self) -> None:
        ctx = ContextChunk(chunk_id="c0", chunk_index=0, content="hi", token_count=10)
        r = SearchResult(
            chunk_id="c1",
            document_id="d1",
            document_name="x.md",
            content="text",
            score=0.5,
            context_before=[ctx],
        )
        assert r.context_before == [ctx]


class TestSearchResponseNewFields:
    def test_total_tokens_defaults_zero(self) -> None:
        resp = SearchResponse(
            results=[],
            query="q",
            total_results=0,
            processing_time_ms=1.0,
            search_mode="semantic",
        )
        assert resp.total_tokens == 0

    def test_search_mode_required(self) -> None:
        with pytest.raises(ValidationError):
            SearchResponse(results=[], query="q", total_results=0, processing_time_ms=1.0)  # type: ignore[call-arg]
