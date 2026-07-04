"""A search against a workspace with no Weaviate collection yet must return
empty results, not 500.

A workspace has no collection until its first document is ingested; Weaviate
answers a query against the non-existent class with HTTP 422 /
``Cannot query field "<Collection>"``. That is "nothing indexed yet", so search
returns ``[]`` (fixes brand-new-workspace 500s and the ingest→search race on a
fresh stack). A missing *property* (real schema drift) must still raise.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from src.models.search import SearchRequest
from src.services.search import (
    SearchService,
    _get_workspace_collection_name,
)


@pytest.fixture(autouse=True)
def stub_embed_query(monkeypatch):
    def _fake(text: str) -> tuple[float, ...]:
        return tuple(0.0 for _ in range(384))

    monkeypatch.setattr("src.services.embedder.embed_query", _fake, raising=False)
    monkeypatch.setattr("src.services.search.embed_query", _fake, raising=False)


def _service_returning(
    *, gql: dict | None = None, http_422_body: str | None = None
) -> SearchService:
    svc = SearchService(database=MagicMock(), weaviate_url="http://fake")
    client = AsyncMock(spec=httpx.AsyncClient)

    async def _post(path, json=None, **_):  # noqa: ANN001
        resp = MagicMock(spec=httpx.Response)
        if http_422_body is not None:
            resp.status_code = 422
            resp.text = http_422_body
            resp.raise_for_status = MagicMock(
                side_effect=httpx.HTTPStatusError("422", request=MagicMock(), response=resp)
            )
        else:
            resp.status_code = 200
            resp.raise_for_status = MagicMock()
            resp.json.return_value = gql
        return resp

    client.post = _post
    svc._client = client
    return svc


def test_is_missing_collection_discriminates_class_vs_property():
    coll = _get_workspace_collection_name("ws1")
    assert SearchService._is_missing_collection(
        f'Cannot query field "{coll}" on type "GetObjectsObj"', coll
    )
    # A missing PROPERTY (collection is the *type*) is NOT a missing collection.
    assert not SearchService._is_missing_collection(
        f'Cannot query field "content_hash" on type "{coll}"', coll
    )


async def test_graphql_missing_collection_returns_empty():
    coll = _get_workspace_collection_name("ws1")
    svc = _service_returning(
        gql={"errors": [{"message": f'Cannot query field "{coll}" on type "GetObjectsObj"'}]}
    )
    out = await svc._search_weaviate("ws1", "u1", SearchRequest(query="hi"))
    assert out == []


async def test_http_422_missing_collection_returns_empty():
    coll = _get_workspace_collection_name("ws1")
    svc = _service_returning(http_422_body=f'Cannot query field "{coll}" on type "GetObjectsObj"')
    out = await svc._search_weaviate("ws1", "u1", SearchRequest(query="hi"))
    assert out == []


async def test_any_http_422_returns_empty():
    # Weaviate briefly 422s while a class/tenant is being created (ingest->search
    # race); any search 422 is treated as "nothing queryable yet" -> empty.
    svc = _service_returning(http_422_body="some transient unprocessable-entity message")
    out = await svc._search_weaviate("ws1", "u1", SearchRequest(query="hi"))
    assert out == []


async def test_tenant_not_found_returns_empty():
    svc = _service_returning(
        gql={"errors": [{"message": 'tenant not found: _get_user_tenant_name("u1")'}]}
    )
    out = await svc._search_weaviate("ws1", "u1", SearchRequest(query="hi"))
    assert out == []


async def test_missing_property_still_raises():
    coll = _get_workspace_collection_name("ws1")
    svc = _service_returning(
        gql={"errors": [{"message": f'Cannot query field "content_hash" on type "{coll}"'}]}
    )
    with pytest.raises(httpx.HTTPError):
        await svc._search_weaviate("ws1", "u1", SearchRequest(query="hi"))
