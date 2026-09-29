"""Offline unit tests for the shared ranking metrics (inherent#391).

hand-computed expected values so the metrics are pinned exactly; no services
required. ``recall_at_k``/``mrr``/``ndcg_at_k`` were promoted unchanged from
``inh-public-api-svc``'s original module (see its own offline tests for their
full coverage) — this file focuses on the new ``hit_at_k`` and a light smoke
check that the re-exported functions still behave.
"""

from __future__ import annotations

from inh_contracts.ranking_metrics import hit_at_k, mrr, recall_at_k


def test_hit_at_k_true_when_any_relevant_in_top_k():
    assert hit_at_k(["a", "b", "c"], {"c", "z"}, 3) == 1.0


def test_hit_at_k_false_when_no_relevant_in_top_k():
    assert hit_at_k(["a", "b", "c"], {"z"}, 3) == 0.0


def test_hit_at_k_respects_cutoff():
    # "b" is relevant but ranked 2nd; k=1 must not see it.
    assert hit_at_k(["a", "b"], {"b"}, 1) == 0.0
    assert hit_at_k(["a", "b"], {"b"}, 2) == 1.0


def test_hit_at_k_differs_from_recall_at_k_with_multiple_relevant():
    ranked = ["a", "x", "y"]
    relevant = {"a", "b"}  # only "a" retrieved
    # hit@k: at least one relevant retrieved -> 1.0
    assert hit_at_k(ranked, relevant, 3) == 1.0
    # recall@k: fraction of the two relevant ids retrieved -> 0.5
    assert recall_at_k(ranked, relevant, 3) == 0.5


def test_hit_at_k_empty_relevant_set_is_zero():
    assert hit_at_k(["a", "b"], set(), 2) == 0.0


def test_hit_at_k_zero_or_negative_k_is_zero():
    assert hit_at_k(["a"], {"a"}, 0) == 0.0
    assert hit_at_k(["a"], {"a"}, -1) == 0.0


def test_hit_at_k_deduplicates_ranked_ids():
    # 4 duplicate "a"s occupy raw positions 1-4; deduped, "b" moves up to
    # cutoff position 2 instead of its raw position 5.
    assert hit_at_k(["a", "a", "a", "a", "b"], {"b"}, 1) == 0.0
    assert hit_at_k(["a", "a", "a", "a", "b"], {"b"}, 2) == 1.0


def test_mrr_still_works_from_shared_module():
    assert mrr(["x", "a"], {"a"}) == 0.5


# Edge cases for the metrics promoted from inh-public-api-svc: pinned here too so
# this package's own coverage gate holds on the code it now owns.


def test_recall_at_k_is_zero_without_relevant_ids_or_positive_k():
    assert recall_at_k(["a"], set(), 3) == 0.0
    assert recall_at_k(["a"], {"a"}, 0) == 0.0


def test_mrr_is_zero_without_relevant_ids_or_any_hit():
    assert mrr(["a", "b"], set()) == 0.0
    assert mrr(["a", "b"], {"z"}) == 0.0


def test_mrr_uses_rank_after_deduplication():
    # Duplicate "a" collapses, so "b" is rank 2, not 3.
    assert mrr(["a", "a", "b"], {"b"}) == 0.5


def test_ndcg_at_k_perfect_ranking_is_one():
    from inh_contracts.ranking_metrics import ndcg_at_k

    assert ndcg_at_k(["a", "b"], {"a": 2, "b": 1}, 2) == 1.0


def test_ndcg_at_k_penalises_a_worse_ordering():
    from inh_contracts.ranking_metrics import ndcg_at_k

    swapped = ndcg_at_k(["b", "a"], {"a": 2, "b": 1}, 2)
    assert 0.0 < swapped < 1.0


def test_ndcg_at_k_is_zero_for_non_positive_k_or_no_gain():
    from inh_contracts.ranking_metrics import ndcg_at_k

    assert ndcg_at_k(["a"], {"a": 1}, 0) == 0.0
    assert ndcg_at_k(["a"], {"a": 0}, 1) == 0.0
