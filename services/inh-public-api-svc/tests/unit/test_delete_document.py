"""Tests for document deletion — REST + MCP + vector/object cleanup (#87 P1).

An agent must be able to retract knowledge: deleting a document removes its
PostgreSQL row + chunks, its Weaviate objects (tenant-scoped), and best-effort
its stored S3 bytes. Tenant isolation is enforced on both surfaces: a caller
can never delete a document in a workspace they don't own.

Cleanup ordering pins the safe failure mode: vectors are deleted BEFORE the
database row, so a mid-flight failure leaves a retryable, still-visible
document rather than orphaned vectors that keep surfacing in search.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from src.main import create_app
from src.models.api_key import APIKeyInfo
from src.services.auth import ResolvedAuth, get_api_key_info, resolve_workspace_write
from src.services.database import get_database
from src.services.deletion import delete_document_everywhere

pytestmark = pytest.mark.asyncio

WS = "test-workspace-id"


def _key(*, permissions: list[str], workspace_id: str | None = WS) -> APIKeyInfo:
    return APIKeyInfo(
        key_id="key-1",
        user_id="test-user-id",
        workspace_id=workspace_id,
        permissions=permissions,  # type: ignore[arg-type]
        rate_limit=100,
        expires_at=None,
        status="active",
    )


def _upload_fields(document_id: str = "doc-001", workspace_id: str = WS) -> dict:
    return {
        "document_id": document_id,
        "workspace_id": workspace_id,
        "user_id": "test-user-id",
        "filename": "stored.pdf",
        "original_filename": "report.pdf",
        "content_type": "application/pdf",
        "size_bytes": 2048,
        "storage_backend": "s3",
        "storage_path": f"{workspace_id}/abc/stored.pdf",
        "storage_bucket": "docs",
        "storage_url": "s3://docs/stored.pdf",
    }


def _mock_db(found: bool = True) -> AsyncMock:
    db = AsyncMock()
    if found:
        db.get_document_upload_fields = AsyncMock(return_value=_upload_fields())
        db.delete_document = AsyncMock(
            return_value={"document_id": "doc-001", "chunk_count": 10, "size_bytes": 2048}
        )
    else:
        db.get_document_upload_fields = AsyncMock(return_value=None)
        db.delete_document = AsyncMock(return_value=None)
    return db


def _mock_search(vectors_deleted: int = 10) -> AsyncMock:
    search = AsyncMock()
    search.delete_document_vectors = AsyncMock(return_value=vectors_deleted)
    return search


def _mock_storage() -> MagicMock:
    storage = MagicMock()
    storage.delete_file = AsyncMock()
    return storage


def _patch_deletion_services(search: AsyncMock, storage: MagicMock):
    """Patch the vector/object services at the deletion-module boundary."""
    from src.services import deletion

    return [
        patch.object(deletion, "get_search_service", AsyncMock(return_value=search)),
        patch.object(deletion, "get_storage_service", MagicMock(return_value=storage)),
    ]


# ---------------------------------------------------------------------------
# Orchestration: delete_document_everywhere
# ---------------------------------------------------------------------------


class TestDeleteDocumentEverywhere:
    async def test_deletes_vectors_then_row_then_storage(self):
        db = _mock_db()
        search = _mock_search()
        storage = _mock_storage()
        order: list[str] = []
        search.delete_document_vectors.side_effect = lambda *a, **k: order.append("vectors") or 10
        db.delete_document.side_effect = lambda *a, **k: order.append("db") or {
            "document_id": "doc-001",
            "chunk_count": 10,
        }
        storage.delete_file.side_effect = lambda *a, **k: order.append("storage")

        p1, p2 = _patch_deletion_services(search, storage)
        with p1, p2:
            outcome = await delete_document_everywhere(db, "doc-001", WS)

        assert outcome.found is True
        assert outcome.vectors_deleted == 10
        assert outcome.chunks_deleted == 10
        assert order == ["vectors", "db", "storage"]
        # Vector delete targets the STORED row's tenant (the uploader), and the
        # workspace collection — both derived from the document, not the caller.
        search.delete_document_vectors.assert_awaited_once_with(WS, "test-user-id", "doc-001")
        db.delete_document.assert_awaited_once_with("doc-001", WS)
        storage.delete_file.assert_awaited_once_with(f"{WS}/abc/stored.pdf")

    async def test_not_found_touches_nothing(self):
        db = _mock_db(found=False)
        search = _mock_search()
        storage = _mock_storage()
        p1, p2 = _patch_deletion_services(search, storage)
        with p1, p2:
            outcome = await delete_document_everywhere(db, "doc-x", WS)

        assert outcome.found is False
        search.delete_document_vectors.assert_not_awaited()
        db.delete_document.assert_not_awaited()
        storage.delete_file.assert_not_awaited()

    async def test_vector_failure_aborts_before_db_delete(self):
        """A Weaviate failure must propagate BEFORE the row is deleted, so the
        operation stays retryable and no orphaned vectors survive a 'deleted'
        document."""
        db = _mock_db()
        search = _mock_search()
        search.delete_document_vectors = AsyncMock(side_effect=RuntimeError("weaviate down"))
        storage = _mock_storage()
        p1, p2 = _patch_deletion_services(search, storage)
        with p1, p2:
            with pytest.raises(RuntimeError, match="weaviate down"):
                await delete_document_everywhere(db, "doc-001", WS)

        db.delete_document.assert_not_awaited()
        storage.delete_file.assert_not_awaited()

    async def test_storage_failure_is_best_effort(self):
        db = _mock_db()
        search = _mock_search()
        storage = _mock_storage()
        storage.delete_file = AsyncMock(side_effect=RuntimeError("s3 down"))
        p1, p2 = _patch_deletion_services(search, storage)
        with p1, p2:
            outcome = await delete_document_everywhere(db, "doc-001", WS)

        assert outcome.found is True
        assert outcome.storage_deleted is False

    async def test_non_s3_backend_skips_storage_delete(self):
        db = _mock_db()
        fields = _upload_fields()
        fields["storage_backend"] = "local"
        db.get_document_upload_fields = AsyncMock(return_value=fields)
        search = _mock_search()
        storage = _mock_storage()
        p1, p2 = _patch_deletion_services(search, storage)
        with p1, p2:
            outcome = await delete_document_everywhere(db, "doc-001", WS)

        assert outcome.found is True
        storage.delete_file.assert_not_awaited()


# ---------------------------------------------------------------------------
# REST: DELETE /v1/documents/{document_id}
# ---------------------------------------------------------------------------


def _app_with(key: APIKeyInfo, db: AsyncMock, *, real_auth: bool = False):
    application = create_app()
    if real_auth:
        # Only fake key extraction; the real workspace resolution + permission
        # checks run so the tests below exercise the actual auth behavior.
        application.dependency_overrides[get_api_key_info] = lambda: key
    else:
        application.dependency_overrides[resolve_workspace_write] = lambda: ResolvedAuth(
            key_info=key, workspace_id=key.workspace_id
        )
    application.dependency_overrides[get_database] = lambda: db
    return application


async def _delete(application, document_id: str = "doc-001", headers: dict | None = None):
    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        return await ac.delete(
            f"/v1/documents/{document_id}",
            headers=headers or {"X-API-Key": "ink_test_key"},
        )


class TestDeleteDocumentEndpoint:
    async def test_delete_success_returns_204(self):
        db = _mock_db()
        search = _mock_search()
        storage = _mock_storage()
        app = _app_with(_key(permissions=["read", "write"]), db)
        p1, p2 = _patch_deletion_services(search, storage)
        with p1, p2:
            response = await _delete(app)

        assert response.status_code == 204
        assert response.content == b""
        db.delete_document.assert_awaited_once_with("doc-001", WS)
        app.dependency_overrides.clear()

    async def test_delete_not_found_returns_404(self):
        db = _mock_db(found=False)
        app = _app_with(_key(permissions=["read", "write"]), db)
        p1, p2 = _patch_deletion_services(_mock_search(), _mock_storage())
        with p1, p2:
            response = await _delete(app, "missing-doc")

        assert response.status_code == 404
        app.dependency_overrides.clear()

    async def test_delete_requires_write_permission(self):
        """Read-only key → 403 and no deletion side effects."""
        db = _mock_db()
        app = _app_with(_key(permissions=["read", "search"]), db, real_auth=True)
        p1, p2 = _patch_deletion_services(_mock_search(), _mock_storage())
        with p1, p2:
            response = await _delete(app)

        assert response.status_code == 403
        db.delete_document.assert_not_awaited()
        app.dependency_overrides.clear()

    async def test_workspace_scoped_key_cannot_target_other_workspace(self):
        """A workspace-scoped key with a mismatching X-Workspace-Id is rejected
        by the auth layer before any deletion logic runs."""
        db = _mock_db()
        app = _app_with(_key(permissions=["read", "write"]), db, real_auth=True)
        p1, p2 = _patch_deletion_services(_mock_search(), _mock_storage())
        with p1, p2:
            response = await _delete(
                app,
                headers={"X-API-Key": "ink_test_key", "X-Workspace-Id": "other-workspace"},
            )

        assert response.status_code == 403
        db.get_document_upload_fields.assert_not_awaited()
        db.delete_document.assert_not_awaited()
        app.dependency_overrides.clear()

    async def test_document_in_other_workspace_is_404_not_deleted(self):
        """Workspace scoping: the lookup is keyed on (document_id, workspace_id),
        so another workspace's document reads as not-found — no existence leak,
        nothing deleted."""
        db = _mock_db(found=False)  # scoped lookup misses
        app = _app_with(_key(permissions=["read", "write"]), db)
        p1, p2 = _patch_deletion_services(_mock_search(), _mock_storage())
        with p1, p2:
            response = await _delete(app, "someone-elses-doc")

        assert response.status_code == 404
        db.get_document_upload_fields.assert_awaited_once_with("someone-elses-doc", WS)
        db.delete_document.assert_not_awaited()
        app.dependency_overrides.clear()

    async def test_vector_cleanup_failure_returns_503_and_keeps_row(self):
        db = _mock_db()
        search = _mock_search()
        search.delete_document_vectors = AsyncMock(side_effect=RuntimeError("weaviate down"))
        app = _app_with(_key(permissions=["read", "write"]), db)
        p1, p2 = _patch_deletion_services(search, _mock_storage())
        with p1, p2:
            response = await _delete(app)

        assert response.status_code == 503
        db.delete_document.assert_not_awaited()
        app.dependency_overrides.clear()

    async def test_storage_failure_still_returns_204(self):
        db = _mock_db()
        storage = _mock_storage()
        storage.delete_file = AsyncMock(side_effect=RuntimeError("s3 down"))
        app = _app_with(_key(permissions=["read", "write"]), db)
        p1, p2 = _patch_deletion_services(_mock_search(), storage)
        with p1, p2:
            response = await _delete(app)

        assert response.status_code == 204
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# MCP: delete_document tool
# ---------------------------------------------------------------------------


from src.mcp_server import server as mcp_server  # noqa: E402


async def _call_tool(name: str, arguments: dict, mock_db, key_info: APIKeyInfo):
    """Drive a tool through the real call_tool dispatcher (auth + perm check)."""
    from mcp.types import CallToolRequest, CallToolRequestParams

    server = mcp_server.create_mcp_server()
    mock_db.validate_api_key = AsyncMock(return_value=key_info)
    with patch.object(mcp_server, "get_database", AsyncMock(return_value=mock_db)):
        req = CallToolRequest(
            method="tools/call",
            params=CallToolRequestParams(name=name, arguments=arguments),
        )
        handler = server.request_handlers[CallToolRequest]
        result = await handler(req)
        return result.root.content


class TestMcpDeleteDocument:
    async def test_tool_is_listed_with_write_permission(self):
        assert mcp_server._TOOL_PERMISSIONS.get("delete_document") == "write"

        from mcp.types import ListToolsRequest

        server = mcp_server.create_mcp_server()
        handler = server.request_handlers[ListToolsRequest]
        result = await handler(ListToolsRequest(method="tools/list"))
        tools = {t.name: t for t in result.root.tools}
        assert "delete_document" in tools
        assert set(tools["delete_document"].inputSchema["required"]) == {
            "api_key",
            "document_id",
        }

    async def test_denied_without_write_permission_never_deletes(self):
        db = _mock_db()
        result = await _call_tool(
            "delete_document",
            {"api_key": "k", "document_id": "doc-001"},
            db,
            _key(permissions=["read", "search"]),
        )
        assert "does not have 'write' permission" in result[0].text
        db.get_document_by_id.assert_not_called()
        db.delete_document.assert_not_awaited()

    async def test_no_access_to_document_is_rejected(self):
        """Tenant isolation on MCP: a document in a workspace the caller doesn't
        own is refused before any deletion side effect."""
        from datetime import datetime, timezone

        from src.models.document import Document

        db = _mock_db()
        db.get_document_by_id = AsyncMock(
            return_value=Document(
                id="doc-001",
                name="report.pdf",
                workspace_id="other-workspace",
                source_type="s3",
                mime_type="application/pdf",
                size_bytes=2048,
                chunk_count=10,
                status="processed",
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
                metadata=None,
            )
        )
        db.get_user_workspace_ids = AsyncMock(return_value=[WS])

        p1, p2 = _patch_deletion_services(_mock_search(), _mock_storage())
        with p1, p2:
            result = await _call_tool(
                "delete_document",
                {"api_key": "k", "document_id": "doc-001"},
                db,
                _key(permissions=["read", "write"], workspace_id=None),
            )

        assert "don't have access" in result[0].text
        db.delete_document.assert_not_awaited()

    async def test_delete_success_returns_structured_payload(self):
        import json
        from datetime import datetime, timezone

        from src.models.document import Document

        db = _mock_db()
        db.get_document_by_id = AsyncMock(
            return_value=Document(
                id="doc-001",
                name="report.pdf",
                workspace_id=WS,
                source_type="s3",
                mime_type="application/pdf",
                size_bytes=2048,
                chunk_count=10,
                status="processed",
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
                metadata=None,
            )
        )
        db.get_user_workspace_ids = AsyncMock(return_value=[WS])

        p1, p2 = _patch_deletion_services(_mock_search(), _mock_storage())
        with p1, p2:
            result = await _call_tool(
                "delete_document",
                {"api_key": "k", "document_id": "doc-001"},
                db,
                _key(permissions=["read", "write"], workspace_id=None),
            )

        text = result[0].text
        block = text.split("```json", 1)[1].rsplit("```", 1)[0].strip()
        payload = json.loads(block)["structured"]
        assert payload["document_id"] == "doc-001"
        assert payload["workspace_id"] == WS
        assert payload["deleted"] is True
        assert payload["chunks_deleted"] == 10
        db.delete_document.assert_awaited_once_with("doc-001", WS)

    async def test_missing_document_id_is_an_error(self):
        """The SDK enforces the input schema: document_id is required, so a
        call without it never reaches the handler (defense in depth — the
        handler also checks)."""
        db = _mock_db()
        result = await _call_tool(
            "delete_document",
            {"api_key": "k"},
            db,
            _key(permissions=["read", "write"]),
        )
        assert "document_id" in result[0].text
        assert "required" in result[0].text
        db.delete_document.assert_not_awaited()


# ---------------------------------------------------------------------------
# SearchService.delete_document_vectors (Weaviate batch delete)
# ---------------------------------------------------------------------------


class TestDeleteDocumentVectors:
    def _service(self):
        from src.services.search import SearchService

        return SearchService(database=AsyncMock(), weaviate_url="http://weaviate:8080")

    async def test_issues_tenant_scoped_batch_delete(self):
        from src.services.search import (
            _get_user_tenant_name,
            _get_workspace_collection_name,
        )

        service = self._service()
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {"results": {"matches": 7, "successful": 7, "failed": 0}}
        client = AsyncMock()
        client.request = AsyncMock(return_value=response)
        service._client = client

        deleted = await service.delete_document_vectors(WS, "test-user-id", "doc-001")

        assert deleted == 7
        client.request.assert_awaited_once()
        call = client.request.call_args
        assert call[0][0] == "DELETE"
        assert call[0][1] == "/v1/batch/objects"
        assert call.kwargs["params"] == {"tenant": _get_user_tenant_name("test-user-id")}
        body = call.kwargs["json"]
        assert body["match"]["class"] == _get_workspace_collection_name(WS)
        where = body["match"]["where"]
        assert where["path"] == ["document_id"]
        assert where["operator"] == "Equal"
        assert where["valueText"] == "doc-001"

    async def test_missing_collection_treated_as_already_clean(self):
        """A workspace whose collection was never created (nothing ingested yet)
        deletes cleanly: 0 vectors, no raise."""
        from src.services.search import _get_workspace_collection_name

        service = self._service()
        response = MagicMock()
        response.status_code = 422
        response.text = (
            f'{{"error":[{{"message":"could not find class '
            f'{_get_workspace_collection_name(WS)} in schema"}}]}}'
        )
        client = AsyncMock()
        client.request = AsyncMock(return_value=response)
        service._client = client

        deleted = await service.delete_document_vectors(WS, "test-user-id", "doc-001")
        assert deleted == 0

    async def test_other_weaviate_error_raises(self):
        service = self._service()
        response = MagicMock()
        response.status_code = 500
        response.text = "internal error"
        client = AsyncMock()
        client.request = AsyncMock(return_value=response)
        service._client = client

        with pytest.raises(RuntimeError):
            await service.delete_document_vectors(WS, "test-user-id", "doc-001")
