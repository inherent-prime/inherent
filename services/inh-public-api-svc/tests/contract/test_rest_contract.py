"""REST contract regression tests (M6 #30).

Locks down the public REST contract for client SDKs/agents:

- **Response shapes** of SearchResponse / SearchResult / DocumentUploadResponse /
  SupportVerdict / the lineage response / DocumentListResponse — required fields
  present, optional fields omittable / backward-compatible.
- **Permissions** — search→'search', documents-read→'read', upload/refresh→
  'write', verify/lineage→'read'. Missing permission → 403; missing / invalid /
  expired key → 401.
- **Error shape** — auth/not-found (``HTTPException``) bodies carry ``detail``;
  validation (422) and ``InherentAPIError`` (400) bodies are RFC 7807
  ``application/problem+json`` with type/title/status/detail.

All offline: auth dependencies are overridden per-test to select a permission
set, the DB / search / storage / MQ layers are mocked, and the lifespan DB init
is stubbed. Patterns mirror tests/unit/test_search_endpoint.py and
tests/integration/test_api_path.py.
"""

from __future__ import annotations

import io
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from src.main import create_app
from src.models.api_key import APIKeyInfo
from src.models.document import (
    DocumentListResponse,
    DocumentUploadResponse,
)
from src.models.search import QualityVerdict, SearchResponse, SearchResult
from src.services import document_intake
from src.services.auth import (
    ResolvedAuth,
    get_api_key_info,
    get_read_permission,
    get_search_permission,
    get_write_permission,
    resolve_workspace_read,
    resolve_workspace_search,
    resolve_workspace_write,
)
from src.services.database import get_database
from src.services.search import get_search_service

pytestmark = [pytest.mark.contract]


# --------------------------------------------------------------------------- #
# App / client helpers.
# --------------------------------------------------------------------------- #
def _resolved(key: APIKeyInfo) -> ResolvedAuth:
    return ResolvedAuth(key_info=key, workspace_id=key.workspace_id)


def _build_app(
    *,
    key: APIKeyInfo,
    db: AsyncMock | None = None,
    search_svc: AsyncMock | None = None,
    override_permissions: bool = True,
):
    """Create an app with auth dependencies overridden.

    When ``override_permissions`` is True the per-route permission gates are
    overridden so the route body runs (used for response-shape tests). When
    False, only ``get_api_key_info`` is overridden so the REAL permission gates
    run — that is how the permission-denied (403) tests exercise the contract.
    """
    app = create_app()
    app.dependency_overrides[get_api_key_info] = lambda: key

    if db is not None:
        app.dependency_overrides[get_database] = lambda: db
    if search_svc is not None:
        app.dependency_overrides[get_search_service] = lambda: search_svc

    if override_permissions:
        app.dependency_overrides[get_read_permission] = lambda: key
        app.dependency_overrides[get_search_permission] = lambda: key
        app.dependency_overrides[get_write_permission] = lambda: key
        app.dependency_overrides[resolve_workspace_read] = lambda: _resolved(key)
        app.dependency_overrides[resolve_workspace_search] = lambda: _resolved(key)
        app.dependency_overrides[resolve_workspace_write] = lambda: _resolved(key)
    return app


def _client(app) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


_HDR = {"X-API-Key": "ink_test"}


