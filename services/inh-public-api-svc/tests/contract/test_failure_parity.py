"""REST ↔ MCP failure-path parity contract (#98/#99).

Both surfaces expose the same write capabilities (upload, refresh, delete),
and a dependency failure (MQ down, vector store down, DB degraded) must leave
the SAME document state and surface an error on either surface. Issues #98
and #99 exist because nothing enforced this: the REST refresh handler marked
the document failed on MQ outage while its MCP twin silently stranded it as
"pending".

This suite pins the cross-surface contract. The REST halves live in
tests/unit/test_upload_document.py (``test_mq_failure_marks_document_failed``)
and tests/unit/test_refresh_and_verify_endpoints.py
(``test_refresh_marks_failed_on_publish_error``); this file covers the MCP
halves and the not-yet-implemented recovery contracts, marked ``xfail`` with
the issue that tracks them. When a fix lands (e.g. PR #96 for #98), the test
starts XPASSing — remove the marker to lock the behavior in.

Rule of thumb (see CLAUDE.md): a state mutation followed by a publish needs a
compensating mark-failed path on EVERY surface that runs it.
"""

from __future__ import annotations

import io
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from mcp.types import CallToolRequest, CallToolRequestParams

from src.main import create_app
from src.mcp_server import server as mcp_server
from src.models.api_key import APIKeyInfo
from src.models.document import Document
from src.services.auth import (
    ResolvedAuth,
    _resolve_workspace,
    get_api_key_info,
    get_write_permission,
    resolve_workspace_read,
    resolve_workspace_write,
)
from src.services.database import get_database

pytestmark = pytest.mark.asyncio

WS = "ws-1"


def _write_key() -> APIKeyInfo:
    return APIKeyInfo(
        key_id="key-1",
        user_id="user-1",
        workspace_id=None,
        permissions=["read", "search", "write"],  # type: ignore[arg-type]
        rate_limit=100,
        expires_at=None,
        status="active",
    )


def _scoped_key(workspace_id: str) -> APIKeyInfo:
    """A workspace-scoped key (#138): bound to exactly ``workspace_id``."""
    return APIKeyInfo(
        key_id="key-scoped",
        user_id="user-1",
        workspace_id=workspace_id,
        permissions=["read", "search", "write"],  # type: ignore[arg-type]
        rate_limit=100,
        expires_at=None,
        status="active",
    )


def _document(document_id: str = "doc-1") -> Document:
    now = datetime.now(timezone.utc)
    return Document(
        id=document_id,
        name="report.pdf",
        workspace_id=WS,
        source_type="s3",
        mime_type="application/pdf",
        size_bytes=2048,
        chunk_count=3,
        status="processed",
        created_at=now,
        updated_at=now,
        metadata=None,
    )


def _upload_fields(document_id: str = "doc-1") -> dict:
    return {
        "document_id": document_id,
        "workspace_id": WS,
        "user_id": "user-1",
        "filename": "stored.pdf",
        "original_filename": "report.pdf",
        "content_type": "application/pdf",
        "size_bytes": 2048,
        "storage_backend": "s3",
        "storage_path": f"{WS}/abc/stored.pdf",
        "storage_bucket": "docs",
        "storage_url": "s3://docs/stored.pdf",
    }


def _mock_db() -> AsyncMock:
    db = AsyncMock()
    db.validate_api_key = AsyncMock(return_value=_write_key())
    db.get_document_by_id = AsyncMock(return_value=_document())
    db.get_user_workspace_ids = AsyncMock(return_value=[WS])
    db.get_document_upload_fields = AsyncMock(return_value=_upload_fields())
    db.create_or_reset_pending_document = AsyncMock(return_value=None)
    db.mark_document_failed = AsyncMock(return_value=None)
    return db


async def _call_mcp_tool(name: str, arguments: dict, db: AsyncMock):
    """Drive a tool through the real dispatcher (auth + permission + handler)."""
    server = mcp_server.create_mcp_server()
    with patch.object(mcp_server, "get_database", AsyncMock(return_value=db)):
        req = CallToolRequest(
            method="tools/call",
            params=CallToolRequestParams(name=name, arguments=arguments),
        )
        handler = server.request_handlers[CallToolRequest]
        result = await handler(req)
        return result.root.content


# ---------------------------------------------------------------------------
# Refresh: MQ down after the document was reset to 'pending'
# ---------------------------------------------------------------------------


class TestRefreshMqDownParity:
    """REST marks the document failed and returns 503 (covered in
    tests/unit/test_refresh_and_verify_endpoints.py). The MCP twin must do the
    equivalent: mark failed + clear error — never a permanently-pending doc."""

    async def test_mcp_refresh_mq_down_marks_document_failed(self):
        db = _mock_db()
        failing_mq = AsyncMock()
        failing_mq.publish = AsyncMock(side_effect=RuntimeError("mq down"))

        with patch("src.services.mq.get_mq_service", new=AsyncMock(return_value=failing_mq)):
            result = await _call_mcp_tool(
                "refresh_stale_source",
                {"api_key": "ink_k", "document_id": "doc-1"},
                db,
            )

        # Parity with REST: the pending reset must be compensated...
        db.mark_document_failed.assert_awaited_once()
        # ...and the caller must see a real error, not a success summary.
        assert "Error" in result[0].text

    async def test_mcp_refresh_mq_down_never_reports_success(self):
        """Even before #98 is fixed, an MQ outage must not read as success —
        this half of the contract already holds (the dispatcher catch-all
        returns an error) and must not regress while #96 is in flight."""
        db = _mock_db()
        failing_mq = AsyncMock()
        failing_mq.publish = AsyncMock(side_effect=RuntimeError("mq down"))

        with patch("src.services.mq.get_mq_service", new=AsyncMock(return_value=failing_mq)):
            result = await _call_mcp_tool(
                "refresh_stale_source",
                {"api_key": "ink_k", "document_id": "doc-1"},
                db,
            )

        assert "Error" in result[0].text
        assert "queued for re-ingestion" not in result[0].text


