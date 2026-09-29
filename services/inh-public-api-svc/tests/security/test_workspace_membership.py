"""Workspace membership + role rules (prime#331).

Unlike the mock-heavy isolation tests, these drive the REAL
``DatabaseService`` methods, ``get_authorized_workspace_ids``,
``_resolve_workspace`` (REST) and the MCP handlers against a small in-memory
stand-in for the Mongo ``workspaces`` collection whose documents can be
edited mid-test. That is what lets "remove the member -> the very next
request is refused" be tested for real instead of asserted by a mock.

Rule under test (see ``src/services/workspace_roles.py``):
owner/admin/member = read + search + write, viewer = read + search, a stranger
= nothing, and a workspace document without ``members`` behaves as before.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from src.mcp_server import server as mcp_server
from src.models.api_key import APIKeyInfo
from src.models.document import Document
from src.services.auth import _resolve_workspace, get_authorized_workspace_ids
from src.services.database import DatabaseService

pytestmark = pytest.mark.security

OWNER, ADMIN, MEMBER, VIEWER, STRANGER = "u-owner", "u-admin", "u-member", "u-viewer", "u-stranger"


# --------------------------------------------------------------------------- #
# In-memory Mongo stand-in: just the filter shapes DatabaseService emits.
# --------------------------------------------------------------------------- #
def _matches(doc: dict[str, Any], flt: dict[str, Any]) -> bool:
    for key, cond in flt.items():
        if key == "$or":
            if not any(_matches(doc, sub) for sub in cond):
                return False
        elif isinstance(cond, dict) and "$in" in cond:
            if doc.get(key) not in cond["$in"]:
                return False
        elif isinstance(cond, dict) and "$exists" in cond:
            if (key in doc) != cond["$exists"]:
                return False
        elif isinstance(cond, dict) and "$elemMatch" in cond:
            if not any(_matches(m, cond["$elemMatch"]) for m in doc.get(key) or []):
                return False
        elif doc.get(key) != cond:
            return False
    return True


class _Cursor:
    def __init__(self, docs: list[dict[str, Any]]):
        self._docs = docs

    def limit(self, _n: int) -> "_Cursor":
        return self

    def __aiter__(self):
        self._it = iter(self._docs)
        return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


class _Collection:
    def __init__(self, docs: list[dict[str, Any]]):
        self.docs = docs
        self.fail = False

    def find(self, flt, _projection=None):
        if self.fail:
            raise RuntimeError("mongo down")
        return _Cursor([d for d in self.docs if _matches(d, flt)])

    async def find_one(self, flt):
        if self.fail:
            raise RuntimeError("mongo down")
        return next((d for d in self.docs if _matches(d, flt)), None)


class _MongoDb:
    def __init__(self, collection: _Collection):
        self._collection = collection

    def __getitem__(self, _name: str):
        return self._collection


class _MongoClient:
    def __init__(self, collection: _Collection):
        self._db = _MongoDb(collection)

    def __getitem__(self, _name: str):
        return self._db


class _PgRow:
    def __init__(self, workspace_id: str):
        self.workspace_id = workspace_id


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _PgSession:
    def __init__(self, rows):
        self._rows = rows

    async def execute(self, *_a, **_k):

        return _Result(self._rows)

    async def close(self):
        pass


class _PgCM:
    def __init__(self, rows):
        self._rows = rows

    async def __aenter__(self):
        return _PgSession(self._rows)

    async def __aexit__(self, *_exc):
        return False


def _member(user_id: str, role: str) -> dict[str, Any]:
    return {"user_id": user_id, "role": role, "added_at": None, "added_by": OWNER}


class _World:
    """A workspaces collection + optional Postgres upload history."""

    def __init__(self, docs: list[dict[str, Any]], pg_history: dict[str, list[str]] | None = None):
        self.collection = _Collection(docs)
        self.database = DatabaseService()
        history = pg_history or {}
        self.database.session_factory = lambda: _PgCM(
            [_PgRow(ws) for ws in history.get("rows", [])]
        )

    @contextmanager
    def active(self):
        with (
            patch(
                "src.services.mongo_client.get_mongo_client",
                return_value=_MongoClient(self.collection),
            ),
            patch("src.services.auth.get_database", AsyncMock(return_value=self.database)),
            patch.object(mcp_server, "get_database", AsyncMock(return_value=self.database)),
        ):
            yield


def _workspace(members: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    doc: dict[str, Any] = {"_id": "ws-1", "user_id": OWNER}
    if members is not None:
        doc["members"] = members
    return doc


def _team_world() -> _World:
    return _World(
        [
            _workspace(
                [
                    _member(ADMIN, "admin"),
                    _member(MEMBER, "member"),
                    _member(VIEWER, "viewer"),
                ]
            )
        ]
    )


def _key(user_id: str, *, workspace_id: str | None = None, write: bool = True) -> APIKeyInfo:
    permissions = ["read", "search"] + (["write"] if write else [])
    return APIKeyInfo(
        key_id=f"key-{user_id}",
        user_id=user_id,
        workspace_id=workspace_id,
        permissions=permissions,
        rate_limit=100,
        expires_at=None,
        status="active",
    )


ROLE_USERS = [(OWNER, True), (ADMIN, True), (MEMBER, True), (VIEWER, False), (STRANGER, None)]


# --------------------------------------------------------------------------- #
# Authorised set (shared by REST and MCP): read vs write
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("user_id, can_write", ROLE_USERS)
@pytest.mark.parametrize("scoped", [False, True], ids=["user-key", "scoped-key"])
async def test_authorized_ids_follow_the_role_matrix(user_id, can_write, scoped):
    world = _team_world()
    key = _key(user_id, workspace_id="ws-1" if scoped else None)
    with world.active():
        readable = await get_authorized_workspace_ids(key, world.database)
        writable = await get_authorized_workspace_ids(key, world.database, permission="write")

    if can_write is None:  # stranger: nothing at all
        assert readable == [] and writable == []
    else:
        assert readable == ["ws-1"]
        assert writable == (["ws-1"] if can_write else [])


async def test_workspace_without_members_field_behaves_as_before():
    world = _World([_workspace(members=None)])
    with world.active():
        owner = await get_authorized_workspace_ids(_key(OWNER), world.database, permission="write")
        other = await get_authorized_workspace_ids(_key(MEMBER), world.database)
        scoped_other = await get_authorized_workspace_ids(
            _key(MEMBER, workspace_id="ws-1"), world.database
        )
    assert owner == ["ws-1"]
    assert other == [] and scoped_other == []


async def test_unknown_role_grants_nothing():
    world = _World([_workspace([_member(MEMBER, "superuser")])])
    with world.active():
        assert await get_authorized_workspace_ids(_key(MEMBER), world.database) == []


async def test_object_id_style_member_ids_match():
    """Members written by a Mongoose-style writer hold an ObjectId, while the
    key's user_id is its hex string."""
    from bson import ObjectId

    oid = ObjectId()
    world = _World([{"_id": "ws-1", "user_id": OWNER, "members": [_member(oid, "member")]}])
    with world.active():
        role = await world.database.get_workspace_role_in_mongo(str(oid), "ws-1")
        listed = await world.database.get_user_workspace_ids(str(oid))
    assert role == "member"
    assert listed == ["ws-1"]