# =========================================================================== #
# Response shapes
# =========================================================================== #
class TestSearchResponseShape:
    async def test_search_response_required_and_optional_fields(
        self, full_key, single_workspace_db, mock_search_service
    ):
        """SearchResponse carries the required fields and the #43 optionals
        (quality_verdict / performed_fallback / fallback_strategy)."""
        # Give the gate a sufficient verdict so no fallback runs and the verdict
        # is surfaced on the response.
        resp = SearchResponse(
            results=[
                SearchResult(
                    chunk_id="c1",
                    document_id="d1",
                    document_name="n.pdf",
                    content="passage",
                    score=0.95,
                )
            ],
            query="q",
            total_results=1,
            processing_time_ms=10.0,
            search_mode="semantic",
            quality_verdict=QualityVerdict(verdict="sufficient", reason_code="ok", confidence=0.9),
        )
        mock_search_service.search = AsyncMock(return_value=resp)

        app = _build_app(key=full_key, db=single_workspace_db, search_svc=mock_search_service)
        async with _client(app) as c:
            r = await c.post("/v1/search", json={"query": "q"}, headers=_HDR)
        assert r.status_code == 200
        body = r.json()
        # Required SearchResponse fields.
        for field in (
            "results",
            "query",
            "total_results",
            "processing_time_ms",
            "search_mode",
            "total_tokens",
        ):
            assert field in body, field
        # #43 optional/backward-compatible fields are always present in the JSON
        # (the gate runs server-side; we assert the SHAPE, not its verdict).
        assert "quality_verdict" in body
        assert "performed_fallback" in body
        assert "fallback_strategy" in body
        assert isinstance(body["performed_fallback"], bool)
        # quality_verdict is either null or the QualityVerdict shape.
        if body["quality_verdict"] is not None:
            assert set(body["quality_verdict"]) == {
                "verdict",
                "reason_code",
                "confidence",
            }
        # Re-validate against the model so the contract is enforced by pydantic.
        SearchResponse.model_validate(body)

    async def test_search_result_optionals_omittable(
        self, full_key, single_workspace_db, mock_search_service
    ):
        """A SearchResult with ONLY core fields validates — provenance /
        freshness / risk / citation optionals are omittable (backward-compat)."""
        app = _build_app(key=full_key, db=single_workspace_db, search_svc=mock_search_service)
        async with _client(app) as c:
            r = await c.post("/v1/search", json={"query": "q"}, headers=_HDR)
        assert r.status_code == 200
        result = r.json()["results"][0]
        # Core fields present.
        for field in ("chunk_id", "document_id", "document_name", "content", "score"):
            assert field in result, field
        # Optional provenance/freshness/risk/citation fields are omittable:
        # when the chunk lacks them they serialise to None / default, never error.
        assert result.get("score_source") is None
        assert result.get("content_hash") is None
        assert result.get("source_uri") is None
        assert result.get("citation") is None
        assert result.get("content_risk") is None
        assert result["is_stale"] is False
        # And a result built from JUST the core fields validates.
        SearchResult.model_validate(
            {
                "chunk_id": "x",
                "document_id": "y",
                "document_name": "z",
                "content": "c",
                "score": 0.5,
            }
        )


class TestDocumentListResponseShape:
    async def test_document_list_response_shape(self, read_key, single_workspace_db):
        app = _build_app(key=read_key, db=single_workspace_db)
        async with _client(app) as c:
            r = await c.get("/v1/documents", headers=_HDR)
        assert r.status_code == 200
        body = r.json()
        assert set(body) >= {"documents", "total", "page", "page_size"}
        DocumentListResponse.model_validate(body)
        doc = body["documents"][0]
        for field in (
            "id",
            "name",
            "workspace_id",
            "source_type",
            "size_bytes",
            "chunk_count",
            "status",
            "created_at",
            "updated_at",
        ):
            assert field in doc, field


