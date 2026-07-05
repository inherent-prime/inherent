"""Pure ranking-quality metrics for retrieval evaluation.

These functions are intentionally dependency-free (stdlib only) and side-effect
free so they can be unit-tested fully offline. They operate on *ranked lists of
document ids* together with relevance information about which ids are relevant.

Conventions
-----------
- A "ranked list" is an ordered sequence of document ids, best match first.
  Duplicates are tolerated; only the first (best-ranked) occurrence of a given
  id contributes to a metric.
- ``k`` is a 1-based cutoff: ``k=5`` considers the top five ranked items.
- Relevance is non-negative. Binary metrics (recall, MRR) treat any id present
  in the relevant set as relevant; graded metrics (nDCG) use the integer grade.

All functions return ``float`` in ``[0.0, 1.0]`` and never raise on empty input.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence


def _dedupe_preserve_order(ranked: Iterable[str]) -> list[str]:
    """Return ``ranked`` with later duplicates removed, order preserved."""
    seen: set[str] = set()
    out: list[str] = []
    for doc_id in ranked:
        if doc_id not in seen:
            seen.add(doc_id)
            out.append(doc_id)
    return out


def recall_at_k(
    ranked_doc_ids: Sequence[str],
    relevant_doc_ids: Iterable[str],
    k: int,
) -> float:
    """Fraction of relevant documents retrieved within the top ``k`` results.

    Args:
        ranked_doc_ids: Document ids ordered best-match-first.
        relevant_doc_ids: The set of ids considered relevant for the query.
        k: 1-based cutoff; only the first ``k`` ranked ids are inspected.

    Returns:
        ``hits / |relevant|`` in ``[0.0, 1.0]``. Returns ``0.0`` when there are
        no relevant documents (nothing to recall) or ``k <= 0``.
    """
    relevant = set(relevant_doc_ids)
    if not relevant or k <= 0:
        return 0.0
    top_k = _dedupe_preserve_order(ranked_doc_ids)[:k]
    hits = sum(1 for doc_id in top_k if doc_id in relevant)
    return hits / len(relevant)


def mrr(
    ranked_doc_ids: Sequence[str],
    relevant_doc_ids: Iterable[str],
) -> float:
    """Reciprocal rank of the first relevant document.

    This is the per-query reciprocal rank; averaging it across queries yields the
    Mean Reciprocal Rank.

    Args:
        ranked_doc_ids: Document ids ordered best-match-first.
        relevant_doc_ids: The set of ids considered relevant for the query.

    Returns:
        ``1 / rank`` of the first relevant hit (rank is 1-based), or ``0.0`` if
        no relevant document appears in the ranking.
    """
    relevant = set(relevant_doc_ids)
    if not relevant:
        return 0.0
    for rank, doc_id in enumerate(_dedupe_preserve_order(ranked_doc_ids), start=1):
        if doc_id in relevant:
            return 1.0 / rank
    return 0.0


def _dcg(grades: Sequence[float]) -> float:
    """Discounted cumulative gain with gain ``2**rel - 1`` and ``log2(rank+1)``."""
    total = 0.0
    for rank, grade in enumerate(grades, start=1):
        if grade > 0:
            total += (2.0**grade - 1.0) / math.log2(rank + 1.0)
    return total


def ndcg_at_k(
    ranked: Sequence[str],
    relevance_by_doc_id: Mapping[str, float],
    k: int,
) -> float:
    """Normalized Discounted Cumulative Gain at cutoff ``k``.

    Uses the standard exponential-gain formulation: each position contributes
    ``(2**rel - 1) / log2(rank + 1)`` and the result is normalized by the ideal
    DCG (the same documents sorted by descending relevance).

    Args:
        ranked: Document ids ordered best-match-first.
        relevance_by_doc_id: Mapping from document id to its integer relevance
            grade (0 = irrelevant). Ids missing from the mapping count as 0.
        k: 1-based cutoff.

    Returns:
        ``DCG@k / IDCG@k`` in ``[0.0, 1.0]``. Returns ``0.0`` when ``k <= 0`` or
        when there is no positive relevance to gain (ideal DCG is zero).
    """
    if k <= 0:
        return 0.0
    top_k = _dedupe_preserve_order(ranked)[:k]
    grades = [float(relevance_by_doc_id.get(doc_id, 0.0)) for doc_id in top_k]
    dcg = _dcg(grades)

    ideal_grades = sorted(
        (g for g in relevance_by_doc_id.values() if g > 0),
        reverse=True,
    )[:k]
    idcg = _dcg(ideal_grades)
    if idcg == 0.0:
        return 0.0
    return dcg / idcg