# --------------------------------------------------------------------------- #
# Removal revokes on the very next request (no caching)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("scoped", [False, True], ids=["user-key", "scoped-key"])
async def test_removing_a_member_revokes_access_on_the_next_request(scoped):
    world = _team_world()
    key = _key(MEMBER, workspace_id="ws-1" if scoped else None)
    with world.active():
        assert await _resolve_workspace(key, "ws-1", required=True, permission="write")
        world.collection.docs[0]["members"] = [
            m for m in world.collection.docs[0]["members"] if m["user_id"] != MEMBER
        ]
        with pytest.raises(HTTPException) as exc:
            await _resolve_workspace(key, "ws-1", required=True, permission="write")
        assert exc.value.status_code == 403
        with pytest.raises(HTTPException):
            await _resolve_workspace(key, "ws-1", required=False)


async def test_removed_member_is_not_readmitted_by_their_upload_history():
    """The Postgres fallback lists every workspace the user ever uploaded to.
    For a membership-managed workspace that must not undo the removal."""
    world = _World([_workspace([])], pg_history={"rows": ["ws-1"]})
    with world.active():
        assert await world.database.get_user_workspace_ids(MEMBER) == []


async def test_legacy_workspace_keeps_the_postgres_fallback_for_its_owner():
    """A document with no ``members`` at all is legacy data: unchanged."""
    world = _World([{"_id": "ws-legacy", "user_id": "someone-else"}], {"rows": ["ws-legacy"]})
    with world.active():
        assert await world.database.get_user_workspace_ids(OWNER) == ["ws-legacy"]


