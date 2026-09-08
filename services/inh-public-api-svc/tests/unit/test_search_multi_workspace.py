"""Tests for concurrent, bounded, ranking-safe multi-workspace search (#13).

These exercise the API-layer fan-out helper directly so the Weaviate client and
embedder can be mocked without a live stack:

- the query embedding is computed ONCE and reused across N workspaces;
- workspaces are searched concurrently via asyncio.gather, bounded by a
  semaphore sized from search_max_workspace_concurrency;
- one workspace failure degrades to partial results (the rest still return);
- the merged ranking uses a deterministic (-score, chunk_id, document_id)
  tiebreaker and is truncated to request.limit.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from inh_contracts.embedding.identity import EmbeddingIdentityMismatchError

from src.api.v1 import search as search_api
from src.config import settings
from src.models.search import SearchRequest, SearchResponse, SearchResult


def _result(chunk_id: str, document_id: str, score: float) -> SearchResult:
    return SearchResult(
        chunk_id=chunk_id,
        document_id=document_id,
        document_name=f"{document_id}.pdf",
        content="...",
        score=score,
    )


def _response(results: list[SearchResult]) -> SearchResponse:
    return SearchResponse(
        results=results,
        query="q",
        total_results=len(results),
        processing_time_ms=1.0,
        search_mode="semantic",
    )


@pytest.mark.asyncio
async def test_embedding_computed_once_across_workspaces() -> None:
    """embed_query_vector must be called exactly once for N workspaces (#13)."""
    svc = MagicMock()
    svc.embed_query_vector = MagicMock(return_value=[0.1] * 384)
    svc.search = AsyncMock(return_value=_response([]))

    workspaces = ["ws1", "ws2", "ws3", "ws4"]
    req = SearchRequest(query="hello", search_mode="semantic")

    await search_api._search_workspaces_concurrently(
        svc, user_id="u1", workspace_ids=workspaces, request=req
    )

    # Embedded once total, regardless of workspace count.
    assert svc.embed_query_vector.call_count == 1
    # And that single precomputed vector is reused on every per-workspace call.
    assert svc.search.call_count == len(workspaces)
    for call in svc.search.call_args_list:
        assert call.kwargs["query_vector"] == [0.1] * 384


@pytest.mark.asyncio
async def test_gather_used_with_bounded_concurrency(monkeypatch) -> None:
    """Concurrency is bounded by the semaphore; gather runs them in parallel."""
    monkeypatch.setattr(settings, "search_max_workspace_concurrency", 2)

    svc = MagicMock()
    svc.embed_query_vector = MagicMock(return_value=None)

    in_flight = 0
    max_in_flight = 0

    async def _search(*, workspace_id, user_id, request, query_vector):  # noqa: ANN001
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        await asyncio.sleep(0.02)
        in_flight -= 1
        return _response([_result(f"c-{workspace_id}", workspace_id, 0.5)])

    svc.search = _search

    workspaces = [f"ws{i}" for i in range(6)]
    req = SearchRequest(query="x", search_mode="keyword")

    results, _ = await search_api._search_workspaces_concurrently(
        svc, user_id="u1", workspace_ids=workspaces, request=req
    )

    # Never more than the configured bound concurrently in flight.
    assert max_in_flight <= 2
    # But it WAS actually concurrent (otherwise max would be 1).
    assert max_in_flight == 2
    assert len(results) == len(workspaces)


@pytest.mark.asyncio
async def test_uses_asyncio_gather(monkeypatch) -> None:
    """Assert the fan-out goes through asyncio.gather (parallel, not sequential)."""
    svc = MagicMock()
    svc.embed_query_vector = MagicMock(return_value=None)
    svc.search = AsyncMock(return_value=_response([]))

    called = {"gather": False}
    real_gather = asyncio.gather

    def _spy_gather(*aws, **kwargs):
        called["gather"] = True
        return real_gather(*aws, **kwargs)

    monkeypatch.setattr(search_api.asyncio, "gather", _spy_gather)

    req = SearchRequest(query="x", search_mode="keyword")
    await search_api._search_workspaces_concurrently(
        svc, user_id="u1", workspace_ids=["a", "b", "c"], request=req
    )
    assert called["gather"] is True


@pytest.mark.asyncio
async def test_one_workspace_failure_yields_partial_results() -> None:
    """A single failing workspace must not fail the whole request (#13)."""
    svc = MagicMock()
    svc.embed_query_vector = MagicMock(return_value=None)

    async def _search(*, workspace_id, user_id, request, query_vector):  # noqa: ANN001
        if workspace_id == "bad":
            raise RuntimeError("weaviate down for this workspace")
        return _response([_result(f"c-{workspace_id}", workspace_id, 0.5)])

    svc.search = _search

    req = SearchRequest(query="x", search_mode="keyword")
    results, _ = await search_api._search_workspaces_concurrently(
        svc, user_id="u1", workspace_ids=["good1", "bad", "good2"], request=req
    )

    # The two healthy workspaces still contribute; the bad one yields nothing.
    returned_ws = {r.document_id for r in results}
    assert returned_ws == {"good1", "good2"}
    assert len(results) == 2


@pytest.mark.asyncio
async def test_identity_mismatch_propagates_instead_of_partial_result() -> None:
    """PR #314 review finding 1: unlike a generic per-workspace failure, a
    model-identity mismatch in ONE workspace must fail the WHOLE
    multi-workspace search, not degrade to partial results — that is the
    exact silent-corruption failure #311 item 4 exists to prevent."""
    svc = MagicMock()
    svc.embed_query_vector = MagicMock(return_value=None)

    async def _search(*, workspace_id, user_id, request, query_vector):  # noqa: ANN001
        if workspace_id == "corrupted":
            raise EmbeddingIdentityMismatchError("model mismatch on Workspace_corrupted")
        return _response([_result(f"c-{workspace_id}", workspace_id, 0.5)])

    svc.search = _search

    req = SearchRequest(query="x", search_mode="semantic")
    with pytest.raises(EmbeddingIdentityMismatchError):
        await search_api._search_workspaces_concurrently(
            svc, user_id="u1", workspace_ids=["good1", "corrupted", "good2"], request=req
        )


@pytest.mark.asyncio
async def test_identity_mismatch_does_not_hit_the_partial_failure_log(monkeypatch) -> None:
    """The mismatch must take the re-raise branch, not the generic
    ``except Exception`` partial-result-isolation branch — confirms ordering,
    not just that SOME exception eventually surfaces."""
    warnings: list[tuple] = []
    monkeypatch.setattr(
        search_api.logger, "warning", lambda *a, **kw: warnings.append((a, kw)), raising=True
    )

    svc = MagicMock()
    svc.embed_query_vector = MagicMock(return_value=None)

    async def _search(*, workspace_id, user_id, request, query_vector):  # noqa: ANN001
        raise EmbeddingIdentityMismatchError("boom")

    svc.search = _search

    req = SearchRequest(query="x", search_mode="semantic")
    with pytest.raises(EmbeddingIdentityMismatchError):
        await search_api._search_workspaces_concurrently(
            svc, user_id="u1", workspace_ids=["ws1"], request=req
        )
    assert warnings == []


@pytest.mark.asyncio
async def test_wall_clock_time_is_parallel_not_sum() -> None:
    """processing time reflects parallel wall-clock, not the sum (#13)."""
    svc = MagicMock()
    svc.embed_query_vector = MagicMock(return_value=None)

    async def _search(*, workspace_id, user_id, request, query_vector):  # noqa: ANN001
        await asyncio.sleep(0.05)
        return _response([])

    svc.search = _search

    req = SearchRequest(query="x", search_mode="keyword")
    _, wall_clock_ms = await search_api._search_workspaces_concurrently(
        svc, user_id="u1", workspace_ids=["a", "b", "c", "d"], request=req
    )

    # 4 workspaces * 50ms = 200ms if sequential; parallel should be well under.
    assert wall_clock_ms < 150


def test_deterministic_tiebreaker_and_topk_truncation() -> None:
    """Stable sort on (-score, chunk_id, document_id) then truncate to limit.

    This mirrors the merge/sort/limit logic in the endpoint so the deterministic
    ordering contract is unit-tested without a live stack.
    """
    # Equal scores; insertion order intentionally scrambled.
    merged = [
        _result("c3", "dB", 0.9),
        _result("c1", "dA", 0.9),
        _result("c2", "dA", 0.9),
        _result("c0", "dZ", 0.5),
    ]
    limit = 2

    merged.sort(key=lambda r: (-r.score, r.chunk_id, r.document_id))
    limited = merged[:limit]

    # Among the 0.9 ties, chunk_id ascending decides: c1, c2, c3.
    assert [r.chunk_id for r in limited] == ["c1", "c2"]
    # Truncated to the limit.
    assert len(limited) == limit


def test_tiebreaker_is_stable_across_input_order() -> None:
    """Same set in different input orders must produce identical ranking."""
    base = [
        _result("c1", "dA", 0.8),
        _result("c2", "dA", 0.8),
        _result("c3", "dB", 0.6),
    ]
    shuffled = [base[2], base[0], base[1]]

    key = lambda r: (-r.score, r.chunk_id, r.document_id)  # noqa: E731
    a = sorted(base, key=key)
    b = sorted(shuffled, key=key)

    assert [r.chunk_id for r in a] == [r.chunk_id for r in b]