class TestSingleChunkResponseShape:
    """GET /v1/chunks/{document_id}/{chunk_id} (#87 single-chunk fetch)."""

    async def test_get_chunk_response_shape(self, read_key, single_workspace_db):
        from src.models.document import DocumentChunk

        chunk = DocumentChunk(
            id="chunk-1",
            document_id="doc-1",
            content="the relevant passage",
            chunk_index=0,
            token_count=12,
            metadata={"heading": "Intro"},
        )
        single_workspace_db.get_document_chunk = AsyncMock(return_value=chunk)
        app = _build_app(key=read_key, db=single_workspace_db)
        async with _client(app) as c:
            r = await c.get("/v1/chunks/doc-1/chunk-1", headers=_HDR)
        assert r.status_code == 200
        body = r.json()
        for field in ("id", "document_id", "content", "chunk_index", "token_count", "metadata"):
            assert field in body, field
        assert body["id"] == "chunk-1"
        assert body["document_id"] == "doc-1"
        DocumentChunk.model_validate(body)

    async def test_get_chunk_404_unknown_chunk(self, read_key, single_workspace_db):
        """Unknown chunk_id under a real document -> 404 with detail."""
        single_workspace_db.get_document_chunk = AsyncMock(return_value=None)
        app = _build_app(key=read_key, db=single_workspace_db)
        async with _client(app) as c:
            r = await c.get("/v1/chunks/doc-1/chunk-missing", headers=_HDR)
        assert r.status_code == 404
        assert "detail" in r.json()

    async def test_get_chunk_tenant_isolation_foreign_workspace_404(
        self, read_key, single_workspace_db
    ):
        """A chunk that belongs to a different workspace must read as 404, not leak."""
        single_workspace_db.get_document_chunk = AsyncMock(return_value=None)
        app = _build_app(key=read_key, db=single_workspace_db)
        app.dependency_overrides[resolve_workspace_read] = lambda: ResolvedAuth(
            key_info=read_key, workspace_id="ws-1"
        )
        async with _client(app) as c:
            r = await c.get("/v1/chunks/doc-foreign/chunk-foreign", headers=_HDR)
        assert r.status_code == 404
        single_workspace_db.get_document_chunk.assert_awaited_once_with(
            document_id="doc-foreign", chunk_id="chunk-foreign", workspace_id="ws-1"
        )


class TestDocumentUploadResponseShape:
    async def test_upload_response_shape(self, write_key):
        app = _build_app(key=write_key, db=AsyncMock())
        # Stub storage + MQ so no real I/O happens.
        storage = MagicMock()
        storage.generate_key = MagicMock(return_value="ws-1/file.txt")
        storage.upload_file = AsyncMock(return_value=None)
        storage.build_storage_url = MagicMock(return_value="s3://b/ws-1/file.txt")
        storage._bucket = "b"
        db = AsyncMock()
        db.get_document_id_by_content_hash = AsyncMock(return_value=None)
        db.get_document_id_by_filename = AsyncMock(return_value=None)
        db.create_or_reset_pending_document = AsyncMock(return_value=None)
        app.dependency_overrides[get_database] = lambda: db
        app.dependency_overrides[resolve_workspace_write] = lambda: ResolvedAuth(
            key_info=write_key, workspace_id="ws-1"
        )

        with (
            patch.object(document_intake, "get_storage_service", return_value=storage),
            patch.object(
                document_intake,
                "get_mq_service",
                new=AsyncMock(return_value=AsyncMock()),
            ),
        ):
            async with _client(app) as c:
                r = await c.post(
                    "/v1/documents",
                    files={"file": ("file.txt", io.BytesIO(b"hello"), "text/plain")},
                    headers=_HDR,
                )
        assert r.status_code == 201
        body = r.json()
        for field in (
            "document_id",
            "name",
            "workspace_id",
            "storage_url",
            "mime_type",
            "size_bytes",
            "status",
            "message",
        ):
            assert field in body, field
        assert body["status"] == "pending"
        DocumentUploadResponse.model_validate(body)


class TestSupportVerdictShape:
    async def test_verify_claim_support_verdict_shape(self, read_key):
        app = _build_app(key=read_key, db=AsyncMock())
        async with _client(app) as c:
            r = await c.post(
                "/v1/verify-claim",
                json={"claim": "the sky is blue", "evidence": ["the sky is blue today"]},
                headers=_HDR,
            )
        assert r.status_code == 200
        body = r.json()
        assert set(body) == {"support_level", "score", "reason"}
        assert body["support_level"] in {"strong", "weak", "none"}
        assert 0.0 <= body["score"] <= 1.0


class TestLineageResponseShape:
    async def test_lineage_response_shape(self, read_key, single_workspace_db):
        app = _build_app(key=read_key, db=single_workspace_db)
        app.dependency_overrides[resolve_workspace_read] = lambda: ResolvedAuth(
            key_info=read_key, workspace_id="ws-1"
        )
        async with _client(app) as c:
            r = await c.get("/v1/documents/doc-1/lineage", headers=_HDR)
        assert r.status_code == 200
        body = r.json()
        for field in (
            "document_id",
            "document_name",
            "workspace_id",
            "chunk_id",
            "source_uri",
            "content_hash",
            "ingested_at",
            "is_stale",
            "status",
        ):
            assert field in body, field
        assert isinstance(body["is_stale"], bool)