# ---------------------------------------------------------------------------
# Delete: vector store down before anything user-visible is removed
# ---------------------------------------------------------------------------


class TestDeleteVectorStoreDownParity:
    """REST returns 503 with the document intact (covered in
    tests/unit/test_delete_document.py). The MCP twin must be equivalent:
    error text, no database delete, operation retryable."""

    async def test_mcp_delete_vector_store_down_leaves_document_intact(self):
        db = _mock_db()
        db.delete_document = AsyncMock()
        failing_search = AsyncMock()
        failing_search.delete_document_vectors = AsyncMock(
            side_effect=RuntimeError("weaviate down")
        )

        with patch(
            "src.services.deletion.get_search_service",
            new=AsyncMock(return_value=failing_search),
        ):
            result = await _call_mcp_tool(
                "delete_document",
                {"api_key": "ink_k", "document_id": "doc-1"},
                db,
            )

        assert "Error" in result[0].text
        db.delete_document.assert_not_awaited()


# ---------------------------------------------------------------------------
# Upload: the compensating mark itself fails (#99)
# ---------------------------------------------------------------------------


class TestUploadDoubleFailureRecovery:
    """When MQ publish fails AND mark_document_failed also fails, the document
    is orphaned: DB says 'pending', the response says 'failed', and nothing can
    reconcile them. #99: the mark is retried with backoff; exhaustion emits a
    CRITICAL log + document_compensation_exhausted_total so the divergence is
    never silent (see src/services/compensation.py)."""

    async def test_upload_mark_failed_failure_is_retried(self):
        key = _write_key()
        db = _mock_db()
        db.get_document_id_by_content_hash = AsyncMock(return_value=None)
        db.get_document_id_by_filename = AsyncMock(return_value=None)
        db.mark_document_failed = AsyncMock(side_effect=RuntimeError("db degraded"))

        storage = MagicMock()
        storage.generate_key.return_value = f"{WS}/fake-uuid/test.txt"
        storage.upload_file = AsyncMock(return_value=f"{WS}/fake-uuid/test.txt")
        storage.build_storage_url.return_value = f"s3://docs/{WS}/fake-uuid/test.txt"
        storage._bucket = "docs"
        failing_mq = AsyncMock()
        failing_mq.publish = AsyncMock(side_effect=RuntimeError("mq down"))

        application = create_app()
        application.dependency_overrides[resolve_workspace_write] = lambda: ResolvedAuth(
            key_info=key, workspace_id=WS
        )
        application.dependency_overrides[get_database] = lambda: db
        try:
            with (
                # Upload storage/MQ acquisition lives in the shared intake
                # pipeline since #87 — patch there, not the REST module.
                patch("src.services.document_intake.get_storage_service", return_value=storage),
                patch(
                    "src.services.document_intake.get_mq_service",
                    new_callable=AsyncMock,
                    return_value=failing_mq,
                ),
            ):
                transport = ASGITransport(app=application)
                async with AsyncClient(transport=transport, base_url="http://test") as ac:
                    response = await ac.post(
                        "/v1/documents",
                        headers={"X-API-Key": "ink_test_key"},
                        files={"file": ("test.txt", io.BytesIO(b"hello"), "text/plain")},
                    )
        finally:
            application.dependency_overrides.clear()

        # The response half of the contract already holds: never claim success.
        assert response.json()["status"] == "failed"
        # The recovery half is the #99 ask: the failed mark must be retried so
        # a transient DB blip can't orphan the row as 'pending'.
        assert db.mark_document_failed.await_count >= 2


# ---------------------------------------------------------------------------
# Refresh: the compensating mark itself fails — both surfaces (#99 sweep)
# ---------------------------------------------------------------------------


class TestRefreshDoubleFailureRecoveryParity:
    """The #99 orphan exists on every compensation site, not just upload: the
    refresh handlers (REST and MCP) also mark the pending reset failed when MQ
    is down, and that mark can itself fail. Both surfaces must retry the mark
    (same helper, same contract) so no surface can silently strand a document
    as 'pending'."""

    async def test_rest_refresh_mark_failed_failure_is_retried(self):
        key = _write_key()
        db = _mock_db()
        db.mark_document_failed = AsyncMock(side_effect=RuntimeError("db degraded"))
        failing_mq = AsyncMock()
        failing_mq.publish = AsyncMock(side_effect=RuntimeError("mq down"))

        application = create_app()
        application.dependency_overrides[resolve_workspace_write] = lambda: ResolvedAuth(
            key_info=key, workspace_id=WS
        )
        application.dependency_overrides[get_database] = lambda: db
        try:
            with patch(
                "src.api.v1.documents.get_mq_service",
                new_callable=AsyncMock,
                return_value=failing_mq,
            ):
                transport = ASGITransport(app=application)
                async with AsyncClient(transport=transport, base_url="http://test") as ac:
                    response = await ac.post(
                        "/v1/documents/doc-1/refresh",
                        headers={"X-API-Key": "ink_test_key"},
                    )
        finally:
            application.dependency_overrides.clear()

        # Response half: the caller sees a real error, never success.
        assert response.status_code == 503
        # Recovery half: the failed mark is retried, not swallowed.
        assert db.mark_document_failed.await_count >= 2

    async def test_mcp_refresh_mark_failed_failure_is_retried(self):
        db = _mock_db()
        db.mark_document_failed = AsyncMock(side_effect=RuntimeError("db degraded"))
        failing_mq = AsyncMock()
        failing_mq.publish = AsyncMock(side_effect=RuntimeError("mq down"))

        with patch("src.services.mq.get_mq_service", new=AsyncMock(return_value=failing_mq)):
            result = await _call_mcp_tool(
                "refresh_stale_source",
                {"api_key": "ink_k", "document_id": "doc-1"},
                db,
            )

        # Parity with REST: error surfaced AND the mark retried.
        assert "Error" in result[0].text
        assert db.mark_document_failed.await_count >= 2


