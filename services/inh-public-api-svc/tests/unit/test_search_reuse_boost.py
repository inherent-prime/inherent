"""Usage-based ranking boost tests (inherent#394).

Weaviate client and embedder mocked -- no live stack required. Covers: the
boost is a no-op with no WORKSPACE_REUSE_BOOST override (byte-for-byte
identical ordering to before this feature existed), a no-op for
reuse_count == 0 even WITH an override, the boost actually reorders results
when it should, and it applies identically across semantic/hybrid/keyword.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.config import settings
from src.models.search import SearchRequest
from src.services.search import SearchService, _get_workspace_collection_name


@pytest.fixture(autouse=True)
def stub_embed_query(monkeypatch):
    """Prevent loading the embedding model / hitting the TEI sidecar."""

    def _fake(text: str) -> tuple[float, ...]:
        return tuple(0.0 for _ in range(384))

    monkeypatch.setattr("src.services.embedder.embed_query", _fake, raising=False)
    monkeypatch.setattr("src.services.search.embed_query", _fake, raising=False)


@pytest.fixture(autouse=True)
def reset_reuse_boost_setting(monkeypatch):
    """Isolate settings.workspace_reuse_boost per test (module-level singleton)."""
    original = dict(settings.workspace_reuse_boost)
    yield
    settings._workspace_reuse_boost = original  # type: ignore[attr-defined]


def _service() -> SearchService:
    return SearchService(database=MagicMock(), weaviate_url="http://fake")


def _mock_client(chunks: list[dict], collection_name: str) -> AsyncMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {"data": {"Get": {collection_name: chunks}}}
    client = AsyncMock()
    client.post.return_value = resp
    return client


def _chunk(document_id: str, chunk_id: str, score: float, reuse_count: int = 0) -> dict:
    return {
        "document_id": document_id,
        "original_filename": "a.txt",
        "content": f"content for {document_id}",
        "chunk_index": 0,
        "reuse_count": reuse_count,
        "_additional": {"id": chunk_id, "score": str(score)},
    }


@pytest.mark.asyncio
async def test_no_workspace_override_leaves_ordering_byte_identical() -> None:
    """No WORKSPACE_REUSE_BOOST entry for this workspace -- scores and order
    must be EXACTLY what they were before this feature existed."""
    settings._workspace_reuse_boost = {}  # type: ignore[attr-defined]
    svc = _service()
    collection = _get_workspace_collection_name("ws1")
    chunks = [
        _chunk("d1", "c1", 0.9, reuse_count=50),  # huge reuse_count -- must not matter
        _chunk("d2", "c2", 0.5, reuse_count=0),
    ]
    svc._client = _mock_client(chunks, collection)

    req = SearchRequest(query="q", search_mode="keyword")
    results = await svc._search_weaviate("ws1", "u1", req)

    assert [r.chunk_id for r in results] == ["c1", "c2"]
    assert results[0].score == 0.9
    assert results[1].score == 0.5


@pytest.mark.asyncio
async def test_zero_reuse_count_is_unboosted_even_with_override() -> None:
    settings._workspace_reuse_boost = {"ws1": 0.5}  # type: ignore[attr-defined]
    svc = _service()
    collection = _get_workspace_collection_name("ws1")
    chunks = [_chunk("d1", "c1", 0.6, reuse_count=0)]
    svc._client = _mock_client(chunks, collection)

    req = SearchRequest(query="q", search_mode="keyword")
    results = await svc._search_weaviate("ws1", "u1", req)

    assert results[0].score == 0.6  # unchanged
    assert results[0].reuse_count == 0


@pytest.mark.asyncio
async def test_reused_chunk_outranks_otherwise_equal_unused_chunk() -> None:
    """The ranking test the issue explicitly asks for: two chunks with the
    SAME base score, one reused -- the reused one must come out on top after
    the boost, having started (Weaviate-sorted) BEHIND the unused one."""
    settings._workspace_reuse_boost = {"ws1": 0.2}  # type: ignore[attr-defined]
    svc = _service()
    collection = _get_workspace_collection_name("ws1")
    # Weaviate returns them in its own (pre-boost) order: unused chunk first.
    chunks = [
        _chunk("d_unused", "c_unused", 0.70, reuse_count=0),
        _chunk("d_reused", "c_reused", 0.70, reuse_count=5),
    ]
    svc._client = _mock_client(chunks, collection)

    req = SearchRequest(query="q", search_mode="keyword")
    results = await svc._search_weaviate("ws1", "u1", req)

    assert [r.chunk_id for r in results] == ["c_reused", "c_unused"]
    assert results[0].score > results[1].score


@pytest.mark.asyncio
async def test_boost_is_capped() -> None:
    """An extreme reuse_count + weight must not exceed the hard cap (1.5x)."""
    settings._workspace_reuse_boost = {"ws1": 1.0}  # type: ignore[attr-defined]
    svc = _service()
    collection = _get_workspace_collection_name("ws1")
    chunks = [_chunk("d1", "c1", 0.5, reuse_count=100_000)]
    svc._client = _mock_client(chunks, collection)

    req = SearchRequest(query="q", search_mode="keyword")
    results = await svc._search_weaviate("ws1", "u1", req)

    assert results[0].score <= 0.75 + 1e-9  # 0.5 * 1.5 cap


@pytest.mark.parametrize("mode,alpha", [("keyword", None), ("semantic", None), ("hybrid", 0.5)])
@pytest.mark.asyncio
async def test_boost_applies_consistently_across_modes(mode: str, alpha: float | None) -> None:
    settings._workspace_reuse_boost = {"ws1": 0.3}  # type: ignore[attr-defined]
    svc = _service()
    collection = _get_workspace_collection_name("ws1")
    if mode == "semantic":
        chunk = {
            "document_id": "d1",
            "original_filename": "a.txt",
            "content": "x",
            "chunk_index": 0,
            "reuse_count": 4,
            "_additional": {"id": "c1", "score": None, "certainty": 0.6},
        }
    else:
        chunk = _chunk("d1", "c1", 0.6, reuse_count=4)
    svc._client = _mock_client([chunk], collection)

    kwargs = {"search_mode": mode}
    if alpha is not None:
        kwargs["alpha"] = alpha
    req = SearchRequest(query="q", **kwargs)
    results = await svc._search_weaviate("ws1", "u1", req)

    assert results[0].score > 0.6
    assert results[0].reuse_count == 4