# =========================================================================== #
# Permissions
# =========================================================================== #
class TestSearchPermission:
    async def test_search_requires_search_permission(self, read_key, mock_search_service):
        """A read-only key (no 'search') is rejected with 403."""
        app = _build_app(
            key=read_key,
            search_svc=mock_search_service,
            override_permissions=False,  # let the real gate run
        )
        async with _client(app) as c:
            r = await c.post("/v1/search", json={"query": "q"}, headers=_HDR)
        assert r.status_code == 403
        # The search service must never run when permission is denied.
        mock_search_service.search.assert_not_called()

    async def test_search_allowed_with_search_permission(
        self, search_only_key, single_workspace_db, mock_search_service
    ):
        app = _build_app(
            key=search_only_key,
            db=single_workspace_db,
            search_svc=mock_search_service,
        )
        async with _client(app) as c:
            r = await c.post("/v1/search", json={"query": "q"}, headers=_HDR)
        assert r.status_code == 200


class TestDocumentsReadPermission:
    async def test_list_documents_requires_read(self, search_only_key, single_workspace_db):
        """A search-only key (no 'read') cannot list documents → 403."""
        app = _build_app(
            key=search_only_key,
            db=single_workspace_db,
            override_permissions=False,
        )
        async with _client(app) as c:
            r = await c.get("/v1/documents", headers=_HDR)
        assert r.status_code == 403

    async def test_lineage_requires_read(self, search_only_key, single_workspace_db):
        app = _build_app(
            key=search_only_key,
            db=single_workspace_db,
            override_permissions=False,
        )
        async with _client(app) as c:
            r = await c.get("/v1/documents/doc-1/lineage", headers=_HDR)
        assert r.status_code == 403

    async def test_get_chunk_requires_read(self, search_only_key, single_workspace_db):
        """A search-only key (no 'read') cannot fetch a single chunk -> 403."""
        app = _build_app(
            key=search_only_key,
            db=single_workspace_db,
            override_permissions=False,
        )
        async with _client(app) as c:
            r = await c.get("/v1/chunks/doc-1/chunk-1", headers=_HDR)
        assert r.status_code == 403


class TestVerifyPermission:
    async def test_verify_requires_read(self, search_only_key):
        app = _build_app(
            key=search_only_key,
            db=AsyncMock(),
            override_permissions=False,
        )
        async with _client(app) as c:
            r = await c.post(
                "/v1/verify-claim",
                json={"claim": "x", "evidence": []},
                headers=_HDR,
            )
        assert r.status_code == 403


class TestWritePermission:
    async def test_upload_requires_write(self, read_key):
        """A read-only key (no 'write') cannot upload → 403."""
        app = _build_app(key=read_key, db=AsyncMock(), override_permissions=False)
        async with _client(app) as c:
            r = await c.post(
                "/v1/documents",
                files={"file": ("f.txt", io.BytesIO(b"hi"), "text/plain")},
                headers=_HDR,
            )
        assert r.status_code == 403

    async def test_refresh_requires_write(self, read_key, single_workspace_db):
        app = _build_app(key=read_key, db=single_workspace_db, override_permissions=False)
        async with _client(app) as c:
            r = await c.post("/v1/documents/doc-1/refresh", headers=_HDR)
        assert r.status_code == 403


