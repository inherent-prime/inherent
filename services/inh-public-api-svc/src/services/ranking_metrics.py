"""Pure ranking-quality metrics for retrieval evaluation.

Moved to ``inh_contracts.ranking_metrics`` (inherent#391) so ``inh-cli``'s
dataset-driven eval harness can call the exact same implementation over a
dependency both packages already carry, instead of reimplementing hit@k/MRR
against a private service package it cannot import. This module is kept as a
thin re-export so every existing import of
``src.services.ranking_metrics`` (the eval runner, existing tests) keeps
working unchanged -- see ``inh_contracts.ranking_metrics`` for the actual
implementation and docs.
"""

from __future__ import annotations

from inh_contracts.ranking_metrics import hit_at_k, mrr, ndcg_at_k, recall_at_k

__all__ = ["hit_at_k", "mrr", "ndcg_at_k", "recall_at_k"]