async def test_mongo_failure_on_a_binding_or_write_check_raises_not_grants():
    world = _team_world()
    world.collection.fail = True
    with world.active():
        with pytest.raises(RuntimeError):
            await get_authorized_workspace_ids(
                _key(VIEWER, workspace_id="ws-1"), world.database, permission="write"
            )
        with pytest.raises(RuntimeError):
            await get_authorized_workspace_ids(_key(VIEWER), world.database, permission="write")


# --------------------------------------------------------------------------- #
# REST: the single write choke point (_resolve_workspace, permission="write")
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("scoped", [False, True], ids=["user-key", "scoped-key"])
@pytest.mark.parametrize("user_id", [OWNER, ADMIN, MEMBER])
async def test_rest_write_allowed_for_owner_admin_member(user_id, scoped):
    world = _team_world()
    key = _key(user_id, workspace_id="ws-1" if scoped else None)
    with world.active():
        resolved = await _resolve_workspace(key, "ws-1", required=True, permission="write")
    assert resolved.workspace_id == "ws-1"


@pytest.mark.parametrize("scoped", [False, True], ids=["user-key", "scoped-key"])
async def test_rest_viewer_write_denied_even_with_a_write_key(scoped):
    world = _team_world()
    key = _key(VIEWER, workspace_id="ws-1" if scoped else None, write=True)
    with world.active():
        with pytest.raises(HTTPException) as exc:
            await _resolve_workspace(key, "ws-1", required=True, permission="write")
        # ...but the same viewer can read and search the workspace.
        resolved = await _resolve_workspace(key, "ws-1", required=False)
    assert exc.value.status_code == 403
    assert "viewer" in exc.value.detail
    assert resolved.workspace_id == "ws-1"


async def test_rest_stranger_gets_the_usual_denial_not_the_viewer_message():
    world = _team_world()
    with world.active():
        with pytest.raises(HTTPException) as exc:
            await _resolve_workspace(_key(STRANGER), "ws-1", required=True, permission="write")
    assert exc.value.status_code == 403
    assert "viewer" not in exc.value.detail


async def test_rest_write_dependency_uses_the_write_role_rule():
    """resolve_workspace_write (what every write route depends on) is the one
    place REST applies the role rule."""
    from src.services.auth import resolve_workspace_write

    world = _team_world()
    with world.active():
        with pytest.raises(HTTPException) as exc:
            await resolve_workspace_write(_key(VIEWER), "ws-1")
        ok = await resolve_workspace_write(_key(MEMBER), "ws-1")
    assert exc.value.status_code == 403
    assert ok.workspace_id == "ws-1"


async def test_rest_viewer_of_one_workspace_and_owner_of_another_resolves_the_owned_one():
    world = _World(
        [
            _workspace([_member(VIEWER, "viewer")]),
            {"_id": "ws-2", "user_id": VIEWER},
        ]
    )
    with world.active():
        resolved = await _resolve_workspace(_key(VIEWER), None, required=True, permission="write")
    assert resolved.workspace_id == "ws-2"


