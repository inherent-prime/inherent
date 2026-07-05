"""Offline retrieval-quality checks (#33/#35).

Validates the golden corpus is well-formed and that the metric harness wires up
correctly against canned rankings. No live stack required — these run in the
default ``-m 'not compose'`` suite. The full end-to-end ranking regression lives
in ``test_compose_retrieval_regression.py`` (compose-marked).
"""

from __future__ import annotations

import pytest

from src.services.ranking_metrics import mrr, ndcg_at_k, recall_at_k

pytestmark = pytest.mark.retrieval_eval


def test_corpus_is_well_formed(golden_corpus):
    """Every judgment references a real fixture, with sane fields."""
    judgments = golden_corpus["judgments"]
    sample_dir = golden_corpus["sample_dir"]
    assert judgments, "golden corpus is empty"

    for j in judgments:
        assert j.get("query_id"), f"missing query_id: {j}"
        assert isinstance(j.get("query"), str) and j["query"].strip(), f"bad query: {j}"
        doc = j.get("document_id")
        assert doc, f"missing document_id: {j}"
        assert (sample_dir / doc).exists(), f"qrel references missing fixture: {doc}"
        assert isinstance(j.get("chunk_index"), int) and j["chunk_index"] >= 0
        assert 0 <= int(j["relevance"]) <= 3, f"relevance out of range: {j}"


def test_corpus_covers_multiple_queries(golden_corpus):
    """The corpus must exercise several distinct queries with relevant docs."""
    queries = golden_corpus["queries"]
    relevant = golden_corpus["relevant"]
    assert len(queries) >= 4, "expected at least 4 distinct queries"
    # Every query should have at least one positively-judged document.
    for qid in queries:
        assert relevant.get(qid), f"query {qid} has no relevant documents"


def test_metric_harness_perfect_ranking(golden_corpus):
    """A ranking that puts the relevant docs first scores perfectly."""
    relevance = golden_corpus["relevance"]
    relevant = golden_corpus["relevant"]
    qid = next(iter(golden_corpus["queries"]))

    # Ideal ranking = relevant docs ordered by descending grade.
    ideal = sorted(relevance[qid], key=lambda d: relevance[qid][d], reverse=True)
    assert recall_at_k(ideal, relevant[qid], k=5) == pytest.approx(1.0)
    assert mrr(ideal, relevant[qid]) == pytest.approx(1.0)
    assert ndcg_at_k(ideal, relevance[qid], k=5) == pytest.approx(1.0)


def test_metric_harness_empty_ranking_scores_zero(golden_corpus):
    """No results → zero recall/MRR/nDCG (and never raises)."""
    relevance = golden_corpus["relevance"]
    relevant = golden_corpus["relevant"]
    qid = next(iter(golden_corpus["queries"]))
    assert recall_at_k([], relevant[qid], k=5) == 0.0
    assert mrr([], relevant[qid]) == 0.0
    assert ndcg_at_k([], relevance[qid], k=5) == 0.0