# ---------------------------------------------------------------------------
# Workspace scoping: a workspace-scoped key must bind identically on REST and
# MCP (#138)
# ---------------------------------------------------------------------------


class TestWorkspaceScopeParity:
    """REST's ``_resolve_workspace`` (src/services/auth.py) binds a
    workspace-scoped API key (``APIKeyInfo.workspace_id is not None``) to
    exactly that workspace and 403s a request for any other workspace — even
    one the owning user also owns (tests/security/test_workspace_isolation.py
    ::test_workspace_scoped_key_cannot_cross_even_to_an_owned_workspace).

    MCP's ``_get_workspace_ids`` had a SEPARATE implementation of this rule
    that only checked ``database.get_user_workspace_ids`` — the user's FULL
    owned set — and never consulted ``key_info.workspace_id`` at all. So a
    scoped key's MCP calls could reach any workspace its owner owned, while
    the identical REST request was rejected. This class pins both halves of
    the contract so the two surfaces cannot drift again: same key, same
    owned-workspace set, same foreign request, same rejection.
    """

    async def test_rest_scoped_key_rejects_other_owned_workspace(self):
        key = _scoped_key("ws-a")
        mock_db = AsyncMock()
        # The key's binding (ws-a) is confirmed owned in Mongo; the request
        # names a DIFFERENT workspace (ws-b), rejected on the header-mismatch
        # check (#138 blocker-2: scoped-key validation consults ONLY
        # user_owns_workspace_in_mongo, never get_user_workspace_ids).
        mock_db.user_owns_workspace_in_mongo = AsyncMock(return_value=True)
        with patch("src.services.auth.get_database", AsyncMock(return_value=mock_db)):
            with pytest.raises(HTTPException) as exc_info:
                await _resolve_workspace(key, "ws-b", required=False)
        assert exc_info.value.status_code == 403
        # Pinned so the MCP test below can assert byte-for-byte wording parity
        # rather than re-deriving the expected string independently.
        assert exc_info.value.detail == (
            "API key is scoped to workspace 'ws-a' and cannot access workspace 'ws-b'"
        )

    async def test_mcp_scoped_key_rejects_other_owned_workspace(self):
        """MCP twin of the REST test above: identical key and owned set, the
        same foreign workspace requested through a real tool call — must also
        be rejected, not silently served, AND with the exact same wording
        REST uses (#138 fix-1 follow-up: outcome parity alone let the two
        surfaces tell the caller different things — REST named the key's own
        binding so it could retry; MCP said only "you don't have access",
        indistinguishable from "workspace doesn't exist")."""
        key = _scoped_key("ws-a")
        db = _mock_db()
        db.validate_api_key = AsyncMock(return_value=key)
        # #138 blocker-2: scoped-key validation consults ONLY
        # user_owns_workspace_in_mongo, never get_user_workspace_ids.
        db.user_owns_workspace_in_mongo = AsyncMock(return_value=True)

        result = await _call_mcp_tool(
            "list_documents", {"api_key": "ink_k", "workspace_id": "ws-b"}, db
        )

        # Byte-for-byte the REST detail above, just "Error: "-prefixed per the
        # MCP text convention.
        assert result[0].text == (
            "Error: API key is scoped to workspace 'ws-a' and cannot access workspace 'ws-b'"
        )
        # The foreign workspace must never be queried.
        db.get_documents.assert_not_awaited()

    async def test_mcp_scoped_key_with_no_workspace_arg_narrows_to_binding(self):
        """Parity with REST's other half: omitting workspace_id must resolve
        to exactly the key's bound workspace, never expand to the user's full
        owned set (REST's _resolve_workspace never expands a scoped key's
        access either)."""
        key = _scoped_key("ws-a")
        db = _mock_db()
        db.validate_api_key = AsyncMock(return_value=key)
        # #138 blocker-2: scoped-key validation consults ONLY
        # user_owns_workspace_in_mongo, never get_user_workspace_ids.
        db.user_owns_workspace_in_mongo = AsyncMock(return_value=True)
        db.get_documents_multi_workspace = AsyncMock(return_value=([], 0))

        await _call_mcp_tool("list_documents", {"api_key": "ink_k"}, db)

        # Only the key's bound workspace was queried — ws-b was never touched.
        db.get_documents_multi_workspace.assert_awaited_once_with(["ws-a"], 1, 20)


