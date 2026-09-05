"""Tests that conversation turn attribution surfaces on search results (#306).

Closes #306's remaining acceptance criterion: "Search returns conversation
chunks with role and turn attribution intact". The ingestion side already
stamps ``turn_index`` / ``turn_id`` / ``role`` / ``turn_ts`` / ``client`` onto
every conversation chunk (``chunk_conversation`` -> ``store.py`` ->
``weaviate.py``); these tests pin the *read* side -- that search GraphQL-selects
those properties and promotes them onto ``SearchResult``.

The Weaviate client and embedder are mocked; no live stack is required. We feed
chunks exactly as Weaviate returns them (a conversation chunk carries the
properties; a file-document chunk returns ``null`` for each, because
``store_chunks_with_tenant`` only sets them when the chunk actually came from
``chunk_conversation``).
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


async def _search(chunks: list[dict]) -> list:
    """Run a keyword search over ``chunks`` against a mocked Weaviate."""
    svc = _service()
    collection = _get_workspace_collection_name("ws1")
    svc._client = _mock_client(chunks, collection)
    return await svc._search_weaviate("ws1", "u1", SearchRequest(query="x", search_mode="keyword"))


# A conversation chunk as Weaviate returns it: every turn property set, because
# store_chunks_with_tenant promoted them from the chunk's metadata.
_CONVERSATION_CHUNK = {
    "document_id": "conv-1",
    "original_filename": "session-42",
    "content": "the deploy failed on the migration step",
    "chunk_index": 7,
    "chunking_strategy": "conversation_turn",
    "turn_index": 3,
    "turn_id": "t-abc",
    "role": "assistant",
    "turn_ts": "2026-09-01T10:15:00Z",
    "client": "agent-cli",
    "_additional": {"id": "c1", "score": "0.9"},
}

# An ordinary file-document chunk: Weaviate returns null for every turn
# property because the ingestion side never set them (see weaviate.py's
# "ONLY set for a chunk that actually came from chunk_conversation").
_FILE_CHUNK = {
    "document_id": "d1",
    "original_filename": "handbook.pdf",
    "content": "expense policy",
    "chunk_index": 0,
    "chunking_strategy": "sections",
    "turn_index": None,
    "turn_id": None,
    "role": None,
    "turn_ts": None,
    "client": None,
    "_additional": {"id": "c2", "score": "0.8"},
}


@pytest.mark.asyncio
async def test_conversation_turn_attribution_promoted_onto_result() -> None:
    """A conversation chunk's role/turn attribution reaches the search result."""
    results = await _search([dict(_CONVERSATION_CHUNK)])

    r = results[0]
    assert r.turn_index == 3
    assert r.turn_id == "t-abc"
    assert r.role == "assistant"
    assert r.turn_ts == "2026-09-01T10:15:00Z"
    assert r.client == "agent-cli"


@pytest.mark.asyncio
async def test_turn_index_zero_is_preserved() -> None:
    """turn_index 0 is the FIRST turn, not "missing" -- it must not be dropped.

    Guards the classic falsy-int bug: an ``if turn_index:`` style promotion
    would silently blank the attribution of every conversation's opening turn.
    """
    chunk = dict(_CONVERSATION_CHUNK, turn_index=0, role="user")
    results = await _search([chunk])

    assert results[0].turn_index == 0
    assert results[0].role == "user"


@pytest.mark.asyncio
async def test_file_document_chunk_has_no_turn_attribution() -> None:
    """No regression: an ordinary file chunk surfaces every turn field as None."""
    results = await _search([dict(_FILE_CHUNK)])

    r = results[0]
    assert r.turn_index is None
    assert r.turn_id is None
    assert r.role is None
    assert r.turn_ts is None
    assert r.client is None


@pytest.mark.asyncio
async def test_legacy_chunk_without_turn_properties_is_none() -> None:
    """Chunks stored before #306 (properties absent from the payload) must not error."""
    chunk = {
        "document_id": "d1",
        "original_filename": "legacy.txt",
        "content": "legacy chunk",
        "chunk_index": 0,
        "_additional": {"id": "c3", "score": "0.9"},
    }
    results = await _search([chunk])

    r = results[0]
    assert r.turn_index is None
    assert r.turn_id is None
    assert r.role is None
    assert r.turn_ts is None
    assert r.client is None


@pytest.mark.asyncio
async def test_blank_turn_ts_and_client_normalise_to_none() -> None:
    """Empty strings are Weaviate's "unset", not real values.

    ``store_chunks_with_tenant`` writes ``turn_ts``/``client`` as ``"" `` when
    the caller supplied neither (Weaviate has no null TEXT), so search must
    normalise them back to None rather than surface a meaningless empty string
    -- the same "only a level when it's notable" rule ``content_risk`` uses.
    """
    chunk = dict(_CONVERSATION_CHUNK, turn_ts="", client="")
    results = await _search([chunk])

    r = results[0]
    # Attribution that IS present still surfaces.
    assert r.turn_index == 3
    assert r.role == "assistant"
    # Attribution that was never supplied reads as absent.
    assert r.turn_ts is None
    assert r.client is None


@pytest.mark.asyncio
async def test_mixed_page_attributes_each_result_independently() -> None:
    """A page mixing conversation and file chunks attributes each one correctly."""
    results = await _search([dict(_CONVERSATION_CHUNK), dict(_FILE_CHUNK)])

    by_doc = {r.document_id: r for r in results}
    assert by_doc["conv-1"].role == "assistant"
    assert by_doc["conv-1"].turn_index == 3
    assert by_doc["d1"].role is None
    assert by_doc["d1"].turn_index is None


def test_graphql_projection_selects_turn_properties() -> None:
    """The query must SELECT the turn properties, or they can never be surfaced.

    Pins the read side against the ingestion schema (``_get_chunk_properties``
    in services/inh-ingestion-svc/src/services/weaviate.py): these five property
    names are the contract between the two services.
    """
    svc = _service()
    gql = svc._build_graphql(
        _get_workspace_collection_name("ws1"),
        "Tenant_x",
        SearchRequest(query="x", search_mode="keyword"),
    )["query"]

    for prop in ("turn_index", "turn_id", "role", "turn_ts", "client"):
        assert prop in gql, prop


@pytest.mark.asyncio
async def test_turn_properties_remain_in_metadata_passthrough() -> None:
    """Promotion is additive: the raw properties still ride in ``metadata``.

    Same contract as content_risk/source_uri -- promoted onto the result AND
    left in the passthrough, so a client reading either place keeps working.
    """
    results = await _search([dict(_CONVERSATION_CHUNK)])

    metadata = results[0].metadata or {}
    assert metadata["turn_index"] == 3
    assert metadata["role"] == "assistant"
    assert metadata["turn_id"] == "t-abc"
