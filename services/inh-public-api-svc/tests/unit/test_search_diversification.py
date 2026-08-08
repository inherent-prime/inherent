"""Unit tests for per-document result diversification (#146).

TDD: written before SearchService._diversify_by_document existed. Exercises
the round-robin diversification logic directly (pure function, no Weaviate/
httpx needed) plus the settings flag's default-on gating in
SearchService.search via _search_weaviate's post-filter step.

The default flipped to True in #146's follow-up once both eval-gate
conditions were met (documented eval improvement + maintainer approval,
2026-08-06 -- see ADR 0004's amendment and settings.py). An operator can
still set ENABLE_DIVERSIFICATION=false to restore the pre-#146 behavior;
that off path stays covered below (TestFetchLimitComposition and the
explicit off case in TestDiversificationSettingsGate).
"""

from __future__ import annotations

import re

import pytest

from src.config import settings
from src.models.search import SearchRequest, SearchResult
from src.services.search import SearchService


def _result(chunk_id: str, document_id: str, score: float) -> SearchResult:
    return SearchResult(
        chunk_id=chunk_id,
        document_id=document_id,
        document_name=f"{document_id}.txt",
        content=f"content for {chunk_id}",
        score=score,
    )


class TestDiversifyByDocument:
    def test_returns_input_unchanged_when_under_limit(self) -> None:
        results = [_result("c1", "docA", 0.9), _result("c2", "docA", 0.8)]
        out = SearchService._diversify_by_document(results, limit=5)
        assert out == results

    def test_multi_document_under_limit_preserves_score_order(self) -> None:
        # Nothing gets truncated here (3 results, limit 10), so nothing needs
        # crowding correction -- round-robin must not reorder a page that was
        # never going to drop a result (#146 cross-review).
        results = [
            _result("a0", "docA", 0.9),
            _result("a1", "docA", 0.8),
            _result("b0", "docB", 0.7),
        ]
        out = SearchService._diversify_by_document(results, limit=10)
        assert out == results

    def test_empty_input_returns_empty(self) -> None:
        assert SearchService._diversify_by_document([], limit=5) == []

    def test_limit_zero_returns_empty(self) -> None:
        results = [_result("c1", "docA", 0.9)]
        assert SearchService._diversify_by_document(results, limit=0) == []

    def test_round_robins_across_documents_before_exhausting_one(self) -> None:
        # docA has 10 highly-relevant chunks; docB and docC have 1 each, all
        # individually less relevant than every docA chunk. A naive score-sorted
        # truncate to 3 would return only docA -- asserted directly below as the
        # contrast baseline, not just described in a comment -- while
        # diversification must surface at least docB and docC in the first
        # `limit` results instead.
        doc_a = [_result(f"a{i}", "docA", 0.9 - i * 0.01) for i in range(10)]
        doc_b = [_result("b0", "docB", 0.5)]
        doc_c = [_result("c0", "docC", 0.4)]
        candidates = doc_a + doc_b + doc_c  # already score-sorted, as Weaviate returns

        naive = candidates[:3]
        assert {r.document_id for r in naive} == {
            "docA"
        }, "contrast baseline: naive truncate should crowd out docB/docC"

        out = SearchService._diversify_by_document(candidates, limit=3)

        assert len(out) == 3
        assert {r.document_id for r in out} == {"docA", "docB", "docC"}
        # Highest-scoring docA chunk still wins its round-robin slot first.
        assert out[0].chunk_id == "a0"

    def test_preserves_within_document_relevance_order(self) -> None:
        doc_a = [_result("a0", "docA", 0.9), _result("a1", "docA", 0.8)]
        doc_b = [_result("b0", "docB", 0.85), _result("b1", "docB", 0.75)]
        candidates = [doc_a[0], doc_b[0], doc_a[1], doc_b[1]]  # interleaved input order

        out = SearchService._diversify_by_document(candidates, limit=4)

        a_positions = [r.chunk_id for r in out if r.document_id == "docA"]
        b_positions = [r.chunk_id for r in out if r.document_id == "docB"]
        assert a_positions == ["a0", "a1"]
        assert b_positions == ["b0", "b1"]

    def test_exhausted_document_is_skipped_in_later_rounds(self) -> None:
        doc_a = [_result("a0", "docA", 0.9)]  # only 1 candidate
        doc_b = [_result(f"b{i}", "docB", 0.8 - i * 0.01) for i in range(3)]
        candidates = doc_a + doc_b

        out = SearchService._diversify_by_document(candidates, limit=4)

        assert len(out) == 4
        assert [r.chunk_id for r in out] == ["a0", "b0", "b1", "b2"]

    def test_fewer_candidates_than_limit_returns_all(self) -> None:
        candidates = [_result("a0", "docA", 0.9), _result("b0", "docB", 0.8)]
        out = SearchService._diversify_by_document(candidates, limit=10)
        assert len(out) == 2
        assert {r.document_id for r in out} == {"docA", "docB"}

    def test_single_document_workspace_still_fills_the_page(self) -> None:
        """Adversarial case (#146 review, now on by default): a workspace
        where every candidate belongs to one document must not return FEWER
        than `limit` results just because there is nothing to diversify
        against -- the single bucket round-robins with itself, which is
        equivalent to a plain score-sorted truncate (documented as a
        boundary in ADR 0004: "Not a fix for single-document corpora").
        A regression here would silently under-fill every single-document
        workspace's search results once diversification is the default.
        """
        candidates = [_result(f"a{i}", "docA", 0.9 - i * 0.01) for i in range(20)]
        out = SearchService._diversify_by_document(candidates, limit=5)
        assert len(out) == 5
        assert {r.document_id for r in out} == {"docA"}
        # Still the top-5 by score, in score order -- self-round-robin changes
        # nothing about which candidates win or their relative order.
        assert [r.chunk_id for r in out] == ["a0", "a1", "a2", "a3", "a4"]