# ---------------------------------------------------------------------------
# Not-found vs permission-denied: REST's undifferentiated 404 must hold on
# MCP too (#138 blocker-1)
# ---------------------------------------------------------------------------


class TestDocumentNotFoundVsPermissionDeniedParity:
    """REST's ``GET /v1/documents/{id}`` (src/api/v1/documents.py) answers a
    MISSING document and a document that EXISTS but is in a workspace the
    caller isn't authorised for with the exact same ``404 "Document not
    found"`` — the workspace-scoped DB query returns ``None`` in both cases,
    so the two are structurally indistinguishable by construction. MCP's
    ``get_document`` used to leak the difference via a separate "you don't
    have access to document" message, which is a cross-workspace EXISTENCE
    ORACLE: a scoped key could learn exactly which document ids exist in a
    workspace it cannot read. This class pins that BOTH surfaces now treat
    the two cases identically, and that a scoped key's genuinely-missing vs
    genuinely-foreign requests are indistinguishable on each surface.
    """

    async def test_rest_missing_and_foreign_document_both_404_identically(self):
        key = _scoped_key("ws-a")
        foreign_doc = _document("doc-x")  # lives in WS ("ws-1"), not "ws-a"

        async def _get_document(document_id: str, workspace_id: str):
            # Simulates the real workspace-scoped query: only matches when
            # queried with the document's OWN workspace.
            return foreign_doc if workspace_id == foreign_doc.workspace_id else None

        db = _mock_db()
        db.get_document = AsyncMock(side_effect=_get_document)

        application = create_app()
        application.dependency_overrides[get_database] = lambda: db
        application.dependency_overrides[resolve_workspace_read] = lambda: ResolvedAuth(
            key_info=key, workspace_id="ws-a"
        )
        try:
            transport = ASGITransport(app=application)
            async with AsyncClient(transport=transport, base_url="http://test") as ac:
                foreign_response = await ac.get(
                    "/v1/documents/doc-x", headers={"X-API-Key": "ink_test_key"}
                )
                missing_response = await ac.get(
                    "/v1/documents/doc-does-not-exist", headers={"X-API-Key": "ink_test_key"}
                )
        finally:
            application.dependency_overrides.clear()

        assert foreign_response.status_code == missing_response.status_code == 404
        assert (
            foreign_response.json()["detail"]
            == missing_response.json()["detail"]
            == "Document not found"
        )

    async def test_mcp_missing_and_foreign_document_both_not_found_identically(self):
        """MCP twin: a scoped key's get_document for a document in a
        different owned workspace, and for an id that does not exist at all,
        must produce byte-for-byte the same error text (#138 blocker-1)."""
        key = _scoped_key("ws-a")
        foreign_doc = _document("doc-x")  # lives in WS ("ws-1"), not "ws-a"

        db = _mock_db()
        db.validate_api_key = AsyncMock(return_value=key)
        # #138 blocker-2: scoped-key validation consults ONLY
        # user_owns_workspace_in_mongo, never get_user_workspace_ids. The
        # binding (ws-a) is owned; the document lives in WS ("ws-1") — a
        # DIFFERENT workspace — so it's rejected on the mismatch regardless.
        db.user_owns_workspace_in_mongo = AsyncMock(return_value=True)
        db.get_document_by_id = AsyncMock(return_value=foreign_doc)
        foreign_result = await _call_mcp_tool(
            "get_document", {"api_key": "ink_k", "document_id": "doc-x"}, db
        )

        db.get_document_by_id = AsyncMock(return_value=None)
        missing_result = await _call_mcp_tool(
            "get_document", {"api_key": "ink_k", "document_id": "doc-does-not-exist"}, db
        )

        # Both name their OWN requested id (so the messages differ only in
        # the id echoed back, never in whether the caller is told "you don't
        # have access" vs "not found") — substitute doc-x for the missing
        # one's id and the strings must match exactly.
        assert foreign_result[0].text == "Error: Document 'doc-x' not found"
        assert missing_result[0].text == "Error: Document 'doc-does-not-exist' not found"


# Upload: mislabeled content (magic-byte mismatch) -- both surfaces (#117)
# ---------------------------------------------------------------------------


