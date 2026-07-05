"""Offline unit tests for the pure ranking metrics in ``metrics.py``.

These use hand-computed expected values so the metrics are pinned exactly. They
require no services and must pass in the default ``-m 'not compose'`` run.
"""

from __future__ import annotations

import math

import pytest

from src.services.ranking_metrics import mrr, ndcg_at_k, recall_at_k

pytestmark = pytest.mark.retrieval_eval


# ---------------------------------------------------------------------------
# recall_at_k
# ---------------------------------------------------------------------------


def test_recall_perfect():
    # both relevant ids are in the top 3 -> 2/2.
    assert recall_at_k(["a", "b", "c"], {"a", "b"}, 3) == 1.0


def test_recall_partial():
    # only "a" is in the top 2 of {a, d} relevant -> 1/2.
    assert recall_at_k(["a", "x", "d"], {"a", "d"}, 2) == 0.5


def test_recall_cutoff_excludes_late_hit():
    # relevant id "c" sits at rank 3 but k=2 -> 0 hits / 1 relevant.
    assert recall_at_k(["a", "b", "c"], {"c"}, 2) == 0.0


def test_recall_no_relevant_returns_zero():
    assert recall_at_k(["a", "b"], set(), 5) == 0.0


def test_recall_empty_ranking():
    assert recall_at_k([], {"a"}, 5) == 0.0


def test_recall_k_zero():
    assert recall_at_k(["a"], {"a"}, 0) == 0.0


def test_recall_dedupes_ranking():
    # duplicate "a" should not count twice; relevant {a, b}, top 3 after dedupe
    # is [a, b, c] -> 2/2.
    assert recall_at_k(["a", "a", "b", "c"], {"a", "b"}, 3) == 1.0


# ---------------------------------------------------------------------------
# mrr
# ---------------------------------------------------------------------------


def test_mrr_first_position():
    assert mrr(["a", "b", "c"], {"a"}) == 1.0


def test_mrr_second_position():
    assert mrr(["x", "a", "b"], {"a"}) == 0.5


def test_mrr_third_position():
    assert mrr(["x", "y", "a"], {"a"}) == pytest.approx(1.0 / 3.0)


def test_mrr_uses_first_relevant():
    # both "b" (rank 2) and "c" (rank 3) relevant -> reciprocal of first = 1/2.
    assert mrr(["a", "b", "c"], {"b", "c"}) == 0.5


def test_mrr_no_hit():
    assert mrr(["a", "b"], {"z"}) == 0.0


def test_mrr_no_relevant():
    assert mrr(["a", "b"], set()) == 0.0


def test_mrr_dedupe_does_not_shift_rank():
    # duplicate leading "x" collapses; first relevant "a" is at rank 2.
    assert mrr(["x", "x", "a"], {"a"}) == 0.5


# ---------------------------------------------------------------------------
# ndcg_at_k
# ---------------------------------------------------------------------------


def test_ndcg_perfect_order_is_one():
    rels = {"a": 3, "b": 2, "c": 1}
    assert ndcg_at_k(["a", "b", "c"], rels, 3) == pytest.approx(1.0)


def test_ndcg_no_relevant_returns_zero():
    assert ndcg_at_k(["a", "b"], {"a": 0, "b": 0}, 5) == 0.0


def test_ndcg_k_zero():
    assert ndcg_at_k(["a"], {"a": 3}, 0) == 0.0


def test_ndcg_missing_ids_count_as_zero():
    # "x" not in relevance map -> grade 0; "a" (grade 3) at rank 2.
    # DCG = (2^3 - 1)/log2(3) = 7/1.5849625 = 4.41633.
    # IDCG (ideal: a at rank 1) = (2^3 - 1)/log2(2) = 7.0.
    expected = (7.0 / math.log2(3)) / 7.0
    assert ndcg_at_k(["x", "a"], {"a": 3}, 5) == pytest.approx(expected)


def test_ndcg_known_value_reordered():
    # ranking [b, a]; grades a=3, b=1.
    # DCG = (2^1-1)/log2(2) + (2^3-1)/log2(3) = 1.0 + 7/1.5849625 = 5.41633.
    # IDCG (ideal [a, b]) = 7/log2(2) + 1/log2(3) = 7.0 + 0.63093 = 7.63093.
    rels = {"a": 3, "b": 1}
    dcg = (2**1 - 1) / math.log2(2) + (2**3 - 1) / math.log2(3)
    idcg = (2**3 - 1) / math.log2(2) + (2**1 - 1) / math.log2(3)
    assert ndcg_at_k(["b", "a"], rels, 2) == pytest.approx(dcg / idcg)


def test_ndcg_cutoff_truncates():
    # k=1 only counts rank 1. ranking [b, a], grades a=3, b=1.
    # DCG@1 = (2^1-1)/log2(2) = 1.0. IDCG@1 = (2^3-1)/log2(2) = 7.0.
    rels = {"a": 3, "b": 1}
    assert ndcg_at_k(["b", "a"], rels, 1) == pytest.approx(1.0 / 7.0)


def test_ndcg_in_unit_range():
    rels = {"a": 2, "b": 3, "c": 1, "d": 0}
    val = ndcg_at_k(["c", "a", "d", "b"], rels, 4)
    assert 0.0 <= val <= 1.0
