"""MCP multi-workspace search must be deterministic (#28).

Results were sorted by score only, over a workspace list derived from a set
(nondeterministic order), so equal-scored results at the top-k cutoff could
order differently between identical requests. A stable tiebreaker fixes it,
matching the REST path's (-score, chunk_id, document_id) key.
"""

from __future__ import annotations

from types import SimpleNamespace

from src.mcp_server.server import _search_rank_key


def _pair(ws, score, chunk_id, document_id="d"):
    return (ws, SimpleNamespace(score=score, chunk_id=chunk_id, document_id=document_id))


def test_orders_by_score_descending():
    high = _pair("w", 0.9, "c1")
    low = _pair("w", 0.1, "c0")
    assert sorted([low, high], key=_search_rank_key)[0][1].score == 0.9


def test_ties_broken_deterministically_by_chunk_id():
    a = _pair("ws-a", 0.5, "c2")
    b = _pair("ws-b", 0.5, "c1")
    ordered = sorted([a, b], key=_search_rank_key)
    assert [p[1].chunk_id for p in ordered] == ["c1", "c2"]
    # Independent of input order.
    ordered2 = sorted([b, a], key=_search_rank_key)
    assert [p[1].chunk_id for p in ordered2] == ["c1", "c2"]
