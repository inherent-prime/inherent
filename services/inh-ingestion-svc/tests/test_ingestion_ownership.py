"""Tests for the #175/#177 workspace-ownership hardening.

#175 (security): DELETE /documents/{document_id} trusted caller-supplied
workspace_id/user_id -- the same missing-ownership-check pattern #134 fixed
on PATCH /chunks/{document_id}/{chunk_index}. See test_weaviate_delete.py for
the endpoint-level tests; this module covers the shared ownership helper
directly.

#177 (security): six more inh-ingestion-svc endpoints were gated only by
verify_api_key, with no check that the caller's claimed workspace_id/job_id
pairing was one it was actually entitled to:

- GET  /ingest/{document_id}/status
- GET  /lineage/{document_id}
- GET  /dead-letter               (the sharpest edge -- see below)
- GET  /dead-letter/{job_id}
- POST /dead-letter/{job_id}/retry     (a write)
- POST /dead-letter/{job_id}/abandon   (a write)

GET /dead-letter returned dead-letter rows across EVERY workspace (workspace_id
was an optional filter), and those rows carry genuine (document_id,
workspace_id, user_id) triples. That enabled an escalation chain: read a
genuine cross-tenant pair from GET /dead-letter, then present it to PATCH
/chunks/{document_id}/{chunk_index} -- #134's ownership guard checks
(document_id, workspace_id) CONSISTENCY, which a harvested pair genuinely
satisfies, so it would pass and let an attacker overwrite (and re-embed) a
victim's chunk. This module proves that chain is closed: GET /dead-letter now
requires and enforces workspace_id (never returns a foreign tenant's rows),
and GET /dead-letter/{job_id} independently 404s a job it doesn't own even if
the caller already has the (correct) job_id.

Every route below mirrors #134's fix (`resolve_owned_document` /
`resolve_owned_dead_letter_job` in src/api/ownership.py): resolve the
row against PostgreSQL first, 404 unless its stored workspace_id matches the
caller's claim (same response for missing vs. foreign-workspace, so
existence doesn't leak), and -- the "lookup-failure-denies" tests below --
never treat a DB lookup failure as "allowed".

POST-#177-REVIEW HARDENING (empty-string bypass): an adversarial review
proved the FIRST version of this fix was itself bypassable --
`GET /dead-letter?workspace_id=` (query param PRESENT but EMPTY) returned
200 with no workspace filter applied, because `Query(...)` only enforces
presence, not non-emptiness, and `DatabaseService.get_dead_letter_jobs`
guarded its WHERE clause with a bare `if workspace_id:` (falsy for `""`).
`TestEmptyWorkspaceIdBypassClosed` below reproduces that exact bypass
end-to-end and pins it closed at all three layers: `require_workspace_id`
(the shared boundary check), the route's own `min_length=1` Query
constraint, and `DatabaseService.get_dead_letter_jobs` itself (now REQUIRES
workspace_id and raises rather than silently widening).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from src.api.ownership import (
    require_storage_path_workspace_prefix,
    require_workspace_id,
    resolve_owned_dead_letter_job,
    resolve_owned_document,
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
    """Override -- these tests mock DatabaseService directly."""
    yield None


# ---------------------------------------------------------------------------
# Unit tests: src/api/ownership.py helpers, in isolation
# ---------------------------------------------------------------------------


class TestRequireWorkspaceId:
    """require_workspace_id -- the boundary check closing the empty-string
    bypass an adversarial review found in the first version of this fix.

    A blank or whitespace-only workspace_id must NEVER reach a DB call that
    might (as get_dead_letter_jobs's old `if workspace_id:` guard did) treat
    "no value" as "no filter" -- these tests pin that at the source.
    """

    def test_valid_value_passes_through_unchanged(self):
        assert require_workspace_id("ws1") == "ws1"

    def test_value_is_stripped(self):
        assert require_workspace_id("  ws1  ") == "ws1"

    def test_empty_string_rejected(self):
        """The exact bypass payload: `?workspace_id=` decodes to `""`."""
        with pytest.raises(HTTPException) as exc_info:
            require_workspace_id("")
        assert exc_info.value.status_code == 422

    def test_whitespace_only_rejected(self):
        """`?workspace_id=%20` decodes to `" "` -- length 1, so a bare
        `min_length=1` Query constraint alone would NOT catch this; the
        strip-then-check here must."""
        with pytest.raises(HTTPException) as exc_info:
            require_workspace_id("   ")
        assert exc_info.value.status_code == 422

    def test_tab_and_newline_only_rejected(self):
        with pytest.raises(HTTPException) as exc_info:
            require_workspace_id("\t\n")
        assert exc_info.value.status_code == 422


class TestResolveOwnedDocument:
    """resolve_owned_document -- owner-allowed / non-owner-denied /
    lookup-failure-denies, decoupled from any specific route."""

    async def test_owner_allowed(self):
        mock_db = MagicMock()
        mock_db.get_document_status = AsyncMock(
            return_value={"document_id": "doc1", "workspace_id": "ws1", "user_id": "u1"}
        )
        document = await resolve_owned_document(mock_db, "doc1", "ws1")
        assert document["workspace_id"] == "ws1"

    async def test_non_owner_denied(self):
        """Document exists but is owned by a DIFFERENT workspace -- 404, not
        a leak of the fact that the document exists at all."""
        mock_db = MagicMock()
        mock_db.get_document_status = AsyncMock(
            return_value={"document_id": "doc1", "workspace_id": "ws_owner", "user_id": "u1"}
        )
        with pytest.raises(HTTPException) as exc_info:
            await resolve_owned_document(mock_db, "doc1", "ws_attacker")
        assert exc_info.value.status_code == 404

    async def test_missing_document_denied_with_same_404(self):
        """No such document -- same 404 shape as the non-owner case (no
        distinguishable response that would leak existence)."""
        mock_db = MagicMock()
        mock_db.get_document_status = AsyncMock(return_value=None)
        with pytest.raises(HTTPException) as exc_info:
            await resolve_owned_document(mock_db, "doc_missing", "ws1")
        assert exc_info.value.status_code == 404

    async def test_lookup_failure_denies_not_allows(self):
        """A DB failure during the ownership lookup must propagate, not be
        swallowed into an implicit 'allow'. No try/except in
        resolve_owned_document is the point -- this test pins that."""
        mock_db = MagicMock()
        mock_db.get_document_status = AsyncMock(side_effect=RuntimeError("DB unavailable"))
        with pytest.raises(RuntimeError, match="DB unavailable"):
            await resolve_owned_document(mock_db, "doc1", "ws1")

    async def test_empty_workspace_id_rejected_before_any_db_call(self):
        """The blank-workspace_id boundary check must fire BEFORE the DB is
        ever touched -- proven here by asserting get_document_status is
        never called."""
        mock_db = MagicMock()
        mock_db.get_document_status = AsyncMock(
            return_value={"document_id": "doc1", "workspace_id": "ws1", "user_id": "u1"}
        )
        with pytest.raises(HTTPException) as exc_info:
            await resolve_owned_document(mock_db, "doc1", "")
        assert exc_info.value.status_code == 422
        mock_db.get_document_status.assert_not_called()


class TestResolveOwnedDeadLetterJob:
    """resolve_owned_dead_letter_job -- same matrix, for dead_letter_jobs."""

    async def test_owner_allowed(self):
        mock_db = MagicMock()
        mock_db.get_dead_letter_job = AsyncMock(
            return_value={"id": 1, "workspace_id": "ws1", "status": "pending"}
        )
        job = await resolve_owned_dead_letter_job(mock_db, 1, "ws1")
        assert job["workspace_id"] == "ws1"

    async def test_non_owner_denied(self):
        mock_db = MagicMock()
        mock_db.get_dead_letter_job = AsyncMock(
            return_value={"id": 1, "workspace_id": "ws_victim", "status": "pending"}
        )
        with pytest.raises(HTTPException) as exc_info:
            await resolve_owned_dead_letter_job(mock_db, 1, "ws_attacker")
        assert exc_info.value.status_code == 404

    async def test_missing_job_denied_with_same_404(self):
        mock_db = MagicMock()
        mock_db.get_dead_letter_job = AsyncMock(return_value=None)
        with pytest.raises(HTTPException) as exc_info:
            await resolve_owned_dead_letter_job(mock_db, 999, "ws1")
        assert exc_info.value.status_code == 404

    async def test_lookup_failure_denies_not_allows(self):
        mock_db = MagicMock()
        mock_db.get_dead_letter_job = AsyncMock(side_effect=RuntimeError("DB unavailable"))
        with pytest.raises(RuntimeError, match="DB unavailable"):
            await resolve_owned_dead_letter_job(mock_db, 1, "ws1")

    async def test_empty_workspace_id_rejected_before_any_db_call(self):
        mock_db = MagicMock()
        mock_db.get_dead_letter_job = AsyncMock(
            return_value={"id": 1, "workspace_id": "ws1", "status": "pending"}
        )
        with pytest.raises(HTTPException) as exc_info:
            await resolve_owned_dead_letter_job(mock_db, 1, "   ")
        assert exc_info.value.status_code == 422
        mock_db.get_dead_letter_job.assert_not_called()


class TestRequireStoragePathWorkspacePrefix:
    """require_storage_path_workspace_prefix -- the #210 fix.

    POST /ingest has no PostgreSQL row to resolve ownership against (it
    CREATES the row), so unlike the two resolve_owned_* classes above this
    checks a pure string invariant: storage_path's first path segment must
    be workspace_id. Both layout conventions live in this codebase
    (workspaces/{id}/... and {id}/...) and both must be accepted; a
    mismatched, blank, or traversal-disguised path must be denied.
    """

    def test_workspaces_prefix_convention_allowed(self):
        """The historical intg-svc/GCS layout: workspaces/{workspace_id}/...
        (inh_contracts.events.DocumentUploadMessage's own example)."""
        result = require_storage_path_workspace_prefix("workspaces/ws1/1234-document.pdf", "ws1")
        assert result == "ws1"

    def test_bare_workspace_id_prefix_convention_allowed(self):
        """inh-public-api-svc's current StorageService.generate_key layout:
        {workspace_id}/{uuid}/{filename}, no 'workspaces/' literal."""
        result = require_storage_path_workspace_prefix("ws1/550e8400-e29b/document.pdf", "ws1")
        assert result == "ws1"

    def test_mismatched_workspace_prefix_denied(self):
        """The core #210 exploit payload: attacker's own workspace_id paired
        with a victim's genuine storage_path."""
        with pytest.raises(HTTPException) as exc_info:
            require_storage_path_workspace_prefix("workspaces/ws_victim/secret.pdf", "ws_attacker")
        assert exc_info.value.status_code == 403

    def test_mismatched_bare_prefix_denied(self):
        with pytest.raises(HTTPException) as exc_info:
            require_storage_path_workspace_prefix("ws_victim/uuid/secret.pdf", "ws_attacker")
        assert exc_info.value.status_code == 403

    def test_path_with_no_workspace_segment_at_all_denied(self):
        """A storage_path that isn't workspace-prefixed at all (e.g. a
        legacy/malformed value) must be denied, not treated as a wildcard
        match -- there is no valid workspace_id it could equal by omission."""
        with pytest.raises(HTTPException) as exc_info:
            require_storage_path_workspace_prefix("document.pdf", "ws1")
        assert exc_info.value.status_code == 403

    def test_empty_storage_path_denied(self):
        with pytest.raises(HTTPException) as exc_info:
            require_storage_path_workspace_prefix("", "ws1")
        assert exc_info.value.status_code == 403

    def test_traversal_disguised_prefix_is_normalized_before_comparison(self):
        """The nominal first segment is the CLAIMED workspace, but '..'
        components make the path actually resolve into a DIFFERENT
        workspace's subtree. posixpath.normpath must collapse this BEFORE
        the prefix is read off, so the mismatch is still caught rather than
        a traversal trick passing the check on the nominal segment alone."""
        with pytest.raises(HTTPException) as exc_info:
            require_storage_path_workspace_prefix(
                "workspaces/ws_attacker/../ws_victim/secret.pdf", "ws_attacker"
            )
        assert exc_info.value.status_code == 403

    def test_traversal_that_genuinely_resolves_into_claimed_workspace_allowed(self):
        """The flip side of the traversal test above: '..' components that
        collapse back into the CLAIMED workspace's own subtree are fine --
        this function only cares about the final, normalized prefix."""
        result = require_storage_path_workspace_prefix("workspaces/ws1/sub/../document.pdf", "ws1")
        assert result == "ws1"

    def test_blank_workspace_id_rejected_before_path_is_even_read(self):
        """Same boundary-first posture as the two resolve_owned_* helpers:
        a blank workspace_id claim is invalid regardless of what
        storage_path says, and must 422 (not 403) to distinguish 'malformed
        request' from 'mismatched but well-formed request'."""
        with pytest.raises(HTTPException) as exc_info:
            require_storage_path_workspace_prefix("workspaces/ws1/document.pdf", "   ")
        assert exc_info.value.status_code == 422

    def test_leading_slash_and_workspaces_prefix_both_tolerated(self):
        """A caller-supplied leading '/' (e.g. '/workspaces/ws1/doc.pdf')
        must not defeat the prefix match."""
        result = require_storage_path_workspace_prefix("/workspaces/ws1/document.pdf", "ws1")
        assert result == "ws1"


# ---------------------------------------------------------------------------
# Route-level tests: shared TestClient fixture
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
    mock_handle.query = AsyncMock(
        return_value={"step": "chunking_text", "progress": 55, "chunks_created": 3}
    )
    mock_temporal_client.get_workflow_handle = MagicMock(return_value=mock_handle)

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


_OWNED_DOC = {
    "document_id": "doc1",
    "workspace_id": "ws_owner",
    "user_id": "user_owner",
    "chunk_count": 3,
}
_OWNED_JOB = {
    "id": 1,
    "workspace_id": "ws_owner",
    "user_id": "user_owner",
    "document_id": "doc1",
    "status": "pending",
    "original_message": {"document_id": "doc1"},
}


# ---------------------------------------------------------------------------
# GET /ingest/{document_id}/status
# ---------------------------------------------------------------------------


class TestIngestionStatusOwnership:
    def test_owner_allowed(self, client: TestClient):
        mock_db = MagicMock()
        mock_db.get_document_status = AsyncMock(return_value=_OWNED_DOC)
        with patch("src.temporal.shared_services.get_db_service", return_value=mock_db):
            resp = client.get(
                "/ingest/doc1/status?workspace_id=ws_owner",
                headers={"X-API-Key": VALID_API_KEY},
            )
        assert resp.status_code == 200

    def test_non_owner_denied(self, client: TestClient):
        mock_db = MagicMock()
        mock_db.get_document_status = AsyncMock(return_value=_OWNED_DOC)
        with patch("src.temporal.shared_services.get_db_service", return_value=mock_db):
            resp = client.get(
                "/ingest/doc1/status?workspace_id=ws_attacker",
                headers={"X-API-Key": VALID_API_KEY},
            )
        assert resp.status_code == 404
        client._mock_temporal_client.get_workflow_handle.assert_not_called()

    def test_missing_workspace_id_returns_422(self, client: TestClient):
        resp = client.get("/ingest/doc1/status", headers={"X-API-Key": VALID_API_KEY})
        assert resp.status_code == 422

    def test_lookup_failure_denies_not_allows(self, client: TestClient):
        mock_db = MagicMock()
        mock_db.get_document_status = AsyncMock(side_effect=RuntimeError("DB unavailable"))
        with patch("src.temporal.shared_services.get_db_service", return_value=mock_db):
            with pytest.raises(RuntimeError, match="DB unavailable"):
                client.get(
                    "/ingest/doc1/status?workspace_id=ws_owner",
                    headers={"X-API-Key": VALID_API_KEY},
                )
        client._mock_temporal_client.get_workflow_handle.assert_not_called()


# ---------------------------------------------------------------------------
# GET /lineage/{document_id}
# ---------------------------------------------------------------------------


class TestLineageOwnership:
    def test_owner_allowed(self, client: TestClient):
        mock_db = MagicMock()
        mock_db.get_document_status = AsyncMock(return_value=_OWNED_DOC)
        mock_db.get_ingestion_events = AsyncMock(return_value=[])
        with patch("src.temporal.shared_services.get_db_service", return_value=mock_db):
            resp = client.get(
                "/lineage/doc1?workspace_id=ws_owner",
                headers={"X-API-Key": VALID_API_KEY},
            )
        assert resp.status_code == 200
        assert resp.json() == {"document_id": "doc1", "events": []}

    def test_non_owner_denied(self, client: TestClient):
        mock_db = MagicMock()
        mock_db.get_document_status = AsyncMock(return_value=_OWNED_DOC)
        mock_db.get_ingestion_events = AsyncMock(return_value=[{"leaked": "event"}])
        with patch("src.temporal.shared_services.get_db_service", return_value=mock_db):
            resp = client.get(
                "/lineage/doc1?workspace_id=ws_attacker",
                headers={"X-API-Key": VALID_API_KEY},
            )
        assert resp.status_code == 404
        mock_db.get_ingestion_events.assert_not_called()

    def test_missing_workspace_id_returns_422(self, client: TestClient):
        resp = client.get("/lineage/doc1", headers={"X-API-Key": VALID_API_KEY})
        assert resp.status_code == 422

    def test_lookup_failure_denies_not_allows(self, client: TestClient):
        mock_db = MagicMock()
        mock_db.get_document_status = AsyncMock(side_effect=RuntimeError("DB unavailable"))
        mock_db.get_ingestion_events = AsyncMock(return_value=[{"leaked": "event"}])
        with patch("src.temporal.shared_services.get_db_service", return_value=mock_db):
            with pytest.raises(RuntimeError, match="DB unavailable"):
                client.get(
                    "/lineage/doc1?workspace_id=ws_owner",
                    headers={"X-API-Key": VALID_API_KEY},
                )
        mock_db.get_ingestion_events.assert_not_called()


# ---------------------------------------------------------------------------
# GET /dead-letter/{job_id}, POST retry, POST abandon
# ---------------------------------------------------------------------------


class TestDeadLetterJobOwnership:
    def test_get_owner_allowed(self, client: TestClient):
        mock_db = MagicMock()
        mock_db.get_dead_letter_job = AsyncMock(return_value=_OWNED_JOB)
        with patch("src.temporal.shared_services.get_db_service", return_value=mock_db):
            resp = client.get(
                "/dead-letter/1?workspace_id=ws_owner",
                headers={"X-API-Key": VALID_API_KEY},
            )
        assert resp.status_code == 200
        assert resp.json()["id"] == 1

    def test_get_non_owner_denied(self, client: TestClient):
        mock_db = MagicMock()
        mock_db.get_dead_letter_job = AsyncMock(return_value=_OWNED_JOB)
        with patch("src.temporal.shared_services.get_db_service", return_value=mock_db):
            resp = client.get(
                "/dead-letter/1?workspace_id=ws_attacker",
                headers={"X-API-Key": VALID_API_KEY},
            )
        assert resp.status_code == 404

    def test_get_missing_workspace_id_returns_422(self, client: TestClient):
        resp = client.get("/dead-letter/1", headers={"X-API-Key": VALID_API_KEY})
        assert resp.status_code == 422

    def test_get_lookup_failure_denies_not_allows(self, client: TestClient):
        mock_db = MagicMock()
        mock_db.get_dead_letter_job = AsyncMock(side_effect=RuntimeError("DB unavailable"))
        with patch("src.temporal.shared_services.get_db_service", return_value=mock_db):
            with pytest.raises(RuntimeError, match="DB unavailable"):
                client.get(
                    "/dead-letter/1?workspace_id=ws_owner",
                    headers={"X-API-Key": VALID_API_KEY},
                )

    def test_retry_owner_allowed(self, client: TestClient):
        fake_trigger = AsyncMock()
        fake_trigger.trigger_workflow_async = AsyncMock(return_value="ingest-doc1")
        mock_db = MagicMock()
        mock_db.get_dead_letter_job = AsyncMock(return_value=dict(_OWNED_JOB))
        mock_db.increment_dead_letter_retry = AsyncMock()
        with patch("src.temporal.shared_services.get_db_service", return_value=mock_db):
            # app.state.trigger is set at startup; swap it directly on the
            # running app instance so this test asserts purely on the
            # route's own ownership check, not on Temporal/DB plumbing.
            client.app.state.trigger = fake_trigger
            resp = client.post(
                "/dead-letter/1/retry?workspace_id=ws_owner",
                headers={"X-API-Key": VALID_API_KEY},
            )
        assert resp.status_code == 200
        fake_trigger.trigger_workflow_async.assert_awaited_once()

    def test_retry_non_owner_denied_and_never_retries(self, client: TestClient):
        fake_trigger = AsyncMock()
        fake_trigger.trigger_workflow_async = AsyncMock(return_value="ingest-doc1")
        mock_db = MagicMock()
        mock_db.get_dead_letter_job = AsyncMock(return_value=dict(_OWNED_JOB))
        mock_db.increment_dead_letter_retry = AsyncMock()
        with patch("src.temporal.shared_services.get_db_service", return_value=mock_db):
            client.app.state.trigger = fake_trigger
            resp = client.post(
                "/dead-letter/1/retry?workspace_id=ws_attacker",
                headers={"X-API-Key": VALID_API_KEY},
            )
        assert resp.status_code == 404
        fake_trigger.trigger_workflow_async.assert_not_awaited()
        mock_db.increment_dead_letter_retry.assert_not_called()

    def test_retry_missing_workspace_id_returns_422(self, client: TestClient):
        resp = client.post("/dead-letter/1/retry", headers={"X-API-Key": VALID_API_KEY})
        assert resp.status_code == 422

    def test_retry_lookup_failure_denies_not_allows(self, client: TestClient):
        fake_trigger = AsyncMock()
        mock_db = MagicMock()
        mock_db.get_dead_letter_job = AsyncMock(side_effect=RuntimeError("DB unavailable"))
        with patch("src.temporal.shared_services.get_db_service", return_value=mock_db):
            client.app.state.trigger = fake_trigger
            with pytest.raises(RuntimeError, match="DB unavailable"):
                client.post(
                    "/dead-letter/1/retry?workspace_id=ws_owner",
                    headers={"X-API-Key": VALID_API_KEY},
                )
        fake_trigger.trigger_workflow_async.assert_not_awaited()

    def test_abandon_owner_allowed(self, client: TestClient):
        mock_db = MagicMock()
        mock_db.get_dead_letter_job = AsyncMock(return_value=dict(_OWNED_JOB))
        mock_db.update_dead_letter_status = AsyncMock(return_value=True)
        with patch("src.temporal.shared_services.get_db_service", return_value=mock_db):
            resp = client.post(
                "/dead-letter/1/abandon?workspace_id=ws_owner",
                headers={"X-API-Key": VALID_API_KEY},
            )
        assert resp.status_code == 200
        mock_db.update_dead_letter_status.assert_awaited_once_with(1, "abandoned")

    def test_abandon_non_owner_denied_and_never_mutates(self, client: TestClient):
        mock_db = MagicMock()
        mock_db.get_dead_letter_job = AsyncMock(return_value=dict(_OWNED_JOB))
        mock_db.update_dead_letter_status = AsyncMock(return_value=True)
        with patch("src.temporal.shared_services.get_db_service", return_value=mock_db):
            resp = client.post(
                "/dead-letter/1/abandon?workspace_id=ws_attacker",
                headers={"X-API-Key": VALID_API_KEY},
            )
        assert resp.status_code == 404
        mock_db.update_dead_letter_status.assert_not_called()

    def test_abandon_missing_workspace_id_returns_422(self, client: TestClient):
        resp = client.post("/dead-letter/1/abandon", headers={"X-API-Key": VALID_API_KEY})
        assert resp.status_code == 422

    def test_abandon_lookup_failure_denies_not_allows(self, client: TestClient):
        mock_db = MagicMock()
        mock_db.get_dead_letter_job = AsyncMock(side_effect=RuntimeError("DB unavailable"))
        mock_db.update_dead_letter_status = AsyncMock(return_value=True)
        with patch("src.temporal.shared_services.get_db_service", return_value=mock_db):
            with pytest.raises(RuntimeError, match="DB unavailable"):
                client.post(
                    "/dead-letter/1/abandon?workspace_id=ws_owner",
                    headers={"X-API-Key": VALID_API_KEY},
                )
        mock_db.update_dead_letter_status.assert_not_called()


# ---------------------------------------------------------------------------
# The #177 escalation chain: GET /dead-letter -> PATCH /chunks
# ---------------------------------------------------------------------------


class TestDeadLetterEscalationChainClosed:
    """Reproduces the exact chain #177 describes and proves each of its two
    steps is now independently blocked:

    1. An attacker holding only INGESTION_API_KEY calls GET /dead-letter to
       harvest a genuine (document_id, workspace_id) pair belonging to a
       victim tenant. Before this fix, GET /dead-letter had no workspace_id
       enforcement -- any workspace's rows were visible. It's now required
       and always enforced as a DB filter, so this harvest returns nothing
       for the attacker's own claimed workspace.
    2. Even granting the attacker somehow already knows the victim's
       (document_id, workspace_id) pair (e.g. leaked another way), reading
       the SAME dead-letter job directly by id now 404s unless the caller's
       claimed workspace_id actually owns it -- closing the single-job read
       path independently of the list path.

    #134's own guard (PATCH /chunks checking document_id<->workspace_id
    CONSISTENCY) is intentionally unchanged and untested here -- it was
    already proven in test_chunk_edit_weaviate.py, and the point of this
    fix is that a genuine pair can no longer be HARVESTED via dead-letter
    routes in the first place, not that the consistency check itself needed
    to change.
    """

    def test_list_route_passes_callers_own_workspace_id_to_the_db_call(self, client: TestClient):
        """Route-plumbing check ONLY: GET /dead-letter?workspace_id=ws_attacker
        calls DatabaseService.get_dead_letter_jobs with EXACTLY the caller's
        own claimed workspace_id, never anything else (e.g. never omits it,
        never substitutes a different value).

        This does NOT prove the DB layer's own filtering actually excludes a
        victim's rows -- get_dead_letter_jobs is mocked here, so this test
        would pass even if database.py's WHERE clause were deleted entirely.
        That distinct claim -- "the real query genuinely filters" -- is
        proven separately by
        TestGetDeadLetterJobsRealFiltering.test_real_query_execution_excludes_foreign_workspace_rows,
        which calls the ACTUAL production method against a real (in-memory)
        database. A prior version of this test's docstring claimed
        test_dead_letter.py covered that gap; it did not (that file also
        only ever mocks get_dead_letter_jobs) -- see docs/developer/learnings.md
        #110's lesson: a citation to "existing infrastructure already covers
        this" is a claim, not a check.
        """
        mock_db = MagicMock()
        mock_db.get_dead_letter_jobs = AsyncMock(return_value=[])
        with patch("src.temporal.shared_services.get_db_service", return_value=mock_db):
            resp = client.get(
                "/dead-letter?workspace_id=ws_attacker",
                headers={"X-API-Key": VALID_API_KEY},
            )
        assert resp.status_code == 200
        mock_db.get_dead_letter_jobs.assert_awaited_once_with(
            workspace_id="ws_attacker", status="pending", limit=50
        )

    def test_list_without_workspace_id_is_rejected_outright(self, client: TestClient):
        """The pre-fix escalation's first step required NO workspace_id at
        all. That request shape must now fail validation before it ever
        reaches the database."""
        resp = client.get("/dead-letter", headers={"X-API-Key": VALID_API_KEY})
        assert resp.status_code == 422

    def test_single_job_read_blocks_the_harvested_pair(self, client: TestClient):
        """Step 2: even with a genuine victim (document_id, workspace_id)
        pair somehow in hand, reading the dead-letter job directly by id
        under the attacker's claimed workspace_id 404s."""
        victim_job = {
            "id": 42,
            "workspace_id": "ws_victim",
            "user_id": "user_victim",
            "document_id": "doc_victim",
            "status": "pending",
            "original_message": {"document_id": "doc_victim"},
        }
        mock_db = MagicMock()
        mock_db.get_dead_letter_job = AsyncMock(return_value=victim_job)
        with patch("src.temporal.shared_services.get_db_service", return_value=mock_db):
            resp = client.get(
                "/dead-letter/42?workspace_id=ws_attacker",
                headers={"X-API-Key": VALID_API_KEY},
            )
        assert resp.status_code == 404

    def test_end_to_end_route_wiring_never_asks_for_another_workspace(self, client: TestClient):
        """Route-plumbing check ONLY (see the previous test's docstring for
        why this is a narrower claim than "the chain is broken"): across the
        full request path (auth -> boundary validation -> DB call), the
        route never asks the DB for anything but the attacker's own claimed
        workspace_id. The DB call is mocked, so -- again -- this does not by
        itself prove foreign rows are excluded; that is
        TestGetDeadLetterJobsRealFiltering's job."""
        mock_db = MagicMock()
        mock_db.get_dead_letter_jobs = AsyncMock(return_value=[])
        with patch("src.temporal.shared_services.get_db_service", return_value=mock_db):
            list_resp = client.get(
                "/dead-letter?workspace_id=ws_attacker",
                headers={"X-API-Key": VALID_API_KEY},
            )
        assert list_resp.status_code == 200
        mock_db.get_dead_letter_jobs.assert_awaited_once_with(
            workspace_id="ws_attacker", status="pending", limit=50
        )


class TestGetDeadLetterJobsRealFiltering:
    """Proves DatabaseService.get_dead_letter_jobs's ACTUAL filtering logic
    excludes another workspace's rows -- not a mock's own configured return
    value, which is what every other test in this module (and
    test_dead_letter.py) exercises instead, since none of them can reach a
    real PostgreSQL in this sandbox.

    Post-#177-review finding: mocking `get_dead_letter_jobs` itself is
    EXACTLY what let the empty-string bypass ship unnoticed -- every test
    stubbed the method, so nothing anywhere ever executed database.py's own
    `if workspace_id:` guard or its WHERE clause. These tests call the real,
    unmocked method against a real (in-memory SQLite) table so the WHERE
    clause genuinely runs. SQLite stands in for PostgreSQL here ONLY for
    this table's DDL (a generic `JSON` column type replaces the postgres-only
    `JSONB` this repo's real schema uses, since SQLite has no JSONB dialect
    type) -- the WHERE-clause construction under test
    (`self.dead_letter_jobs.c.workspace_id == workspace_id`) is
    dialect-agnostic SQLAlchemy Core and behaves identically on both engines.
    """

    def _make_sqlite_backed_db_service(self):
        """Build a DatabaseService.__new__ instance wired to a real
        in-memory SQLite engine, with `dead_letter_jobs` swapped for a
        schema-compatible (same column NAMES the production code
        references) stand-in table. Returns (db, engine, table)."""
        import sqlalchemy as sa

        from src.services.database import DatabaseService

        metadata = sa.MetaData()
        dead_letter_jobs = sa.Table(
            "dead_letter_jobs",
            metadata,
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("document_id", sa.String, nullable=False),
            sa.Column("workspace_id", sa.String, nullable=False),
            sa.Column("user_id", sa.String, nullable=False),
            sa.Column("workflow_run_id", sa.String, nullable=True),
            sa.Column("original_message", sa.JSON, nullable=False),
            sa.Column("error_message", sa.Text, nullable=False),
            sa.Column("error_type", sa.String, nullable=False),
            sa.Column("retry_count", sa.Integer, nullable=False, default=0),
            sa.Column("status", sa.String, nullable=False, default="pending"),
            sa.Column("created_at", sa.DateTime, nullable=False),
            sa.Column("updated_at", sa.DateTime, nullable=False),
            sa.Column("resolved_at", sa.DateTime, nullable=True),
        )
        engine = sa.create_engine("sqlite:///:memory:")
        metadata.create_all(engine)

        db = DatabaseService.__new__(DatabaseService)
        db.engine = engine
        db.dead_letter_jobs = dead_letter_jobs

        from contextlib import contextmanager

        @contextmanager
        def _get_session():
            with sa.orm.Session(engine) as session:
                yield session
                session.commit()

        db.get_session = _get_session
        return db, engine, dead_letter_jobs

    def _seed(self, engine, table, **overrides):
        from datetime import UTC, datetime

        row = {
            "document_id": "doc",
            "workspace_id": "ws",
            "user_id": "user",
            "workflow_run_id": None,
            "original_message": {},
            "error_message": "e",
            "error_type": "TimeoutError",
            "retry_count": 0,
            "status": "pending",
            "created_at": datetime.now(UTC),
            "updated_at": datetime.now(UTC),
            "resolved_at": None,
        }
        row.update(overrides)
        with engine.begin() as conn:
            conn.execute(table.insert(), [row])

    async def test_real_query_execution_excludes_foreign_workspace_rows(self):
        """Two dead-letter jobs exist in the SAME table, one per workspace.
        Calling the real get_dead_letter_jobs for ws_attacker must return
        ONLY ws_attacker's row -- proof the WHERE clause genuinely executes
        and filters, not just that some mock was asked nicely."""
        db, engine, table = self._make_sqlite_backed_db_service()
        self._seed(
            engine, table, document_id="doc_victim", workspace_id="ws_victim", user_id="u_victim"
        )
        self._seed(
            engine,
            table,
            document_id="doc_attacker_own",
            workspace_id="ws_attacker",
            user_id="u_attacker",
        )

        rows = await db.get_dead_letter_jobs(workspace_id="ws_attacker", status=None, limit=50)

        assert len(rows) == 1
        assert rows[0]["workspace_id"] == "ws_attacker"
        assert rows[0]["document_id"] == "doc_attacker_own"
        # The victim's row must never appear, however the result is sliced.
        assert all(r["document_id"] != "doc_victim" for r in rows)
        assert all(r["workspace_id"] != "ws_victim" for r in rows)

    async def test_blank_workspace_id_never_reaches_the_query_and_returns_no_rows(self):
        """The blank-input side of the same guarantee: with the SAME two
        cross-tenant rows seeded, `workspace_id=""` and `workspace_id="  "`
        must raise (never silently execute a query that would return both
        tenants' rows). This is the exact input shape of the original
        bypass finding, run against a real, populated database -- not just
        asserted in isolation against `require_workspace_id`."""
        db, engine, table = self._make_sqlite_backed_db_service()
        self._seed(engine, table, document_id="doc_victim", workspace_id="ws_victim")
        self._seed(engine, table, document_id="doc_attacker_own", workspace_id="ws_attacker")

        for blank in ("", "   "):
            with pytest.raises(ValueError, match="non-blank workspace_id"):
                await db.get_dead_letter_jobs(workspace_id=blank, status=None, limit=50)

        # Sanity: the rows really are both there (the ValueError is from the
        # guard, not from an empty table masking the real risk).
        with_valid_scope = await db.get_dead_letter_jobs(
            workspace_id="ws_victim", status=None, limit=50
        )
        assert len(with_valid_scope) == 1
        assert with_valid_scope[0]["document_id"] == "doc_victim"


class TestEmptyWorkspaceIdBypassClosed:
    """Reproduces the exact bypass an adversarial review found in the FIRST
    version of the #177 fix, end to end.

    `GET /dead-letter?workspace_id=` -- the query param PRESENT but set to
    the EMPTY STRING -- passed the original `Query(...)` validation (which
    only enforces presence) and then hit `get_dead_letter_jobs`'s old
    `if workspace_id:` guard, falsy for `""`, which silently skipped the
    WHERE clause entirely and returned every workspace's dead-letter rows.
    This class asserts the fixed behavior at both the route and the mocked
    DB call, and (in the last test) via a REAL DatabaseService method call
    against an in-memory-mocked engine, so the assertion isn't just "the
    mock was asked correctly" but "the query-building code itself refuses a
    blank scope".
    """

    def test_empty_workspace_id_query_param_rejected(self, client: TestClient):
        """The literal payload from the finding: `?workspace_id=`."""
        mock_db = MagicMock()
        # If this mock is EVER called, the bypass has reopened -- the
        # boundary check must reject the request before reaching here.
        mock_db.get_dead_letter_jobs = AsyncMock(
            return_value=[{"id": 1, "workspace_id": "ws_victim"}]
        )
        with patch("src.temporal.shared_services.get_db_service", return_value=mock_db):
            resp = client.get(
                "/dead-letter?workspace_id=",
                headers={"X-API-Key": VALID_API_KEY},
            )
        assert resp.status_code == 422
        mock_db.get_dead_letter_jobs.assert_not_called()

    def test_whitespace_workspace_id_query_param_rejected(self, client: TestClient):
        """`?workspace_id=%20` (a single space) has length 1, so a bare
        `min_length=1` Query constraint alone would NOT catch this --
        require_workspace_id's strip-then-check must."""
        mock_db = MagicMock()
        mock_db.get_dead_letter_jobs = AsyncMock(
            return_value=[{"id": 1, "workspace_id": "ws_victim"}]
        )
        with patch("src.temporal.shared_services.get_db_service", return_value=mock_db):
            resp = client.get(
                "/dead-letter?workspace_id=%20",
                headers={"X-API-Key": VALID_API_KEY},
            )
        assert resp.status_code == 422
        mock_db.get_dead_letter_jobs.assert_not_called()

    def test_single_job_routes_reject_empty_workspace_id_too(self, client: TestClient):
        """The same payload against the single-job dead-letter routes
        (#1's instruction: apply the boundary check everywhere workspace_id
        was made required, not just the list endpoint)."""
        mock_db = MagicMock()
        mock_db.get_dead_letter_job = AsyncMock(
            return_value={"id": 1, "workspace_id": "ws_victim", "status": "pending"}
        )
        with patch("src.temporal.shared_services.get_db_service", return_value=mock_db):
            get_resp = client.get(
                "/dead-letter/1?workspace_id=", headers={"X-API-Key": VALID_API_KEY}
            )
            retry_resp = client.post(
                "/dead-letter/1/retry?workspace_id=", headers={"X-API-Key": VALID_API_KEY}
            )
            abandon_resp = client.post(
                "/dead-letter/1/abandon?workspace_id=", headers={"X-API-Key": VALID_API_KEY}
            )
        assert get_resp.status_code == 422
        assert retry_resp.status_code == 422
        assert abandon_resp.status_code == 422
        mock_db.get_dead_letter_job.assert_not_called()

    def test_document_routes_reject_empty_workspace_id_too(self, client: TestClient):
        """Same payload against the document-scoped routes."""
        mock_db = MagicMock()
        mock_db.get_document_status = AsyncMock(return_value=_OWNED_DOC)
        with patch("src.temporal.shared_services.get_db_service", return_value=mock_db):
            status_resp = client.get(
                "/ingest/doc1/status?workspace_id=", headers={"X-API-Key": VALID_API_KEY}
            )
            lineage_resp = client.get(
                "/lineage/doc1?workspace_id=", headers={"X-API-Key": VALID_API_KEY}
            )
            delete_resp = client.delete(
                "/documents/doc1?workspace_id=&user_id=u1", headers={"X-API-Key": VALID_API_KEY}
            )
        assert status_resp.status_code == 422
        assert lineage_resp.status_code == 422
        assert delete_resp.status_code == 422
        mock_db.get_document_status.assert_not_called()

    async def test_db_layer_raises_on_blank_workspace_id_rather_than_widening(self):
        """DatabaseService.get_dead_letter_jobs itself -- not just the route
        -- must refuse a blank workspace_id (#2 in the review: the DB layer
        is what stops the NEXT caller reintroducing this bug). This calls
        the real method (engine mocked as present so the blank-check is
        what's under test, not the "not connected" guard)."""
        from src.services.database import DatabaseService

        db = DatabaseService.__new__(DatabaseService)
        db.engine = MagicMock()  # truthy, so we reach the workspace_id check

        for blank in ("", "   ", None):
            with pytest.raises(ValueError, match="non-blank workspace_id"):
                await db.get_dead_letter_jobs(workspace_id=blank)  # type: ignore[arg-type]