class TestDiversificationSettingsGate:
    def test_enabled_by_default(self) -> None:
        """Eval-gate policy cleared (#146): both conditions were met --
        documented eval improvement (recall@5 0.5->1.0 on multi_doc_crowding,
        ADR 0004) and maintainer approval (ADR 0004 amendment, 2026-08-06) --
        so diversification now ships on by default.
        """
        from src.config.settings import get_settings

        assert get_settings().enable_diversification is True

    def test_operator_can_still_disable_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An operator who wants the pre-#146 behavior sets
        ENABLE_DIVERSIFICATION=false; Settings must still honor that override
        (this is a real operator escape hatch, not just an internal flag).
        """
        from src.config.settings import Settings

        monkeypatch.setenv("ENABLE_DIVERSIFICATION", "false")
        assert Settings().enable_diversification is False  # type: ignore[call-arg]


class TestFetchLimitComposition:
    """_build_graphql must actually widen the Weaviate fetch when
    enable_diversification is on -- the round-robin post-filter
    (_diversify_by_document) has nothing to diversify across if the query
    itself only ever asked Weaviate for `limit` rows. A regression that
    enables the post-filter without touching _build_graphql's fetch_limit
    would leave TestDiversifyByDocument's unit tests green while production
    crowding remained unfixed, since that class only tests the pure function
    directly -- this class tests the composition point instead (#146 review).
    """

    def _make_service(self) -> SearchService:
        return SearchService(database=None, weaviate_url="http://localhost:8080")  # type: ignore[arg-type]

    def _fetch_limit(self, request: SearchRequest) -> int:
        """Extract the `limit: N` Weaviate over-fetches for, from the built query."""
        gql = self._make_service()._build_graphql(  # noqa: SLF001
            collection_name="Workspace_ws1",
            tenant_name="ws1",
            request=request,
        )
        match = re.search(r"limit:\s*(\d+)", gql["query"])
        assert match, f"no `limit: N` found in built query: {gql['query']}"
        return int(match.group(1))

    def test_diversification_off_fetches_exactly_the_page_size(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "enable_diversification", False)
        request = SearchRequest(query="hello", limit=5, search_mode="keyword")
        assert self._fetch_limit(request) == 5

    def test_diversification_on_widens_fetch_by_the_multiplier(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "enable_diversification", True)
        monkeypatch.setattr(settings, "diversification_over_fetch_multiplier", 5)
        request = SearchRequest(query="hello", limit=5, search_mode="keyword")
        assert self._fetch_limit(request) == 25

    def test_diversification_fetch_is_capped_at_100(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "enable_diversification", True)
        monkeypatch.setattr(settings, "diversification_over_fetch_multiplier", 5)
        request = SearchRequest(query="hello", limit=100, search_mode="keyword")
        assert self._fetch_limit(request) == 100

    def test_diversification_composes_with_min_score_over_fetch_via_max(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """min_score's own 3x over-fetch and diversification's multiplier both
        apply; the wider of the two must win (max(), not one overriding the
        other), since both filters need enough candidates to work with.
        """
        monkeypatch.setattr(settings, "enable_diversification", True)
        # multiplier (2) * limit (5) = 10, smaller than min_score's 3x = 15 --
        # min_score's branch must win here.
        monkeypatch.setattr(settings, "diversification_over_fetch_multiplier", 2)
        request = SearchRequest(query="hello", limit=5, search_mode="keyword", min_score=0.5)
        assert self._fetch_limit(request) == 15

        # multiplier (10) * limit (5) = 50, larger than min_score's 3x = 15 --
        # diversification's branch must win here.
        monkeypatch.setattr(settings, "diversification_over_fetch_multiplier", 10)
        assert self._fetch_limit(request) == 50
