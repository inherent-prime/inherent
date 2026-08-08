"""MCP workspace-boundary regression tests (#32).

The MCP server exposes ``search_documents`` and ``get_document_context``. Both
must enforce that a user can only reach workspaces / documents they are
authorised for. These tests run offline by patching ``get_database`` (and, where
needed, ``get_search_service``) at the module boundary.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

# The MCP package was renamed src/mcp -> src/mcp_server so it no longer shadows
# the third-party ``mcp`` SDK under pytest's ``pythonpath = ["src"]``; these
# boundary checks now run offline (no skip).
from src.mcp_server import server as mcp_server
from src.models.api_key import APIKeyInfo
from src.models.document import Document

pytestmark = [pytest.mark.security]


def _key(user_id: str = "user-1") -> APIKeyInfo:
    return APIKeyInfo(
        key_id="key-1",
        user_id=user_id,
        workspace_id=None,
        permissions=["read", "search"],
        rate_limit=100,
        expires_at=None,
        status="active",
    )


def _scoped_key(workspace_id: str, user_id: str = "user-1") -> APIKeyInfo:
    """A workspace-scoped key (#138): bound to exactly ``workspace_id``,
    regardless of how many workspaces the owning user also owns."""
    return APIKeyInfo(
        key_id="key-scoped",
        user_id=user_id,
        workspace_id=workspace_id,
        permissions=["read", "search", "write"],
        rate_limit=100,
        expires_at=None,
        status="active",
    )


def _patch_db(mock_db: AsyncMock):
    return patch.object(mcp_server, "get_database", AsyncMock(return_value=mock_db))


@pytest.mark.asyncio
async def test_get_workspace_ids_rejects_unauthorised_workspace() -> None:
    """Requesting a specific workspace the user does not own returns an error
    and no workspace ids."""
    mock_db = AsyncMock()
    mock_db.get_user_workspace_ids = AsyncMock(return_value=["ws-owned"])
    with _patch_db(mock_db):
        ws_ids, error = await mcp_server._get_workspace_ids(_key(), "ws-foreign")
    assert ws_ids == []
    assert error is not None
    assert "don't have access" in error


@pytest.mark.asyncio
async def test_get_workspace_ids_allows_owned_workspace() -> None:
    """A workspace the user owns resolves to exactly that workspace.

    Unscoped key: authorization comes from get_user_workspace_ids (the
    Mongo-union-Postgres listing helper), not user_owns_workspace_in_mongo
    (which only applies to a workspace-scoped key's binding, #138 blocker-2).
    """
    mock_db = AsyncMock()
    mock_db.get_user_workspace_ids = AsyncMock(return_value=["ws-a", "ws-b"])
    with _patch_db(mock_db):
        ws_ids, error = await mcp_server._get_workspace_ids(_key(), "ws-b")
    assert ws_ids == ["ws-b"]
    assert error is None


@pytest.mark.asyncio
async def test_search_blocks_unauthorised_workspace_and_never_searches() -> None:
    """When a foreign workspace is requested, _handle_search returns the access
    error WITHOUT ever invoking the search service."""
    mock_db = AsyncMock()
    mock_db.get_user_workspace_ids = AsyncMock(return_value=["ws-owned"])
    mock_search = AsyncMock()

    with (
        _patch_db(mock_db),
        patch.object(mcp_server, "get_search_service", AsyncMock(return_value=mock_search)),
    ):
        result = await mcp_server._handle_search(
            _key(), {"query": "secret", "workspace_id": "ws-foreign"}
        )

    text = result[0].text
    assert "don't have access" in text
    # The search service must never be called for an unauthorised workspace.
    mock_search.search.assert_not_called()


def _foreign_doc(document_id: str = "doc-x", workspace_id: str = "ws-foreign") -> Document:
    return Document(
        id=document_id,
        name="foreign.txt",
        workspace_id=workspace_id,
        source_type="upload",
        mime_type="text/plain",
        size_bytes=10,
        chunk_count=1,
        status="processed",
        created_at=__import__("datetime").datetime.now(),
        updated_at=__import__("datetime").datetime.now(),
    )


@pytest.mark.asyncio
async def test_get_context_blocks_document_in_unauthorised_workspace() -> None:
    """get_document_context must refuse a document whose workspace the user does
    not own, answer with the SAME undifferentiated "not found" REST uses (not
    a distinguishable "you don't have access"), and must NOT fetch its chunks.

    A distinguishable message here is a cross-workspace EXISTENCE ORACLE: a
    caller could tell "doc-x doesn't exist" apart from "doc-x exists in a
    workspace you can't read" and iterate ids to map another workspace's
    documents. REST's GET /v1/documents/{id} closes this by construction (the
    workspace-scoped query returns None either way, so both cases 404
    identically) — this pins the MCP equivalent (#138 blocker-1 follow-up).
    """
    mock_db = AsyncMock()
    mock_db.get_document_by_id = AsyncMock(return_value=_foreign_doc())
    mock_db.get_user_workspace_ids = AsyncMock(return_value=["ws-owned"])
    mock_db.get_document_chunks_by_doc_id = AsyncMock(return_value=[])

    with _patch_db(mock_db):
        result = await mcp_server._handle_get_context(_key(), {"document_id": "doc-x"})

    assert result[0].text == "Error: Document 'doc-x' not found"
    # Must not have leaked the document body.
    mock_db.get_document_chunks_by_doc_id.assert_not_called()


@pytest.mark.asyncio
async def test_get_context_unauthorised_and_missing_document_are_indistinguishable() -> None:
    """The oracle-closure proof: a document that EXISTS in a workspace the
    caller can't read and a document_id that does not exist AT ALL must
    produce the exact same error text (#138 blocker-1 follow-up) — an agent
    (or attacker) probing ids gets no signal either way."""
    mock_db = AsyncMock()
    mock_db.get_user_workspace_ids = AsyncMock(return_value=["ws-owned"])

    mock_db.get_document_by_id = AsyncMock(return_value=_foreign_doc("doc-x"))
    with _patch_db(mock_db):
        unauthorized_result = await mcp_server._handle_get_context(_key(), {"document_id": "doc-x"})

    mock_db.get_document_by_id = AsyncMock(return_value=None)
    with _patch_db(mock_db):
        missing_result = await mcp_server._handle_get_context(_key(), {"document_id": "doc-x"})

    assert (
        unauthorized_result[0].text == missing_result[0].text == "Error: Document 'doc-x' not found"
    )


# ---------------------------------------------------------------------------
# #138: workspace-scoped key binding parity with REST's _resolve_workspace.
#
# REST's _resolve_workspace (src/services/auth.py) binds a workspace-scoped
# key (APIKeyInfo.workspace_id is not None) to exactly that workspace and
# rejects any other workspace_id — even one the owning user also owns
# (see tests/security/test_workspace_isolation.py::
# test_workspace_scoped_key_cannot_cross_even_to_an_owned_workspace). MCP's
# _get_workspace_ids used to derive access purely from
# database.get_user_workspace_ids(user_id) — the user's FULL owned set —
# never consulting key_info.workspace_id at all.
#
# Re-measured directly (checked out src/mcp_server/server.py and
# src/services/auth.py at pre-#138 commit 51d07e6, ran this file unchanged):
# 13 of the 17 tests below FAIL against that old code, 4 pass. The 4 that
# pass do so legitimately, not because they're weak: three exercise an
# UNSCOPED key (behavior the #138 fix never touched) and one
# (test_get_workspace_ids_scoped_key_matching_request_resolves) asks a
# scoped key for its OWN workspace, which old code also happened to allow
# (the user's owned set included it too). The other 13 fail against old code
# because it let a scoped key reach a workspace outside its binding, or
# expanded a no-argument call to the owner's full set, or (for the
# BLOCKER-3 additions further down) never enforced the binding at each
# individual call site at all — and all 13 pass against the current, fixed
# code. That is what makes them regression tests: red before the fix, green
# after. (An earlier version of this comment claimed the opposite — that
# these tests would PASS unchanged against old code — which was false and
# would have told a future maintainer these tests guard nothing.)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_workspace_ids_scoped_key_cannot_cross_to_owned_workspace() -> None:
    """A workspace-scoped key must not reach a different workspace, even one
    the owning user also owns (mirrors the REST regression test).

    The rejection must name the key's OWN bound workspace (REST parity, #138
    follow-up) — not the generic "don't have access" wording, which reads as
    "that workspace doesn't exist" and gives the caller no way to retry
    correctly. Revealing the key's own binding leaks nothing: it's the
    caller's own grant.
    """
    mock_db = AsyncMock()
    # The key's binding (ws-a) is confirmed still owned in Mongo; the request
    # is for a DIFFERENT workspace (ws-b) the key was never scoped to — the
    # rejection must come from the mismatch, not from a stale/unowned binding.
    # Scoped-key validation (#138 blocker-2) consults ONLY
    # user_owns_workspace_in_mongo, never get_user_workspace_ids — mock the
    # former, not the latter, or this test would pass for the wrong reason.
    mock_db.user_owns_workspace_in_mongo = AsyncMock(return_value=True)
    with _patch_db(mock_db):
        ws_ids, error = await mcp_server._get_workspace_ids(_scoped_key("ws-a"), "ws-b")
    assert ws_ids == []
    assert error == (
        "Error: API key is scoped to workspace 'ws-a' and cannot access workspace 'ws-b'"
    )


@pytest.mark.asyncio
async def test_get_workspace_ids_scoped_key_matching_request_resolves() -> None:
    """A scoped key requesting its own bound workspace still resolves fine."""
    mock_db = AsyncMock()
    # Scoped-key validation (#138 blocker-2) consults ONLY
    # user_owns_workspace_in_mongo, never get_user_workspace_ids — mock the
    # former, not the latter, or this test would pass for the wrong reason.
    mock_db.user_owns_workspace_in_mongo = AsyncMock(return_value=True)
    with _patch_db(mock_db):
        ws_ids, error = await mcp_server._get_workspace_ids(_scoped_key("ws-a"), "ws-a")
    assert ws_ids == ["ws-a"]
    assert error is None


@pytest.mark.asyncio
async def test_get_workspace_ids_scoped_key_no_request_narrows_to_binding() -> None:
    """With no workspace_id argument, a scoped key must narrow to exactly its
    bound workspace — never expand to the user's full owned set (REST parity:
    _resolve_workspace never expands a scoped key's access either)."""
    mock_db = AsyncMock()
    # Scoped-key validation (#138 blocker-2) consults ONLY
    # user_owns_workspace_in_mongo, never get_user_workspace_ids — mock the
    # former, not the latter, or this test would pass for the wrong reason.
    mock_db.user_owns_workspace_in_mongo = AsyncMock(return_value=True)
    with _patch_db(mock_db):
        ws_ids, error = await mcp_server._get_workspace_ids(_scoped_key("ws-a"), None)
    assert ws_ids == ["ws-a"]
    assert error is None


@pytest.mark.asyncio
async def test_search_blocks_scoped_key_from_other_owned_workspace() -> None:
    """search_documents must refuse a scoped key querying a workspace outside
    its binding, name the key's own bound workspace in the error (#138
    follow-up), and never invoke the search service for it."""
    mock_db = AsyncMock()
    # Scoped-key validation (#138 blocker-2) consults ONLY
    # user_owns_workspace_in_mongo, never get_user_workspace_ids — mock the
    # former, not the latter, or this test would pass for the wrong reason.
    mock_db.user_owns_workspace_in_mongo = AsyncMock(return_value=True)
    mock_search = AsyncMock()

    with (
        _patch_db(mock_db),
        patch.object(mcp_server, "get_search_service", AsyncMock(return_value=mock_search)),
    ):
        result = await mcp_server._handle_search(
            _scoped_key("ws-a"), {"query": "secret", "workspace_id": "ws-b"}
        )

    text = result[0].text
    assert "scoped to workspace 'ws-a'" in text
    assert "cannot access workspace 'ws-b'" in text
    mock_search.search.assert_not_called()


# ---------------------------------------------------------------------------
# #138 blocker-3: one scoped-key test per authorization site the #138 fix
# touched. Coverage before this section: _get_workspace_ids, _handle_search,
# list_documents (via test_failure_parity.py). Uncovered: _resolve_
# document_for_user (gates get_document / list_chunks / explain_lineage /
# delete_document / refresh_stale_source), _handle_get_context,
# _handle_report_feedback, _handle_get_retrieval_health, and
# _resolve_single_workspace_for_upload — each gets its own test below so a
# future refactor of any one of them regresses here, not in production.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_document_for_user_scoped_key_cannot_cross_to_owned_workspace() -> None:
    """The shared document-access gate must reject a scoped key reading a
    document in a different owned workspace, with the undifferentiated
    not-found text (#138 blocker-1 + blocker-3)."""
    mock_db = AsyncMock()
    mock_db.get_document_by_id = AsyncMock(return_value=_foreign_doc("doc-x", "ws-b"))
    # Scoped-key validation (#138 blocker-2) consults ONLY
    # user_owns_workspace_in_mongo, never get_user_workspace_ids — mock the
    # former, not the latter, or this test would pass for the wrong reason.
    mock_db.user_owns_workspace_in_mongo = AsyncMock(return_value=True)

    with _patch_db(mock_db):
        document, workspace_ids, error = await mcp_server._resolve_document_for_user(
            _scoped_key("ws-a"), "doc-x"
        )

    assert document is None
    assert workspace_ids == ["ws-a"]  # the key's authorised set, not ["ws-a", "ws-b"]
    assert error == "Error: Document 'doc-x' not found"


@pytest.mark.asyncio
async def test_delete_document_scoped_key_cannot_cross_to_owned_workspace() -> None:
    """Highest-impact instance: a scoped key must NOT be able to permanently
    delete a document in a workspace it isn't bound to, even one its owner
    also owns (#138 blocker-3). Asserts the deletion pipeline is never
    reached — ``get_document_upload_fields`` is delete_document_everywhere's
    first call, so its absence proves the rejection happened first."""
    mock_db = AsyncMock()
    mock_db.get_document_by_id = AsyncMock(return_value=_foreign_doc("doc-x", "ws-b"))
    # Scoped-key validation (#138 blocker-2) consults ONLY
    # user_owns_workspace_in_mongo, never get_user_workspace_ids — mock the
    # former, not the latter, or this test would pass for the wrong reason.
    mock_db.user_owns_workspace_in_mongo = AsyncMock(return_value=True)

    with _patch_db(mock_db):
        result = await mcp_server._handle_delete_document(
            _scoped_key("ws-a"), {"document_id": "doc-x"}
        )

    assert result[0].text == "Error: Document 'doc-x' not found"
    mock_db.get_document_upload_fields.assert_not_awaited()


@pytest.mark.asyncio
async def test_refresh_stale_source_scoped_key_cannot_cross_to_owned_workspace() -> None:
    """A scoped key must NOT be able to re-trigger ingestion for a document in
    a workspace it isn't bound to (#138 blocker-3). No pending-reset and no
    MQ publish must happen."""
    mock_db = AsyncMock()
    mock_db.get_document_by_id = AsyncMock(return_value=_foreign_doc("doc-x", "ws-b"))
    # Scoped-key validation (#138 blocker-2) consults ONLY
    # user_owns_workspace_in_mongo, never get_user_workspace_ids — mock the
    # former, not the latter, or this test would pass for the wrong reason.
    mock_db.user_owns_workspace_in_mongo = AsyncMock(return_value=True)

    with _patch_db(mock_db):
        result = await mcp_server._handle_refresh_stale_source(
            _scoped_key("ws-a"), {"document_id": "doc-x"}
        )

    assert result[0].text == "Error: Document 'doc-x' not found"
    mock_db.get_document_upload_fields.assert_not_awaited()
    mock_db.create_or_reset_pending_document.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_context_blocks_scoped_key_from_document_in_other_owned_workspace() -> None:
    """get_document_context must refuse a scoped key reading a document in a
    different owned workspace (#138 blocker-3), with the same undifferentiated
    not-found text as the unscoped case above."""
    mock_db = AsyncMock()
    mock_db.get_document_by_id = AsyncMock(return_value=_foreign_doc("doc-x", "ws-b"))
    # Scoped-key validation (#138 blocker-2) consults ONLY
    # user_owns_workspace_in_mongo, never get_user_workspace_ids — mock the
    # former, not the latter, or this test would pass for the wrong reason.
    mock_db.user_owns_workspace_in_mongo = AsyncMock(return_value=True)
    mock_db.get_document_chunks_by_doc_id = AsyncMock(return_value=[])

    with _patch_db(mock_db):
        result = await mcp_server._handle_get_context(_scoped_key("ws-a"), {"document_id": "doc-x"})

    assert result[0].text == "Error: Document 'doc-x' not found"
    mock_db.get_document_chunks_by_doc_id.assert_not_called()


@pytest.mark.asyncio
async def test_report_feedback_scoped_key_limits_lookup_to_bound_workspace() -> None:
    """A scoped key's feedback event lookup must be limited to exactly its
    bound workspace, never the owner's full owned set (#138 blocker-3) —
    otherwise a scoped key could read/promote eval events captured in a
    workspace it isn't authorised for."""
    mock_db = AsyncMock()
    # Scoped-key validation (#138 blocker-2) consults ONLY
    # user_owns_workspace_in_mongo, never get_user_workspace_ids — mock the
    # former, not the latter, or this test would pass for the wrong reason.
    mock_db.user_owns_workspace_in_mongo = AsyncMock(return_value=True)
    mock_db.get_eval_event = AsyncMock(return_value=None)  # -> EventNotFoundError path

    with _patch_db(mock_db):
        result = await mcp_server._handle_report_feedback(
            _scoped_key("ws-a"), {"event_id": "evt-1", "verdict": "answered"}
        )

    mock_db.get_eval_event.assert_awaited_once_with(event_id="evt-1", workspace_ids=["ws-a"])
    assert "unknown or expired event_id" in result[0].text


@pytest.mark.asyncio
async def test_get_retrieval_health_blocks_scoped_key_from_other_owned_workspace() -> None:
    """get_retrieval_health must refuse a scoped key requesting a scorecard
    for a different owned workspace (#138 blocker-3), naming the key's own
    binding per the fix-1 wording, and must never build the scorecard."""
    mock_db = AsyncMock()
    # Scoped-key validation (#138 blocker-2) consults ONLY
    # user_owns_workspace_in_mongo, never get_user_workspace_ids — mock the
    # former, not the latter, or this test would pass for the wrong reason.
    mock_db.user_owns_workspace_in_mongo = AsyncMock(return_value=True)

    with (
        _patch_db(mock_db),
        patch.object(mcp_server, "build_scorecard", AsyncMock()) as mock_build,
    ):
        result = await mcp_server._handle_get_retrieval_health(
            _scoped_key("ws-a"), {"workspace_id": "ws-b"}
        )

    text = result[0].text
    assert "scoped to workspace 'ws-a'" in text
    assert "cannot access workspace 'ws-b'" in text
    mock_build.assert_not_awaited()


@pytest.mark.asyncio
async def test_upload_resolve_workspace_scoped_key_rejects_other_owned_workspace() -> None:
    """upload_document's workspace resolver must refuse a scoped key naming a
    different owned workspace as the upload target (#138 blocker-3)."""
    mock_db = AsyncMock()
    # Scoped-key validation (#138 blocker-2) consults ONLY
    # user_owns_workspace_in_mongo, never get_user_workspace_ids — mock the
    # former, not the latter, or this test would pass for the wrong reason.
    mock_db.user_owns_workspace_in_mongo = AsyncMock(return_value=True)

    with _patch_db(mock_db):
        workspace_id, error = await mcp_server._resolve_single_workspace_for_upload(
            _scoped_key("ws-a"), "ws-b"
        )

    assert workspace_id is None
    assert error == (
        "Error: API key is scoped to workspace 'ws-a' and cannot access workspace 'ws-b'"
    )


@pytest.mark.asyncio
async def test_upload_resolve_workspace_scoped_key_no_arg_narrows_to_binding() -> None:
    """Omitting workspace_id on upload must resolve a scoped key to exactly
    its bound workspace, never the owner's other workspace and never the
    "multiple workspaces, disambiguate" error a user-scoped key with two
    workspaces would get (#138 blocker-3)."""
    mock_db = AsyncMock()
    # Scoped-key validation (#138 blocker-2) consults ONLY
    # user_owns_workspace_in_mongo, never get_user_workspace_ids — mock the
    # former, not the latter, or this test would pass for the wrong reason.
    mock_db.user_owns_workspace_in_mongo = AsyncMock(return_value=True)

    with _patch_db(mock_db):
        workspace_id, error = await mcp_server._resolve_single_workspace_for_upload(
            _scoped_key("ws-a"), None
        )

    assert workspace_id == "ws-a"
    assert error is None
