"""Tests for the #134/#137 chunk-edit hardening.

#137 (bug): WeaviateService.update_chunk() only pushed the new ``content``
property and never passed a fresh ``vector=``. Chunk collections have no
server-side vectorizer (Configure.Vectorizer.none()) -- vectors are supplied
explicitly at store time -- so an edit left the OLD embedding attached to the
NEW text: semantic search kept matching stale content while
get_document/list_chunks (PG-backed) already showed the edit.

#134 (security): PATCH /chunks/{document_id}/{chunk_index} was gated only by
verify_api_key, with no check that the caller's workspace actually owns
document_id. workspace_id/user_id were left unset on ChunkEditInput, so the
Weaviate write derived its collection/tenant from empty strings -- no tenant
scope enforced at all. The fix resolves document_id against PostgreSQL
(mirroring resolve_workspace_read's "match or 404" pattern used by the
public-API read paths) and only forwards the *resolved* workspace_id/user_id,
never the caller's.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.config.settings import Settings
from src.services.weaviate import (
    WeaviateService,
    get_user_tenant_name,
    get_workspace_collection_name,
)

# ---------------------------------------------------------------------------
# Override conftest autouse fixtures -- these tests don't need PostgreSQL.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
async def cleanup_test_data():
    """Override global autouse cleanup -- no DB needed for these tests."""
    yield


@pytest.fixture()
def db_service():
    """Override -- API/unit tests below mock the DB service directly."""
    yield None


WS1 = get_workspace_collection_name("ws1")
U_OWNER = get_user_tenant_name("user_owner")


# ---------------------------------------------------------------------------
# #137 -- WeaviateService.update_chunk re-embeds and writes a fresh vector
# ---------------------------------------------------------------------------


class TestUpdateChunkReembeds:
    @pytest.fixture
    def mock_settings(self):
        settings = MagicMock(spec=Settings)
        settings.weaviate_url = "http://localhost:8080"
        settings.weaviate_api_key = None
        return settings

    @pytest.fixture
    def weaviate_service(self, mock_settings):
        service = WeaviateService(mock_settings)
        service.client = MagicMock()
        service.client.is_ready.return_value = True
        return service

    @pytest.mark.asyncio
    async def test_update_chunk_reembeds_and_sets_new_vector(self, weaviate_service):
        """The new content is re-embedded and the fresh vector is written.

        Also pins content_hash/ingested_at advancing alongside content
        (judge blocker 3): a partial property write that bumps content but
        leaves the old hash/timestamp behind would make the public API's
        content_hash contract (sha256 of the *returned* content) false for a
        legitimately edited chunk -- the exact #9 defect, reintroduced on the
        Weaviate/search surface instead of PG. A test against the OLD code
        fails here on multiple fronts: no ``vector`` kwarg at all (KeyError),
        and ``properties`` only ever contained ``content``.
        """
        mock_collection = MagicMock()
        mock_tenant_collection = MagicMock()
        weaviate_service.client.collections.get.return_value = mock_collection
        mock_collection.with_tenant.return_value = mock_tenant_collection

        new_vector = [0.1, 0.2, 0.3]
        with patch("src.services.embedder.embed_text", return_value=new_vector) as mock_embed:
            await weaviate_service.update_chunk(
                document_id="doc1",
                chunk_index=2,
                content="brand new content",
                workspace_id="ws1",
                user_id="user1",
            )

        mock_embed.assert_called_once_with("brand new content")
        mock_tenant_collection.data.update.assert_called_once()
        _, kwargs = mock_tenant_collection.data.update.call_args
        props = kwargs["properties"]
        assert props["content"] == "brand new content"
        assert props["content_hash"] == hashlib.sha256(b"brand new content").hexdigest()
        # ingested_at must be bumped (freshly stamped), not omitted/stale.
        assert isinstance(props["ingested_at"], datetime)
        assert props["ingested_at"].tzinfo is not None
        assert kwargs["vector"] == new_vector

    @pytest.mark.asyncio
    async def test_update_chunk_scopes_to_correct_collection_and_tenant(self, weaviate_service):
        """Collection/tenant are still derived from workspace_id/user_id."""
        mock_collection = MagicMock()
        mock_tenant_collection = MagicMock()
        weaviate_service.client.collections.get.return_value = mock_collection
        mock_collection.with_tenant.return_value = mock_tenant_collection

        with patch("src.services.embedder.embed_text", return_value=[0.0, 0.0]):
            await weaviate_service.update_chunk(
                document_id="doc1",
                chunk_index=0,
                content="text",
                workspace_id="ws1",
                user_id="user1",
            )

        weaviate_service.client.collections.get.assert_called_once_with(WS1)
        mock_collection.with_tenant.assert_called_once_with(get_user_tenant_name("user1"))


# ---------------------------------------------------------------------------
# #134 -- PATCH /chunks/{document_id}/{chunk_index} enforces workspace
# ownership before writing anywhere.
# ---------------------------------------------------------------------------

VALID_API_KEY = "test-secret-key-abc123"


def _make_mock_settings(**overrides):
    defaults = {
        "ingestion_api_key": VALID_API_KEY,
        "api_host": "127.0.0.1",
        "api_port": 8000,
        "temporal_host": "localhost:7233",
        "temporal_namespace": "default",
        "temporal_task_queue": "document-ingestion",
        "log_level": "INFO",
    }
    defaults.update(overrides)
    s = MagicMock()
    for k, v in defaults.items():
        setattr(s, k, v)
    return s


@pytest.fixture()
def client():
    """TestClient with Temporal mocked; DB is mocked per-test via patch."""
    mock_settings = _make_mock_settings()

    mock_temporal_client = AsyncMock()
    mock_handle = AsyncMock()
    mock_handle.result = AsyncMock(return_value=MagicMock(success=True, error=None))
    mock_temporal_client.start_workflow = AsyncMock(return_value=mock_handle)

    with (
        patch("src.api.app.TemporalWorkerManager") as mock_manager_cls,
        patch("src.api.auth.get_settings", return_value=mock_settings),
    ):
        instance = mock_manager_cls.return_value
        instance.start = AsyncMock()
        instance.stop = AsyncMock()
        instance.get_client = AsyncMock(return_value=mock_temporal_client)
        instance.is_running = True

        from src.api.app import create_app

        app = create_app(mock_settings)

        with TestClient(app) as tc:
            tc._mock_temporal_client = mock_temporal_client
            yield tc


class TestChunkEditWorkspaceOwnership:
    """PATCH /chunks/{document_id}/{chunk_index}."""

    def test_requires_auth(self, client: TestClient):
        resp = client.patch(
            "/chunks/doc1/0?workspace_id=ws1",
            json={"content": "new text"},
        )
        assert resp.status_code == 401

    def test_requires_workspace_id_query_param(self, client: TestClient):
        """No workspace context at all -- old code accepted this and wrote
        to Weaviate with workspace_id="" (the #134 vulnerability's root
        cause). The fixed endpoint must require it."""
        resp = client.patch(
            "/chunks/doc1/0",
            json={"content": "new text"},
            headers={"X-API-Key": VALID_API_KEY},
        )
        assert resp.status_code == 422

    @patch("src.temporal.shared_services.get_db_service")
    def test_unknown_document_returns_404(self, mock_get_db, client: TestClient):
        mock_db_svc = MagicMock()
        mock_db_svc.get_document_status = AsyncMock(return_value=None)
        mock_get_db.return_value = mock_db_svc

        resp = client.patch(
            "/chunks/doc_missing/0?workspace_id=ws1",
            json={"content": "new text"},
            headers={"X-API-Key": VALID_API_KEY},
        )

        assert resp.status_code == 404
        client._mock_temporal_client.start_workflow.assert_not_called()

    @patch("src.temporal.shared_services.get_db_service")
    def test_foreign_workspace_returns_404_and_never_starts_workflow(
        self, mock_get_db, client: TestClient
    ):
        """Document exists but is owned by a DIFFERENT workspace than the
        caller claims. This is the exact #134 cross-tenant scenario: without
        the ownership check, the old code would have started the edit
        workflow anyway (writing into an empty-string-scoped tenant)."""
        mock_db_svc = MagicMock()
        mock_db_svc.get_document_status = AsyncMock(
            return_value={
                "document_id": "doc1",
                "workspace_id": "ws_owner",
                "user_id": "user_owner",
            }
        )
        mock_get_db.return_value = mock_db_svc

        resp = client.patch(
            "/chunks/doc1/0?workspace_id=ws_attacker",
            json={"content": "malicious content"},
            headers={"X-API-Key": VALID_API_KEY},
        )

        assert resp.status_code == 404
        client._mock_temporal_client.start_workflow.assert_not_called()

    @patch("src.temporal.shared_services.get_db_service")
    def test_success_propagates_resolved_workspace_and_user(self, mock_get_db, client: TestClient):
        """On a legitimate same-workspace edit, the workflow input carries
        the *resolved* workspace_id/user_id from PostgreSQL -- not caller
        input -- so the downstream Weaviate write is tenant-scoped."""
        mock_db_svc = MagicMock()
        mock_db_svc.get_document_status = AsyncMock(
            return_value={
                "document_id": "doc1",
                "workspace_id": "ws1",
                "user_id": "user_owner",
                "chunk_count": 5,
            }
        )
        mock_get_db.return_value = mock_db_svc

        resp = client.patch(
            "/chunks/doc1/3?workspace_id=ws1",
            json={"content": "updated text"},
            headers={"X-API-Key": VALID_API_KEY},
        )

        assert resp.status_code == 200
        assert resp.json() == {
            "document_id": "doc1",
            "chunk_index": 3,
            "updated": True,
        }

        call_args = client._mock_temporal_client.start_workflow.call_args
        workflow_input = call_args.args[1]
        assert workflow_input.workspace_id == "ws1"
        assert workflow_input.user_id == "user_owner"
        assert workflow_input.content == "updated text"

    @patch("src.temporal.shared_services.get_db_service")
    def test_out_of_range_chunk_index_returns_404_without_starting_workflow(
        self, mock_get_db, client: TestClient
    ):
        """#134 follow-up item 8: an out-of-range chunk_index must 404
        immediately -- get_document_status already returned chunk_count for
        free -- instead of burning a TEI embed round-trip (and, pre-#137-fix,
        returning a silently-successful no-op) on a chunk that can't exist."""
        mock_db_svc = MagicMock()
        mock_db_svc.get_document_status = AsyncMock(
            return_value={
                "document_id": "doc1",
                "workspace_id": "ws1",
                "user_id": "user_owner",
                "chunk_count": 5,
            }
        )
        mock_get_db.return_value = mock_db_svc

        resp = client.patch(
            "/chunks/doc1/99?workspace_id=ws1",
            json={"content": "updated text"},
            headers={"X-API-Key": VALID_API_KEY},
        )

        assert resp.status_code == 404
        client._mock_temporal_client.start_workflow.assert_not_called()