class TestUploadContentTypeMismatchParity:
    """#117: REST and MCP share the exact same `intake_document` pipeline, so
    a magic-byte mismatch must reject identically on both -- no document row,
    no S3 object, a real error surfaced to the caller. Both surfaces are
    exercised through their real entry points (HTTP route / MCP dispatcher),
    not by calling intake_document directly, so this pins the FULL path each
    surface actually runs in production.
    """

    async def test_rest_and_mcp_both_reject_a_mismatched_upload_with_no_side_effects(self):
        # --- REST: PNG bytes declared as text/plain -----------------------
        rest_db = _mock_db()
        rest_db.get_document_id_by_content_hash = AsyncMock(return_value=None)
        rest_db.get_document_id_by_filename = AsyncMock(return_value=None)

        rest_storage = MagicMock()
        rest_storage.generate_key.return_value = f"{WS}/fake-uuid/scan.png"
        rest_storage.upload_file = AsyncMock(return_value=f"{WS}/fake-uuid/scan.png")
        rest_storage.build_storage_url.return_value = f"s3://docs/{WS}/fake-uuid/scan.png"
        rest_storage._bucket = "docs"
        rest_mq = AsyncMock()
        rest_mq.publish = AsyncMock(return_value="1-0")

        write_key = _write_key()
        application = create_app()
        application.dependency_overrides[get_api_key_info] = lambda: write_key
        application.dependency_overrides[get_write_permission] = lambda: write_key
        application.dependency_overrides[resolve_workspace_write] = lambda: ResolvedAuth(
            key_info=write_key, workspace_id=WS
        )
        application.dependency_overrides[get_database] = lambda: rest_db
        try:
            with (
                patch(
                    "src.services.document_intake.get_storage_service",
                    return_value=rest_storage,
                ),
                patch(
                    "src.services.document_intake.get_mq_service",
                    new_callable=AsyncMock,
                    return_value=rest_mq,
                ),
            ):
                transport = ASGITransport(app=application)
                async with AsyncClient(transport=transport, base_url="http://test") as ac:
                    rest_response = await ac.post(
                        "/v1/documents",
                        headers={"X-API-Key": "ink_test_key"},
                        files={
                            "file": (
                                "scan.png",
                                io.BytesIO(b"\x89PNG\r\n\x1a\n fake png bytes"),
                                "text/plain",
                            )
                        },
                    )
        finally:
            application.dependency_overrides.clear()

        assert rest_response.status_code == 400
        rest_storage.upload_file.assert_not_awaited()
        rest_db.create_or_reset_pending_document.assert_not_awaited()
        rest_mq.publish.assert_not_awaited()

        # --- MCP: text content whose bytes match the PDF signature --------
        # upload_document only transports text (#87 Task 3), so the only
        # sniff failure it CAN trigger is "declared text, but the bytes
        # match a different registered type's signature" -- exactly this.
        mcp_db = _mock_db()
        mcp_db.get_document_id_by_content_hash = AsyncMock(return_value=None)
        mcp_db.get_document_id_by_filename = AsyncMock(return_value=None)
        mcp_storage = MagicMock()
        mcp_storage.generate_key.return_value = f"{WS}/fake-uuid/notes.md"
        mcp_storage.upload_file = AsyncMock(return_value=f"{WS}/fake-uuid/notes.md")
        mcp_storage.build_storage_url.return_value = f"s3://docs/{WS}/fake-uuid/notes.md"
        mcp_storage._bucket = "docs"
        mcp_mq = AsyncMock()
        mcp_mq.publish = AsyncMock(return_value="1-0")

        with (
            patch(
                "src.services.document_intake.get_storage_service",
                return_value=mcp_storage,
            ),
            patch(
                "src.services.document_intake.get_mq_service",
                new_callable=AsyncMock,
                return_value=mcp_mq,
            ),
        ):
            mcp_result = await _call_mcp_tool(
                "upload_document",
                {
                    "api_key": "ink_k",
                    "filename": "notes.md",
                    "content": "%PDF-1.4\nthis is text, not a real pdf",
                    "content_type": "text/plain",
                },
                mcp_db,
            )

        assert "Error" in mcp_result[0].text
        mcp_storage.upload_file.assert_not_awaited()
        mcp_db.create_or_reset_pending_document.assert_not_awaited()
        mcp_mq.publish.assert_not_awaited()


# ---------------------------------------------------------------------------
# Upload: explicitly-unsupported legacy formats (#124/#126 review blocker 3)
# ---------------------------------------------------------------------------


