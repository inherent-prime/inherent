"""The ranking metrics were promoted from tests/evals/metrics.py to src so the
eval runner (production code) can use them. This guards the promoted location
and spot-checks behavior parity."""

from src.services.ranking_metrics import mrr, ndcg_at_k, recall_at_k


def test_recall_at_k_promoted():
    assert recall_at_k(["a", "b", "c"], {"a", "b"}, 3) == 1.0
    assert recall_at_k(["a", "x", "d"], {"a", "d"}, 2) == 0.5


def test_mrr_promoted():
    assert mrr(["x", "a"], {"a"}) == 0.5


def test_ndcg_at_k_promoted():
    assert ndcg_at_k(["a"], {"a": 1.0}, 1) == 1.0