# =========================================================================== #
# Authentication (missing / invalid / expired key → 401)
# =========================================================================== #
class TestAuthentication:
    async def test_missing_key_returns_401(self):
        """No API key header → 401 (real auth dependency, DB stubbed)."""
        app = create_app()
        with patch("src.main.get_database", new_callable=AsyncMock):
            async with _client(app) as c:
                r = await c.post("/v1/search", json={"query": "q"})
        assert r.status_code == 401

    async def test_invalid_key_returns_401(self):
        """A key the DB does not recognise → 401."""
        app = create_app()
        db = AsyncMock()
        db.validate_api_key = AsyncMock(return_value=None)
        with patch("src.services.auth.get_database", new=AsyncMock(return_value=db)):
            # Reset the cached auth-service singleton so it picks up our mock DB.
            import src.services.auth as auth_mod

            auth_mod._auth_service = None
            async with _client(app) as c:
                r = await c.post("/v1/search", json={"query": "q"}, headers=_HDR)
            auth_mod._auth_service = None
        assert r.status_code == 401

    async def test_expired_key_returns_401(self, expired_key):
        """A recognised-but-expired key → 401 (is_expired check in require_api_key)."""
        app = create_app()
        db = AsyncMock()
        db.validate_api_key = AsyncMock(return_value=expired_key)
        with patch("src.services.auth.get_database", new=AsyncMock(return_value=db)):
            import src.services.auth as auth_mod

            auth_mod._auth_service = None
            async with _client(app) as c:
                r = await c.post("/v1/search", json={"query": "q"}, headers=_HDR)
            auth_mod._auth_service = None
        assert r.status_code == 401


# =========================================================================== #
# Error shape (the documented contract for each status)
# =========================================================================== #
class TestErrorShape:
    async def test_401_carries_detail(self):
        """Auth failures are HTTPException → body carries a 'detail' string."""
        app = create_app()
        with patch("src.main.get_database", new_callable=AsyncMock):
            async with _client(app) as c:
                r = await c.post("/v1/search", json={"query": "q"})
        assert r.status_code == 401
        assert "detail" in r.json()

    async def test_403_carries_detail(self, read_key, mock_search_service):
        app = _build_app(key=read_key, search_svc=mock_search_service, override_permissions=False)
        async with _client(app) as c:
            r = await c.post("/v1/search", json={"query": "q"}, headers=_HDR)
        assert r.status_code == 403
        assert "detail" in r.json()

    async def test_404_carries_detail(self, read_key):
        """A not-found document is HTTPException(404) → 'detail' body."""
        db = AsyncMock()
        db.get_document = AsyncMock(return_value=None)
        db.get_user_workspace_ids = AsyncMock(return_value=["ws-1"])
        app = _build_app(key=read_key, db=db)
        app.dependency_overrides[resolve_workspace_read] = lambda: ResolvedAuth(
            key_info=read_key, workspace_id="ws-1"
        )
        async with _client(app) as c:
            r = await c.get("/v1/documents/missing", headers=_HDR)
        assert r.status_code == 404
        assert "detail" in r.json()

    async def test_422_is_rfc7807_problem_json(
        self, full_key, single_workspace_db, mock_search_service
    ):
        """Request validation failures use RFC 7807 application/problem+json
        with type/title/status/detail."""
        app = _build_app(key=full_key, db=single_workspace_db, search_svc=mock_search_service)
        async with _client(app) as c:
            # Missing required 'query' field → 422.
            r = await c.post("/v1/search", json={}, headers=_HDR)
        assert r.status_code == 422
        assert r.headers["content-type"].startswith("application/problem+json")
        body = r.json()
        for field in ("type", "title", "status", "detail"):
            assert field in body, field
        assert body["status"] == 422

    async def test_inherent_api_error_is_rfc7807_problem_json(self, write_key):
        """An InherentAPIError (here BadRequestError for an empty upload) is
        rendered as RFC 7807 problem+json with type/title/status/detail."""
        app = _build_app(key=write_key, db=AsyncMock())
        app.dependency_overrides[resolve_workspace_write] = lambda: ResolvedAuth(
            key_info=write_key, workspace_id="ws-1"
        )
        async with _client(app) as c:
            r = await c.post(
                "/v1/documents",
                files={"file": ("empty.txt", io.BytesIO(b""), "text/plain")},
                headers=_HDR,
            )
        assert r.status_code == 400
        assert r.headers["content-type"].startswith("application/problem+json")
        body = r.json()
        for field in ("type", "title", "status", "detail"):
            assert field in body, field
        assert body["status"] == 400