# --------------------------------------------------------------------------- #
# MCP: every write tool is denied for a viewer; read/search tools are not
# --------------------------------------------------------------------------- #
def _doc() -> Document:
    now = __import__("datetime").datetime.now()
    return Document(
        id="doc-1",
        name="a.txt",
        workspace_id="ws-1",
        source_type="upload",
        mime_type="text/plain",
        size_bytes=1,
        chunk_count=1,
        status="processed",
        created_at=now,
        updated_at=now,
    )


WRITE_TOOL_ARGS: dict[str, dict[str, Any]] = {
    "delete_document": {"document_id": "doc-1"},
    "refresh_stale_source": {"document_id": "doc-1"},
    "create_chunk": {"document_id": "doc-1", "content": "x"},
    "edit_chunk": {"document_id": "doc-1", "chunk_index": 0, "content": "x"},
    "delete_chunk": {"document_id": "doc-1", "chunk_index": 0},
    "upload_document": {"filename": "a.txt", "content": "hello", "workspace_id": "ws-1"},
}


async def test_every_mcp_write_tool_is_covered_by_the_viewer_test():
    """A new write tool must be added to WRITE_TOOL_ARGS (and so to the viewer
    denial test below) or this fails."""
    write_tools = {n for n, t in mcp_server._TOOLS.items() if t.permission == "write"}
    assert write_tools == set(WRITE_TOOL_ARGS)


@pytest.mark.parametrize("tool_name", sorted(WRITE_TOOL_ARGS))
async def test_mcp_viewer_write_tools_are_denied_before_any_side_effect(tool_name):
    world = _team_world()
    tool = mcp_server._TOOLS[tool_name]
    side_effects = AsyncMock()
    with (
        world.active(),
        patch.object(world.database, "get_document_by_id", AsyncMock(return_value=_doc())),
        patch("src.mcp_server.server.intake_document", side_effects),
        patch("src.services.deletion.delete_document_everywhere", side_effects),
        patch("src.services.chunk_writes.create_chunk_everywhere", side_effects),
    ):
        result = await tool.handler(_key(VIEWER), dict(WRITE_TOOL_ARGS[tool_name]))
    text = result[0].text
    assert "viewer" in text and text.startswith("Error:")
    side_effects.assert_not_called()


async def test_mcp_member_upload_is_allowed():
    world = _team_world()
    intake = AsyncMock(return_value=MagicMock(model_dump_json=lambda: "{}"))
    with world.active(), patch("src.mcp_server.server.intake_document", intake):
        await mcp_server._handle_upload_document(
            _key(MEMBER), {"filename": "a.txt", "content": "hi", "workspace_id": "ws-1"}
        )
    intake.assert_awaited_once()


async def test_mcp_viewer_upload_without_workspace_id_is_denied():
    world = _team_world()
    with world.active():
        result = await mcp_server._handle_upload_document(
            _key(VIEWER), {"filename": "a.txt", "content": "hi"}
        )
    assert result[0].text.startswith("Error:")


async def test_mcp_viewer_can_still_resolve_the_workspace_for_read_and_search():
    world = _team_world()
    with world.active():
        ids, error = await mcp_server._get_workspace_ids(_key(VIEWER), "ws-1")
        scoped_ids, scoped_error = await mcp_server._get_workspace_ids(
            _key(VIEWER, workspace_id="ws-1"), None
        )
    assert (ids, error) == (["ws-1"], None)
    assert (scoped_ids, scoped_error) == (["ws-1"], None)


async def test_mcp_stranger_on_a_document_still_gets_the_undifferentiated_not_found():
    world = _team_world()
    with (
        world.active(),
        patch.object(world.database, "get_document_by_id", AsyncMock(return_value=_doc())),
    ):
        result = await mcp_server._handle_delete_document(_key(STRANGER), {"document_id": "doc-1"})
    assert result[0].text == "Error: Document 'doc-1' not found"