class TestUploadLegacyFormatRejectionParity:
    """#124/#126: legacy .doc (application/msword) is deliberately NOT in
    FILE_TYPE_REGISTRY and must be rejected on BOTH surfaces with no side
    effects -- no S3 object, no document row, no MQ publish.

    This is the exact pair a review caught missing: REST's own local
    rejection table meant MCP's `upload_document` never learned about it,
    and MCP's content_type default (derived from the filename extension
    when the caller omits it) fell through to a generic MCP-eligible type
    for "report.doc" -- silently ACCEPTING the format both issues say must
    be rejected. Both are now sourced from the shared
    `inh_contracts.EXPLICITLY_UNSUPPORTED` table; this test is the pin.
    """

    async def test_rest_and_mcp_both_reject_legacy_doc_with_no_side_effects(self):
        # --- REST: declared application/msword -----------------------------
        rest_db = _mock_db()
        rest_db.get_document_id_by_content_hash = AsyncMock(return_value=None)
        rest_db.get_document_id_by_filename = AsyncMock(return_value=None)

        rest_storage = MagicMock()
        rest_storage.generate_key.return_value = f"{WS}/fake-uuid/report.doc"
        rest_storage.upload_file = AsyncMock(return_value=f"{WS}/fake-uuid/report.doc")
        rest_storage.build_storage_url.return_value = f"s3://docs/{WS}/fake-uuid/report.doc"
        rest_storage._bucket = "docs"
        rest_mq = AsyncMock()
        rest_mq.publish = AsyncMock(return_value="1-0")

        write_key = _write_key()
        application = create_app()
        application.dependency_overrides[get_api_key_info] = lambda: write_key
        application.dependency_overrides[get_write_permission] = lambda: write_key
        application.dependency_overrides[resolve_workspace_write] = lambda: ResolvedAuth(
            key_info=write_key, workspace_id=WS
        )
        application.dependency_overrides[get_database] = lambda: rest_db
        try:
            with (
                patch(
                    "src.services.document_intake.get_storage_service",
                    return_value=rest_storage,
                ),
                patch(
                    "src.services.document_intake.get_mq_service",
                    new_callable=AsyncMock,
                    return_value=rest_mq,
                ),
            ):
                transport = ASGITransport(app=application)
                async with AsyncClient(transport=transport, base_url="http://test") as ac:
                    rest_response = await ac.post(
                        "/v1/documents",
                        headers={"X-API-Key": "ink_test_key"},
                        files={
                            "file": (
                                "report.doc",
                                io.BytesIO(b"\xd0\xcf\x11\xe0 fake OLE compound file bytes"),
                                "application/msword",
                            )
                        },
                    )
        finally:
            application.dependency_overrides.clear()

        assert rest_response.status_code == 400
        assert ".docx" in rest_response.json()["detail"]
        rest_storage.upload_file.assert_not_awaited()
        rest_db.create_or_reset_pending_document.assert_not_awaited()
        rest_mq.publish.assert_not_awaited()

        # --- MCP: content_type OMITTED, exactly the gap the review found --
        # '.doc' has no FILE_TYPE_REGISTRY entry, so the default-content-type
        # resolution used to fall through to 'text/markdown' (MCP-eligible)
        # and sail straight through as if it were prose.
        mcp_db = _mock_db()
        mcp_db.get_document_id_by_content_hash = AsyncMock(return_value=None)
        mcp_db.get_document_id_by_filename = AsyncMock(return_value=None)
        mcp_storage = MagicMock()
        mcp_storage.generate_key.return_value = f"{WS}/fake-uuid/report.doc"
        mcp_storage.upload_file = AsyncMock(return_value=f"{WS}/fake-uuid/report.doc")
        mcp_storage.build_storage_url.return_value = f"s3://docs/{WS}/fake-uuid/report.doc"
        mcp_storage._bucket = "docs"
        mcp_mq = AsyncMock()
        mcp_mq.publish = AsyncMock(return_value="1-0")

        with (
            patch(
                "src.services.document_intake.get_storage_service",
                return_value=mcp_storage,
            ),
            patch(
                "src.services.document_intake.get_mq_service",
                new_callable=AsyncMock,
                return_value=mcp_mq,
            ),
        ):
            mcp_result = await _call_mcp_tool(
                "upload_document",
                {
                    "api_key": "ink_k",
                    "filename": "report.doc",
                    "content": "Q3 revenue was 4.2M, pasted straight from a .doc file",
                    # content_type deliberately omitted.
                },
                mcp_db,
            )

        assert "Error" in mcp_result[0].text
        assert ".docx" in mcp_result[0].text
        mcp_storage.upload_file.assert_not_awaited()
        mcp_db.create_or_reset_pending_document.assert_not_awaited()
        mcp_mq.publish.assert_not_awaited()


# ---------------------------------------------------------------------------
# Upload: a NEWLY-ACCEPTED type's magic-byte mismatch -- both surfaces
# (#121/#122). Same contract as TestUploadContentTypeMismatchParity above,
# pinned separately for a type that did not exist before this workstream, so
# a future regression in either surface's handling of the #122 'code' spec
# specifically is caught (not just the pre-existing pdf/text/plain pair).
# ---------------------------------------------------------------------------


class TestNewTypeUploadContentTypeMismatchParity:
    """#122: source code (`text/x-python` and friends) is a NEW MCP-visible
    type as of this workstream (`surfaces={"rest", "mcp"}` — see the 'code'
    FileTypeSpec). It must inherit the exact same magic-byte-sniff
    protection every other MCP-eligible type gets, on both surfaces, not
    just at REST (where the extension-fallback path also applies)."""

    async def test_rest_and_mcp_both_reject_a_mismatched_code_upload_with_no_side_effects(self):
        # --- REST: real PNG bytes declared as 'main.py' via the #122
        # octet-stream extension-fallback path (the fallback resolves a
        # spec to validate against -- it must NOT become a bypass for the
        # cross-spec magic-byte check). ---------------------------------
        rest_db = _mock_db()
        rest_db.get_document_id_by_content_hash = AsyncMock(return_value=None)
        rest_db.get_document_id_by_filename = AsyncMock(return_value=None)

        rest_storage = MagicMock()
        rest_storage.generate_key.return_value = f"{WS}/fake-uuid/main.py"
        rest_storage.upload_file = AsyncMock(return_value=f"{WS}/fake-uuid/main.py")
        rest_storage.build_storage_url.return_value = f"s3://docs/{WS}/fake-uuid/main.py"
        rest_storage._bucket = "docs"
        rest_mq = AsyncMock()
        rest_mq.publish = AsyncMock(return_value="1-0")

        write_key = _write_key()
        application = create_app()
        application.dependency_overrides[get_api_key_info] = lambda: write_key
        application.dependency_overrides[get_write_permission] = lambda: write_key
        application.dependency_overrides[resolve_workspace_write] = lambda: ResolvedAuth(
            key_info=write_key, workspace_id=WS
        )
        application.dependency_overrides[get_database] = lambda: rest_db
        try:
            with (
                patch(
                    "src.services.document_intake.get_storage_service",
                    return_value=rest_storage,
                ),
                patch(
                    "src.services.document_intake.get_mq_service",
                    new_callable=AsyncMock,
                    return_value=rest_mq,
                ),
            ):
                transport = ASGITransport(app=application)
                async with AsyncClient(transport=transport, base_url="http://test") as ac:
                    rest_response = await ac.post(
                        "/v1/documents",
                        headers={"X-API-Key": "ink_test_key"},
                        files={
                            "file": (
                                "main.py",
                                io.BytesIO(b"\x89PNG\r\n\x1a\n fake png pretending to be code"),
                                "application/octet-stream",
                            )
                        },
                    )
        finally:
            application.dependency_overrides.clear()

        assert rest_response.status_code == 400
        rest_storage.upload_file.assert_not_awaited()
        rest_db.create_or_reset_pending_document.assert_not_awaited()
        rest_mq.publish.assert_not_awaited()

        # --- MCP: text content declared as the new 'text/x-python' alias
        # whose bytes match the PDF signature instead (MCP only transports
        # text, so this is the MCP-reachable analog of the REST case
        # above). ----------------------------------------------------------
        mcp_db = _mock_db()
        mcp_db.get_document_id_by_content_hash = AsyncMock(return_value=None)
        mcp_db.get_document_id_by_filename = AsyncMock(return_value=None)
        mcp_storage = MagicMock()
        mcp_storage.generate_key.return_value = f"{WS}/fake-uuid/main.py"
        mcp_storage.upload_file = AsyncMock(return_value=f"{WS}/fake-uuid/main.py")
        mcp_storage.build_storage_url.return_value = f"s3://docs/{WS}/fake-uuid/main.py"
        mcp_storage._bucket = "docs"
        mcp_mq = AsyncMock()
        mcp_mq.publish = AsyncMock(return_value="1-0")

        with (
            patch(
                "src.services.document_intake.get_storage_service",
                return_value=mcp_storage,
            ),
            patch(
                "src.services.document_intake.get_mq_service",
                new_callable=AsyncMock,
                return_value=mcp_mq,
            ),
        ):
            mcp_result = await _call_mcp_tool(
                "upload_document",
                {
                    "api_key": "ink_k",
                    "filename": "main.py",
                    "content": "%PDF-1.4\nthis is text, not a real pdf",
                    "content_type": "text/x-python",
                },
                mcp_db,
            )

        assert "Error" in mcp_result[0].text
        mcp_storage.upload_file.assert_not_awaited()
        mcp_db.create_or_reset_pending_document.assert_not_awaited()
        mcp_mq.publish.assert_not_awaited()


