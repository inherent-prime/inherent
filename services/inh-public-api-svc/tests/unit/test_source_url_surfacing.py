"""source_url surfaces on search results / citations (inherent#391).

Same offline harness as test_content_risk_surfacing.py: the Weaviate client
and embedder are mocked, so no live stack is required. We feed chunks with a
``source_url`` property (as Weaviate would return it) and assert it is
promoted onto both ``SearchResult`` and its nested ``Citation``, that an
absent/unsafe value degrades to ``None`` rather than erroring, and that it is
distinct from ``source_uri``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.models.search import SearchRequest
from src.services.search import SearchService, _get_workspace_collection_name


@pytest.fixture(autouse=True)
def stub_embed_query(monkeypatch):
    def _fake(text: str) -> tuple[float, ...]:
        return tuple(0.0 for _ in range(384))

    monkeypatch.setattr("src.services.embedder.embed_query", _fake, raising=False)
    monkeypatch.setattr("src.services.search.embed_query", _fake, raising=False)


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


def _chunk(**overrides) -> dict:
    base = {
        "document_id": "d1",
        "original_filename": "a.txt",
        "content": "hello world",
        "chunk_index": 0,
        "source_uri": "workspaces/ws1/a.txt",
        "_additional": {"id": "c1", "score": "0.9"},
    }
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_source_url_promoted_onto_result_and_citation() -> None:
    svc = _service()
    collection = _get_workspace_collection_name("ws1")
    svc._client = _mock_client(
        [_chunk(source_url="https://drive.google.com/file/d/abc123/view")],
        collection,
    )

    req = SearchRequest(query="x", search_mode="keyword")
    results = await svc._search_weaviate("ws1", "u1", req)

    r = results[0]
    assert r.source_url == "https://drive.google.com/file/d/abc123/view"
    # Distinct from source_uri -- this engine's own stored copy, unchanged.
    assert r.source_uri == "workspaces/ws1/a.txt"
    assert r.citation is not None
    assert r.citation.source_url == "https://drive.google.com/file/d/abc123/view"


@pytest.mark.asyncio
async def test_missing_source_url_is_none() -> None:
    """The vast majority of chunks (no connector link) must not error."""
    svc = _service()
    collection = _get_workspace_collection_name("ws1")
    svc._client = _mock_client([_chunk()], collection)

    req = SearchRequest(query="x", search_mode="keyword")
    results = await svc._search_weaviate("ws1", "u1", req)

    assert results[0].source_url is None
    assert results[0].citation.source_url is None


@pytest.mark.asyncio
async def test_unsafe_source_url_degrades_to_none() -> None:
    """Defense in depth: a chunk written before validation existed (or
    written directly, bypassing the upload boundary) must not surface an
    unsafe value just because it made it into Weaviate."""
    svc = _service()
    collection = _get_workspace_collection_name("ws1")
    svc._client = _mock_client([_chunk(source_url="javascript:alert(1)")], collection)

    req = SearchRequest(query="x", search_mode="keyword")
    results = await svc._search_weaviate("ws1", "u1", req)

    assert results[0].source_url is None


@pytest.mark.asyncio
async def test_non_string_source_url_is_none() -> None:
    """A malformed (non-string) property value must not raise."""
    svc = _service()
    collection = _get_workspace_collection_name("ws1")
    svc._client = _mock_client([_chunk(source_url=123)], collection)

    req = SearchRequest(query="x", search_mode="keyword")
    results = await svc._search_weaviate("ws1", "u1", req)

    assert results[0].source_url is None
