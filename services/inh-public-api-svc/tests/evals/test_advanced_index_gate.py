"""Eval-gate scaffolding tests for advanced retrieval methods (#47).

OFFLINE. These assert the *governance* contract around the experimental
advanced retrieval methods (cross-encoder rerank, GraphRAG-style graph index,
hierarchical index), NOT any real retrieval behaviour (none is implemented):

1. the three feature flags exist and default to ``False`` (off by default);
2. with the flags off (the default), the no-op dispatch point
   ``SearchService._apply_advanced_methods`` returns results UNCHANGED;
3. the eval-gate policy is documented and enforced by the defaults — no advanced
   method is enabled by default, so none can ship on without clearing the gate
   (documented eval improvement vs the hybrid baseline #45 + maintainer
   approval; see docs/advanced-indexes.md).

No live stack required — runs in the default ``-m 'not compose'`` suite.
"""

from __future__ import annotations

import pytest

from src.config.settings import Settings
from src.models.search import SearchRequest, SearchResult
from src.services.search import SearchService

pytestmark = pytest.mark.retrieval_eval


# The three advanced-method flags gated by the eval policy (#47).
ADVANCED_FLAGS = (
    "enable_reranker",
    "enable_graphrag_index",
    "enable_hierarchy_index",
)


def _make_service() -> SearchService:
    """A SearchService that never touches the network.

    The constructor only stores its arguments; ``_apply_advanced_methods`` is a
    pure, synchronous no-op, so a dummy database and URL are sufficient offline.
    """
    return SearchService(database=None, weaviate_url="http://localhost:8080")  # type: ignore[arg-type]


def _sample_results() -> list[SearchResult]:
    return [
        SearchResult(
            chunk_id="c1",
            document_id="d1",
            document_name="doc.txt",
            content="hello world",
            score=0.9,
        ),
        SearchResult(
            chunk_id="c2",
            document_id="d1",
            document_name="doc.txt",
            content="second chunk",
            score=0.5,
        ),
    ]


def test_advanced_flags_exist() -> None:
    """All three advanced-method flags are defined on Settings (#47)."""
    fields = Settings.model_fields
    for flag in ADVANCED_FLAGS:
        assert flag in fields, f"missing advanced-method flag: {flag}"


@pytest.mark.parametrize("flag", ADVANCED_FLAGS)
def test_advanced_flags_default_off(flag: str) -> None:
    """Eval-gate policy (#47): every advanced method is OFF by default.

    No method may ship enabled-by-default without a documented eval improvement
    vs the hybrid baseline (#45) + maintainer approval (docs/advanced-indexes.md).
    Asserting the defaults here makes the gate impossible to defeat silently.
    """
    settings = Settings()  # type: ignore[call-arg]
    assert getattr(settings, flag) is False, f"{flag} must default to False (off by default)"


def test_apply_advanced_methods_is_noop_when_flags_off() -> None:
    """With flags off (default), the dispatch point returns results unchanged."""
    service = _make_service()
    results = _sample_results()
    request = SearchRequest(query="hello")

    out = service._apply_advanced_methods(results, request)

    # Same object, same contents — pure no-op, no reranking/reordering/filtering.
    assert out is results
    assert [r.chunk_id for r in out] == ["c1", "c2"]
    assert [r.score for r in out] == [0.9, 0.5]


def test_no_advanced_method_enabled_by_default() -> None:
    """Gate policy, restated as a single guard: defaults must keep ALL off.

    This documents the policy that no advanced retrieval method (rerank / graph /
    hierarchy) is enabled by default. Flipping any default to True here is a
    deliberate, gated change requiring an eval improvement vs the hybrid baseline
    (#45) and maintainer approval.
    """
    settings = Settings()  # type: ignore[call-arg]
    enabled = [flag for flag in ADVANCED_FLAGS if getattr(settings, flag)]
    assert enabled == [], f"advanced methods enabled by default (gate violated): {enabled}"


def test_diversification_defaults_on() -> None:
    """Eval-gate policy (#146): per-document diversification is ON by default.

    Unlike the three ADVANCED_FLAGS above, diversification IS implemented (see
    ADR 0004 and SearchService._diversify_by_document) -- it was gated for a
    different reason: it changes ranking order for every multi-chunk-per-
    document query, not just crowded ones, so it needed the same documented
    eval improvement + maintainer approval before defaulting on, same as any
    #47 method. Both conditions are now met -- see
    docs/adr/0004-per-document-diversification.md for the measured evidence
    (recall@5 0.5->1.0 on the multi_doc_crowding golden corpus category) and
    its 2026-08-06 amendment recording maintainer approval and the default
    flip. An operator who wants the pre-flip behavior sets
    ENABLE_DIVERSIFICATION=false.
    """
    settings = Settings()  # type: ignore[call-arg]
    assert (
        settings.enable_diversification is True
    ), "enable_diversification must default to True (on by default, #146)"