# ---------------------------------------------------------------------------
# Upload: the stored content_type label for a multi-MIME spec -- both
# surfaces (#197)
# ---------------------------------------------------------------------------


class TestUploadCodeContentTypeLabelParity:
    """#197: the "code" spec (#122) pools 22 MIME aliases across 21 distinct
    languages under one registry entry. REST never mislabels a code upload —
    it either forwards the client's own declared ``Content-Type`` verbatim,
    or (a generic/absent header) resolves via the honest ``.go``-extension
    fallback while still storing whatever the client actually declared. MCP
    is the surface that GUESSES a `content_type` when the caller omits it
    (REST has no equivalent guess — its route has no schema-advertised
    default the way the MCP tool does), and that guess used to be wrong for
    every code file (``mime_types[0]`` == "text/x-python" always). This pins
    that MCP's guess now agrees with what an accurate REST declaration for
    the SAME file would store — both surfaces end up with the identical,
    correct ``text/x-go`` label for a ``.go`` upload, through their real
    entry points (HTTP route / MCP dispatcher), asserted against the exact
    kwarg persisted to storage (``create_or_reset_pending_document``'s
    ``content_type``) so this can't pass on a response-body coincidence.
    """

    async def test_rest_declared_and_mcp_defaulted_go_upload_store_the_same_mime(self):
        # --- REST: client declares the correct, specific Content-Type -----
        # (the realistic REST shape: REST has no schema-advertised default
        # to fall back on the way MCP's optional `content_type` arg does, so
        # a REST client either declares accurately or sends octet-stream —
        # this exercises the "declares accurately" half, which is what MCP's
        # now-fixed default must agree with.)
        rest_db = _mock_db()
        rest_db.get_document_id_by_content_hash = AsyncMock(return_value=None)
        rest_db.get_document_id_by_filename = AsyncMock(return_value=None)

        rest_storage = MagicMock()
        rest_storage.generate_key.return_value = f"{WS}/fake-uuid/lib.go"
        rest_storage.upload_file = AsyncMock(return_value=f"{WS}/fake-uuid/lib.go")
        rest_storage.build_storage_url.return_value = f"s3://docs/{WS}/fake-uuid/lib.go"
        rest_storage._bucket = "docs"
        rest_mq = AsyncMock()
        rest_mq.publish = AsyncMock(return_value="1-0")

        write_key = _write_key()
        application = create_app()
        application.dependency_overrides[get_api_key_info] = lambda: write_key
        application.dependency_overrides[get_write_permission] = lambda: write_key
        application.dependency_overrides[resolve_workspace_write] = lambda: ResolvedAuth(
            key_info=write_key, workspace_id=WS
        )
        application.dependency_overrides[get_database] = lambda: rest_db
        try:
            with (
                patch(
                    "src.services.document_intake.get_storage_service",
                    return_value=rest_storage,
                ),
                patch(
                    "src.services.document_intake.get_mq_service",
                    new_callable=AsyncMock,
                    return_value=rest_mq,
                ),
            ):
                transport = ASGITransport(app=application)
                async with AsyncClient(transport=transport, base_url="http://test") as ac:
                    rest_response = await ac.post(
                        "/v1/documents",
                        headers={"X-API-Key": "ink_test_key"},
                        files={
                            "file": (
                                "lib.go",
                                io.BytesIO(b"package main\n"),
                                "text/x-go",
                            )
                        },
                    )
        finally:
            application.dependency_overrides.clear()

        assert rest_response.status_code == 201
        rest_stored_content_type = rest_db.create_or_reset_pending_document.call_args.kwargs[
            "content_type"
        ]
        assert rest_stored_content_type == "text/x-go"

        # --- MCP: content_type OMITTED -- must now resolve to the SAME
        # specific label, not the pre-#197 "text/x-python" guess. ----------
        mcp_db = _mock_db()
        mcp_db.get_document_id_by_content_hash = AsyncMock(return_value=None)
        mcp_db.get_document_id_by_filename = AsyncMock(return_value=None)
        mcp_storage = MagicMock()
        mcp_storage.generate_key.return_value = f"{WS}/fake-uuid/lib.go"
        mcp_storage.upload_file = AsyncMock(return_value=f"{WS}/fake-uuid/lib.go")
        mcp_storage.build_storage_url.return_value = f"s3://docs/{WS}/fake-uuid/lib.go"
        mcp_storage._bucket = "docs"
        mcp_mq = AsyncMock()
        mcp_mq.publish = AsyncMock(return_value="1-0")

        with (
            patch(
                "src.services.document_intake.get_storage_service",
                return_value=mcp_storage,
            ),
            patch(
                "src.services.document_intake.get_mq_service",
                new_callable=AsyncMock,
                return_value=mcp_mq,
            ),
        ):
            mcp_result = await _call_mcp_tool(
                "upload_document",
                {
                    "api_key": "ink_k",
                    "filename": "lib.go",
                    "content": "package main\n",
                    # content_type deliberately omitted -- the #197 gap.
                },
                mcp_db,
            )

        assert "Error" not in mcp_result[0].text, mcp_result[0].text
        mcp_stored_content_type = mcp_db.create_or_reset_pending_document.call_args.kwargs[
            "content_type"
        ]
        assert mcp_stored_content_type == "text/x-go"
        assert mcp_stored_content_type != "text/x-python"
        # The pin: both surfaces land on the exact same label for the exact
        # same file.
        assert mcp_stored_content_type == rest_stored_content_type


