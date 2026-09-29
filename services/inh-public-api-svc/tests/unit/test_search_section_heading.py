"""Search returns a chunk's section heading (inherent#390 / #392).

The numbered_sections chunker records each chunk's own heading; ingestion now
mirrors it to a Weaviate ``section_heading`` property. Search must SELECT it,
or no result -- and no pack MCP profile tool, which reads
``result.metadata["section_heading"]`` -- can ever show one (found by the live
pilot-flow E2E: every result came back headingless). Mocked Weaviate HTTP, no
live services.
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


async def _run_search(chunk: dict) -> tuple[list, str]:
    service = SearchService(MagicMock(), "http://weaviate:8080")
    response = MagicMock()
    response.status_code = 200
    response.raise_for_status = MagicMock()
    response.json.return_value = {"data": {"Get": {_get_workspace_collection_name("ws1"): [chunk]}}}
    client = AsyncMock()
    client.post.return_value = response
    service._client = client

    result = await service.search("ws1", "u1", SearchRequest(query="test", limit=5))
    graphql = client.post.call_args.kwargs["json"]["query"]
    return result.results, graphql


@pytest.mark.asyncio
async def test_search_selects_section_heading_from_weaviate():
    _, graphql = await _run_search(
        {"document_id": "d1", "content": "x", "_additional": {"id": "c1", "score": "0.9"}}
    )
    assert "section_heading" in graphql


@pytest.mark.asyncio
async def test_result_metadata_carries_the_section_heading():
    results, _ = await _run_search(
        {
            "document_id": "d1",
            "original_filename": "a.docx",
            "content": "1.1 Some clause text",
            "section_heading": "1.1 Some clause text",
            "_additional": {"id": "c1", "score": "0.9"},
        }
    )
    assert results[0].metadata["section_heading"] == "1.1 Some clause text"
