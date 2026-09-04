"""Conversations are exempt from the ingested_at staleness rule (#306 follow-up).

Offline: no DB / MQ / Weaviate — the Weaviate client and embedder are mocked
the same way ``test_content_risk_surfacing.py`` mocks them.

Why the exemption exists: ``is_stale`` (#42) assumes a document whose chunks
are all re-stamped together, so an old ``ingested_at`` means "nothing has been
re-ingested since". ``ConversationMemoryWorkflow`` breaks that assumption —
every ~90s flush appends only its OWN new chunks (``append=True``) and leaves
earlier flushes' chunks untouched, so a live conversation's opening turns age
past ``freshness_max_age_days`` and would read ``is_stale=true`` while nothing
about them is actually stale. There is also no refresh path to clear it.

Every test here pairs the conversation assertion with the equivalent FILE
assertion, so the exemption can never quietly widen into "nothing is ever
stale".
"""

from __future__ import annotations

import datetime as _dt
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from inh_contracts.conversation import (
    CONVERSATION_CONTENT_TYPE,
    CONVERSATION_DOCUMENT_TYPE,
    FILE_DOCUMENT_TYPE,
)

from src.config import settings
from src.models.document import Document, DocumentChunk
from src.models.search import SearchRequest
from src.services.lineage import build_lineage
from src.services.search import SearchService, _get_workspace_collection_name

FILE_CONTENT_TYPE = "application/pdf"


def _old() -> datetime:
    """An ingested_at comfortably past the freshness window."""
    return datetime.now(UTC) - timedelta(days=settings.freshness_max_age_days + 30)


# --- The shared helper ------------------------------------------------------


class TestComputeIsStaleConversationExemption:
    def test_old_conversation_chunk_is_not_stale_by_content_type(self) -> None:
        assert (
            SearchService._compute_is_stale(_old(), content_type=CONVERSATION_CONTENT_TYPE) is False
        )

    def test_old_conversation_chunk_is_not_stale_by_document_type(self) -> None:
        assert (
            SearchService._compute_is_stale(_old(), document_type=CONVERSATION_DOCUMENT_TYPE)
            is False
        )

    def test_old_file_chunk_is_still_stale(self) -> None:
        """Regression guard: file documents keep their existing behavior."""
        assert SearchService._compute_is_stale(_old(), content_type=FILE_CONTENT_TYPE) is True
        assert SearchService._compute_is_stale(_old(), document_type=FILE_DOCUMENT_TYPE) is True

    def test_old_chunk_with_no_type_signal_is_still_stale(self) -> None:
        """Callers that pass neither signal are byte-identical to pre-change."""
        assert SearchService._compute_is_stale(_old()) is True

    def test_fresh_conversation_chunk_is_also_not_stale(self) -> None:
        recent = datetime.now(UTC) - timedelta(days=1)
        assert (
            SearchService._compute_is_stale(recent, content_type=CONVERSATION_CONTENT_TYPE) is False
        )


# --- The search path --------------------------------------------------------


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


async def _search_one(content_type: str | None) -> object:
    svc = _service()
    collection = _get_workspace_collection_name("ws1")
    chunk: dict = {
        "document_id": "d1",
        "original_filename": "thread-42",
        "content": "user: how do I rotate the key?",
        "chunk_index": 0,
        "ingested_at": _old().isoformat().replace("+00:00", "Z"),
        "_additional": {"id": "c1", "score": "0.9"},
    }
    if content_type is not None:
        chunk["content_type"] = content_type
    svc._client = _mock_client([chunk], collection)
    results = await svc._search_weaviate(
        "ws1", "u1", SearchRequest(query="x", search_mode="keyword")
    )
    return results[0]


@pytest.mark.asyncio
async def test_search_result_for_old_conversation_chunk_is_not_stale() -> None:
    r = await _search_one(CONVERSATION_CONTENT_TYPE)
    assert r.is_stale is False
    # The citation carries the same verdict — the two must never disagree.
    assert r.citation is not None
    assert r.citation.is_stale is False


@pytest.mark.asyncio
async def test_search_result_for_old_file_chunk_is_still_stale() -> None:
    r = await _search_one(FILE_CONTENT_TYPE)
    assert r.is_stale is True
    assert r.citation is not None
    assert r.citation.is_stale is True


@pytest.mark.asyncio
async def test_search_result_with_no_content_type_is_still_stale() -> None:
    """Chunks written before content_type was selected must not go silently fresh."""
    r = await _search_one(None)
    assert r.is_stale is True


def test_graphql_selects_content_type() -> None:
    """The exemption is only reachable if the query actually asks for the field."""
    svc = _service()
    collection = _get_workspace_collection_name("ws1")
    gql = svc._build_graphql(
        collection,
        "tenant_x",
        SearchRequest(query="x", search_mode="keyword"),
    )["query"]
    assert "content_type" in gql


# --- The lineage path -------------------------------------------------------


def _document(mime_type: str) -> Document:
    now = _dt.datetime.now()
    return Document(
        id="doc-1",
        name="thread-42",
        workspace_id="ws-1",
        source_type="local",
        mime_type=mime_type,
        size_bytes=10,
        chunk_count=1,
        status="processed",
        created_at=now,
        updated_at=now,
        metadata={},
    )


def _chunk(metadata: dict) -> DocumentChunk:
    return DocumentChunk(
        id="chunk-1",
        document_id="doc-1",
        content="user: how do I rotate the key?",
        chunk_index=0,
        metadata=metadata,
    )


def test_lineage_for_old_conversation_is_not_stale() -> None:
    lineage = build_lineage(
        _document(CONVERSATION_CONTENT_TYPE),
        [_chunk({"ingested_at": _old().isoformat()})],
    )
    assert lineage.ingested_at is not None
    assert lineage.is_stale is False


def test_lineage_for_old_file_is_still_stale() -> None:
    lineage = build_lineage(
        _document(FILE_CONTENT_TYPE),
        [_chunk({"ingested_at": _old().isoformat()})],
    )
    assert lineage.is_stale is True


def test_lineage_prefers_chunk_content_type_over_document_mime_type() -> None:
    """A conversation chunk stamps its own content_type; it wins over the row's."""
    lineage = build_lineage(
        _document(FILE_CONTENT_TYPE),
        [_chunk({"ingested_at": _old().isoformat(), "content_type": CONVERSATION_CONTENT_TYPE})],
    )
    assert lineage.is_stale is False