# ---------------------------------------------------------------------------
# Auth: an expired key must be rejected on both surfaces, INDEPENDENT of
# whether the DatabaseService implementation itself filters expiry (#180)
# ---------------------------------------------------------------------------


class TestExpiredKeyDispatcherParity:
    """REST's ``require_api_key`` (src/services/auth.py) calls
    ``key_info.is_expired()`` itself, AFTER ``validate_api_key`` returns —
    see tests/security/test_auth_regression.py::test_expired_key_is_unauthorized,
    which proves this by making the mocked ``database.validate_api_key`` return
    an already-expired ``APIKeyInfo`` directly (bypassing the real Postgres
    implementation's own ``expires_at`` filter) and asserting ``require_api_key``
    STILL 401s. That is the load-bearing half of the contract: REST does not
    trust the DB layer alone to enforce expiry.

    Before #180, MCP's ``call_tool`` dispatcher had no equivalent check — it
    dispatched straight to the tool handler once ``database.validate_api_key``
    returned a non-None ``key_info``, trusting that EVERY ``DatabaseService``
    implementation (present and future) independently re-checks
    ``expires_at``. The real Postgres-backed implementation happens to do
    this, so the gap was not exploitable through it — but the dispatcher
    itself carried no such guarantee, which is exactly the "primary path
    correct, failure path next to it wrong" shape CLAUDE.md's parity rule
    exists to catch.

    This test drives the SAME scenario as the REST regression test above
    (``database.validate_api_key`` returns an expired key_info directly, not
    filtered by any real DB query) through the real MCP dispatcher, and pins
    that it now refuses to dispatch — mirroring REST's own
    "don't trust the DB layer alone" posture, not just its current outcome.
    """

    def _expired_key(self) -> APIKeyInfo:
        return APIKeyInfo(
            key_id="key-expired",
            user_id="user-1",
            workspace_id=None,
            permissions=["read", "search", "write"],  # type: ignore[arg-type]
            rate_limit=100,
            expires_at=datetime.now(timezone.utc) - timedelta(days=1),
            status="active",
        )

    async def test_mcp_dispatcher_rejects_expired_key_even_when_db_layer_does_not_filter_it(self):
        db = _mock_db()
        # Simulates a DatabaseService implementation that does NOT filter
        # expired rows itself (unlike the real Postgres-backed one) —
        # exactly the alternate-backend scenario #180 describes.
        db.validate_api_key = AsyncMock(return_value=self._expired_key())

        result = await _call_mcp_tool("list_documents", {"api_key": "ink_expired"}, db)

        # Byte-for-byte the same wording REST's require_api_key raises for an
        # expired key (src/services/auth.py), just "Error: "-prefixed per the
        # MCP text convention — so the two surfaces don't merely reject, they
        # describe the SAME rejection the same way.
        assert result[0].text == "Error: API key has expired"
        # The tool body must never run: no workspace lookup, no document
        # listing — proving the check happens in the dispatcher itself,
        # before any handler is reached.
        db.get_user_workspace_ids.assert_not_awaited()
        db.get_documents_multi_workspace.assert_not_awaited()

    async def test_mcp_dispatcher_still_allows_a_non_expired_key(self):
        """Sanity: the new check is not over-broad — a key with no expiry
        (or a future one) still dispatches normally."""
        db = _mock_db()
        db.get_documents_multi_workspace = AsyncMock(return_value=([], 0))

        result = await _call_mcp_tool("list_documents", {"api_key": "ink_k"}, db)

        assert "Error" not in result[0].text
