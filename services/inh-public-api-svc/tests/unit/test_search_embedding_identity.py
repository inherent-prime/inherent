"""Weaviate collection model-identity guard on the query path (#311 item 4).

Read-only counterpart to inh-ingestion-svc's write-path guard
(``WeaviateService._check_or_stamp_collection_identity``): a query against a
collection built with a different embedding model must hard-fail instead of
silently returning plausible-looking noise. Covers the same four cases the
issue calls out (matching passes, mismatched model_id raises, mismatched
dimension raises, unstamped legacy collection follows the documented policy)
plus the query-path-specific behaviors: never stamps, fails open on a schema
fetch problem, is cached per collection, and is skipped entirely for
keyword-only search (no vector is used at all).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from inh_contracts.embedding.identity import EmbeddingIdentityMismatchError, encode_identity
from inh_contracts.embedding.provider import EmbeddingIdentity

from src.models.search import SearchRequest
from src.services.search import SearchService, _get_workspace_collection_name

CURRENT = EmbeddingIdentity(model_id="BAAI/bge-small-en-v1.5", dimension=384)


@pytest.fixture(autouse=True)
def stub_active_identity(monkeypatch):
    """Pin the "active provider identity" the guard compares against."""
    monkeypatch.setattr(
        "src.services.embedder.get_active_embedding_identity", lambda: CURRENT, raising=False
    )


@pytest.fixture(autouse=True)
def stub_embed_query(monkeypatch):
    def _fake(text: str) -> tuple[float, ...]:
        return tuple(0.0 for _ in range(384))

    monkeypatch.setattr("src.services.embedder.embed_query", _fake, raising=False)
    monkeypatch.setattr("src.services.search.embed_query", _fake, raising=False)


def _service_with_schema(
    *,
    description: str | None = None,
    status: int = 200,
    raise_error: BaseException | None = None,
) -> tuple[SearchService, list[str]]:
    svc = SearchService(database=MagicMock(), weaviate_url="http://fake")
    client = AsyncMock(spec=httpx.AsyncClient)
    calls: list[str] = []

    async def _get(path, **_kwargs):
        calls.append(path)
        if raise_error is not None:
            raise raise_error
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = status
        resp.json.return_value = {"description": description}
        return resp

    client.get = _get
    svc._client = client
    return svc, calls


# --- the four cases the issue calls out --------------------------------------------------------


@pytest.mark.asyncio
async def test_matching_identity_passes() -> None:
    coll = _get_workspace_collection_name("ws1")
    svc, calls = _service_with_schema(description=encode_identity(CURRENT))

    await svc._ensure_identity_checked(coll)  # must not raise

    assert calls == [f"/v1/schema/{coll}"]
    assert coll in svc._identity_checked


@pytest.mark.asyncio
async def test_mismatched_model_id_raises() -> None:
    coll = _get_workspace_collection_name("ws1")
    stale = EmbeddingIdentity(model_id="some-other-model", dimension=384)
    svc, _calls = _service_with_schema(description=encode_identity(stale))

    with pytest.raises(EmbeddingIdentityMismatchError):
        await svc._ensure_identity_checked(coll)
    assert coll not in svc._identity_checked


@pytest.mark.asyncio
async def test_mismatched_dimension_raises() -> None:
    coll = _get_workspace_collection_name("ws1")
    stale = EmbeddingIdentity(model_id=CURRENT.model_id, dimension=768)
    svc, _calls = _service_with_schema(description=encode_identity(stale))

    with pytest.raises(EmbeddingIdentityMismatchError):
        await svc._ensure_identity_checked(coll)
    assert coll not in svc._identity_checked


@pytest.mark.asyncio
async def test_unstamped_legacy_collection_passes_without_raising() -> None:
    coll = _get_workspace_collection_name("ws1")
    svc, _calls = _service_with_schema(description=None)

    await svc._ensure_identity_checked(coll)  # must not raise

    assert coll in svc._identity_checked


# --- query-path-specific behavior ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_query_path_never_writes_schema_even_when_adopting_legacy() -> None:
    """Deliberate asymmetry vs. the write path: query never stamps (see
    _ensure_identity_checked's docstring)."""
    coll = _get_workspace_collection_name("ws1")
    svc, _calls = _service_with_schema(description=None)
    client = svc._client

    await svc._ensure_identity_checked(coll)

    client.put.assert_not_called()
    client.patch.assert_not_called()
    client.post.assert_not_called()


@pytest.mark.asyncio
async def test_second_call_is_cached_does_not_refetch_schema() -> None:
    coll = _get_workspace_collection_name("ws1")
    svc, calls = _service_with_schema(description=encode_identity(CURRENT))

    await svc._ensure_identity_checked(coll)
    await svc._ensure_identity_checked(coll)

    assert calls == [f"/v1/schema/{coll}"]  # only the first call hit the network


@pytest.mark.asyncio
async def test_schema_fetch_network_error_fails_open() -> None:
    """A schema-endpoint outage must not masquerade as an identity mismatch --
    the real connectivity problem surfaces from the GraphQL query itself."""
    coll = _get_workspace_collection_name("ws1")
    svc, _calls = _service_with_schema(raise_error=httpx.ConnectError("weaviate unreachable"))

    await svc._ensure_identity_checked(coll)  # must not raise

    assert coll not in svc._identity_checked  # not treated as verified, either


@pytest.mark.asyncio
async def test_schema_endpoint_404_fails_open() -> None:
    """A collection that doesn't exist yet has nothing to assert against --
    _search_weaviate's own missing-collection handling covers the empty
    result, not this guard."""
    coll = _get_workspace_collection_name("ws1")
    svc, _calls = _service_with_schema(status=404)

    await svc._ensure_identity_checked(coll)  # must not raise


@pytest.mark.asyncio
async def test_keyword_mode_search_never_checks_identity() -> None:
    """Pure BM25 never touches the vector space -- the guard must not even
    fetch the schema for a keyword-only search."""
    svc, calls = _service_with_schema(description=encode_identity(CURRENT))
    client = svc._client

    async def _post(path, json=None, **_kwargs):
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 200
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {"data": {"Get": {_get_workspace_collection_name("ws1"): []}}}
        return resp

    client.post = _post

    results = await svc._search_weaviate(
        "ws1", "u1", SearchRequest(query="hi", search_mode="keyword")
    )

    assert results == []
    assert calls == []  # schema endpoint never hit
