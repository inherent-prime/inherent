"""Unit tests for GET /v1/documents/{id}/lineage (#40) and the shared builders.

Offline: the database is mocked and auth dependencies are overridden, so no real
services are touched. Also asserts that the REST search route and the MCP search
tools build an EQUIVALENT SearchRequest from the same params (shared builder, no
drift).
"""

from __future__ import annotations

import datetime as _dt
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from src.config.settings import settings as _app_settings
from src.main import create_app
from src.models.api_key import APIKeyInfo
from src.models.document import Document, DocumentChunk
from src.models.search import SearchRequest
from src.services.auth import (
    ResolvedAuth,
    get_api_key_info,
    get_read_permission,
    resolve_workspace_read,
)
from src.services.database import get_database
from src.services.search import build_search_request


@pytest.fixture
def read_key() -> APIKeyInfo:
    return APIKeyInfo(
        key_id="r-key",
        user_id="user-1",
        workspace_id="ws-1",
        permissions=["read"],
        rate_limit=100,
        expires_at=None,
        status="active",
    )


@pytest.fixture
def lineage_doc() -> Document:
    return Document(
        id="doc-1",
        name="report.pdf",
        workspace_id="ws-1",
        source_type="s3",
        mime_type="application/pdf",
        size_bytes=10,
        chunk_count=1,
        status="processed",
        created_at=_dt.datetime.now(),
        updated_at=_dt.datetime.now(),
        metadata={"storage_url": "https://example/report.pdf"},
    )


# Freshness fixtures are RELATIVE to now, never absolute (#332).
# These previously pinned ingested_at to a literal date and asserted
# is_stale is False. That silently expires: the assertion held until the
# date aged past settings.freshness_max_age_days (90), then failed forever
# -- and it failed on main, so every open PR inherited a red job for a
# reason unrelated to its own diff. Anchor to now +/- the threshold so the
# tests assert the BEHAVIOUR (recent evidence is fresh, old evidence is
# stale) rather than a calendar date.
_FRESH_INGESTED_AT = (_dt.datetime.now(_dt.UTC) - _dt.timedelta(days=1)).isoformat()
_STALE_INGESTED_AT = (
    _dt.datetime.now(_dt.UTC) - _dt.timedelta(days=_app_settings.freshness_max_age_days + 1)
).isoformat()


def _chunk_ingested_at(ingested_at: str) -> DocumentChunk:
    return DocumentChunk(
        id="chunk-1",
        document_id="doc-1",
        content="text",
        chunk_index=0,
        metadata={
            "source_uri": "s3://bucket/report.pdf",
            "content_hash": "abc123",
            "ingested_at": ingested_at,
        },
    )


@pytest.fixture
def lineage_chunk() -> DocumentChunk:
    return _chunk_ingested_at(_FRESH_INGESTED_AT)


@pytest.fixture
def mock_db(lineage_doc, lineage_chunk) -> AsyncMock:
    mock = AsyncMock()
    mock.get_document = AsyncMock(return_value=lineage_doc)
    mock.get_document_chunks = AsyncMock(return_value=[lineage_chunk])
    mock.get_user_workspace_ids = AsyncMock(return_value=["ws-1"])
    return mock


@pytest.fixture
def app(read_key, mock_db):
    application = create_app()
    application.dependency_overrides[get_api_key_info] = lambda: read_key
    application.dependency_overrides[get_read_permission] = lambda: read_key
    application.dependency_overrides[resolve_workspace_read] = lambda: ResolvedAuth(
        key_info=read_key, workspace_id="ws-1"
    )
    application.dependency_overrides[get_database] = lambda: mock_db
    yield application
    application.dependency_overrides.clear()


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


class TestLineageEndpoint:
    async def test_evidence_older_than_the_threshold_is_flagged_stale(self, mock_db, client):
        """The mirror image of the fresh case (#332).

        Without this, nothing pins is_stale=True, so a bug that hardwired the
        flag to False would look identical to a passing suite -- which is
        exactly how the expiring fixture this test accompanies stayed
        invisible until the day it flipped.
        """
        mock_db.get_document_chunks = AsyncMock(
            return_value=[_chunk_ingested_at(_STALE_INGESTED_AT)]
        )
        resp = await client.get("/v1/documents/doc-1/lineage", headers={"X-API-Key": "k"})
        assert resp.status_code == 200
        assert resp.json()["is_stale"] is True

    async def test_returns_provenance_and_freshness(self, client):
        resp = await client.get("/v1/documents/doc-1/lineage", headers={"X-API-Key": "k"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["document_name"] == "report.pdf"
        assert body["source_uri"] == "s3://bucket/report.pdf"
        assert body["content_hash"] == "abc123"
        assert body["ingested_at"].startswith(_FRESH_INGESTED_AT[:10])
        assert body["is_stale"] is False
        assert body["chunk_id"] == "chunk-1"

    async def test_404_when_document_missing(self, client, mock_db):
        mock_db.get_document = AsyncMock(return_value=None)
        resp = await client.get("/v1/documents/missing/lineage", headers={"X-API-Key": "k"})
        assert resp.status_code == 404

    async def test_404_when_chunk_missing(self, client):
        resp = await client.get(
            "/v1/documents/doc-1/lineage",
            params={"chunk_id": "nope"},
            headers={"X-API-Key": "k"},
        )
        assert resp.status_code == 404

    async def test_requires_read_permission(self, app, mock_db):
        # Drop the read override so the real get_read_permission runs against a
        # search-only key — it must 403.
        app.dependency_overrides.pop(get_read_permission, None)
        app.dependency_overrides.pop(resolve_workspace_read, None)
        search_only = APIKeyInfo(
            key_id="s",
            user_id="user-1",
            workspace_id="ws-1",
            permissions=["search"],
            rate_limit=100,
            expires_at=None,
            status="active",
        )
        app.dependency_overrides[get_api_key_info] = lambda: search_only
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/v1/documents/doc-1/lineage", headers={"X-API-Key": "k"})
        assert resp.status_code == 403


class TestSharedSearchBuilder:
    """The shared builder must produce the SAME SearchRequest the REST body
    parser would, given the same params — proving REST and MCP cannot drift."""

    def test_builder_equivalent_to_direct_model(self):
        params = {
            "query": "what is x",
            "limit": 7,
            "min_score": 0.4,
            "search_mode": "hybrid",
            "alpha": 0.3,
            "document_ids": ["d1", "d2"],
            "include_context": True,
            "context_window": 4,
        }
        # REST: FastAPI validates the body straight into SearchRequest(**params).
        rest_request = SearchRequest(**params)
        # MCP: goes through the shared builder, plus transport-only keys it must
        # ignore.
        mcp_request = build_search_request({**params, "api_key": "k", "workspace_id": "ws-1"})
        assert mcp_request == rest_request

    def test_builder_ignores_unknown_keys_and_none(self):
        request = build_search_request(
            {"query": "x", "api_key": "k", "workspace_id": None, "limit": None}
        )
        assert request.query == "x"
        # limit None dropped → model default applies.
        assert request.limit == 10
