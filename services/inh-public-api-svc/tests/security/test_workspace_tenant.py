"""Owner-as-tenant, caller-as-attribution (prime#331).

Every member of a workspace must see the whole workspace, so vector/context
access and stored documents use the workspace OWNER's tenant, never the
individual caller's. Who actually did something (audit, eval capture,
``uploaded_by``) stays the real caller. Driven against the in-memory Mongo
stand-in from test_workspace_membership so the owner really is looked up.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.api.v1 import search as rest_search
from src.mcp_server import server as mcp_server
from src.services import conversation_intake, document_intake
from src.services.data_plane import data_plane_user_id, data_plane_user_ids
from tests.security.test_workspace_membership import (
    ADMIN,
    MEMBER,
    OWNER,
    STRANGER,
    _key,
    _member,
    _team_world,
    _workspace,
    _World,
)

pytestmark = [pytest.mark.security, pytest.mark.real_data_plane]


# --------------------------------------------------------------------------- #
# The resolver
# --------------------------------------------------------------------------- #
async def test_every_role_resolves_to_the_owner():
    world = _team_world()
    with world.active():
        for caller in (OWNER, ADMIN, MEMBER):
            assert await data_plane_user_id(world.database, "ws-1", caller) == OWNER


async def test_workspace_missing_from_mongo_falls_back_to_the_callers_own_id():
    """Legacy/standalone workspace: reachable only as its legacy owner (membership
    comes from Mongo), so the caller's own id is today's tenant."""
    world = _World([])
    with world.active():
        assert await data_plane_user_id(world.database, "ws-legacy", "u-legacy") == "u-legacy"


async def test_resolves_many_workspaces_in_one_lookup():
    world = _World([_workspace([_member(MEMBER, "member")]), {"_id": "ws-2", "user_id": MEMBER}])
    with world.active():
        tenants = await data_plane_user_ids(world.database, ["ws-1", "ws-2", "ws-3"], MEMBER)
    assert tenants == {"ws-1": OWNER, "ws-2": MEMBER, "ws-3": MEMBER}


async def test_mongo_failure_raises_never_falls_back_to_the_callers_tenant():
    world = _team_world()
    world.collection.fail = True
    with world.active(), pytest.raises(RuntimeError):
        await data_plane_user_id(world.database, "ws-1", MEMBER)


async def test_owner_change_applies_on_the_next_request_no_cache():
    world = _team_world()
    with world.active():
        assert await data_plane_user_id(world.database, "ws-1", MEMBER) == OWNER
        world.collection.docs[0]["user_id"] = ADMIN
        assert await data_plane_user_id(world.database, "ws-1", MEMBER) == ADMIN


# --------------------------------------------------------------------------- #
# Search: tenant = owner, attribution = member
# --------------------------------------------------------------------------- #
def _search_response():
    return SimpleNamespace(results=[], total_results=0, processing_time_ms=1.0)


async def test_mcp_search_uses_the_owner_tenant_but_captures_the_member():
    world = _team_world()
    search_service = MagicMock()
    search_service.search = AsyncMock(return_value=_search_response())
    capture = AsyncMock(return_value="ev_1")
    with (
        world.active(),
        patch.object(mcp_server, "get_search_service", AsyncMock(return_value=search_service)),
        patch.object(mcp_server, "capture_enabled", return_value=True),
        patch.object(mcp_server, "capture_search_event", capture),
        patch.object(mcp_server, "purge_expired_events", AsyncMock()),
    ):
        _, _, error, event_id = await mcp_server._run_search(
            _key(MEMBER), {"query": "q", "workspace_id": "ws-1"}, capture=True
        )

    assert error is None and event_id == "ev_1"
    assert search_service.search.await_args.args[1] == OWNER  # tenant
    assert capture.await_args.kwargs["user_id"] == MEMBER  # attribution


async def test_rest_fan_out_searches_each_workspace_in_its_owners_tenant():
    world = _World([_workspace([_member(MEMBER, "member")]), {"_id": "ws-2", "user_id": MEMBER}])
    search_service = MagicMock()
    search_service.embed_query_vector = MagicMock(return_value=[0.1])
    search_service.search = AsyncMock(return_value=_search_response())
    request = SimpleNamespace(query="q")
    with (
        world.active(),
        patch.object(rest_search, "get_database", AsyncMock(return_value=world.database)),
    ):
        await rest_search._search_workspaces_concurrently(
            search_service, user_id=MEMBER, workspace_ids=["ws-1", "ws-2"], request=request
        )

    tenants = {
        c.kwargs["workspace_id"]: c.kwargs["user_id"] for c in search_service.search.await_args_list
    }
    assert tenants == {"ws-1": OWNER, "ws-2": MEMBER}


async def test_rest_context_expansion_is_scoped_to_the_owner_tenant():
    world = _team_world()
    expand = AsyncMock()
    request = SimpleNamespace(include_context=True, context_window=1)
    response = SimpleNamespace(results=[object()], total_tokens=0)
    with (
        world.active(),
        patch.object(rest_search, "get_database", AsyncMock(return_value=world.database)),
        patch("src.services.context_window.ContextWindowBuilder.expand", expand),
        patch.object(rest_search, "_compute_total_tokens", return_value=0),
    ):
        await rest_search._expand_context_and_total_tokens(response, request, "ws-1", MEMBER)

    assert expand.await_args.kwargs["user_id"] == OWNER


async def test_tool_profile_search_uses_the_owner_tenant():
    from src.mcp_server import tool_profiles

    world = _team_world()
    with (
        world.active(),
        patch.object(tool_profiles, "get_database", AsyncMock(return_value=world.database)),
    ):
        tenant = await tool_profiles.data_plane_user_id(
            await tool_profiles.get_database(), "ws-1", MEMBER
        )
    assert tenant == OWNER


async def test_eval_run_replays_in_the_owner_tenant():
    from src.api.v1 import evals

    world = _team_world()
    tasks = MagicMock()
    auth = SimpleNamespace(workspace_id="ws-1", key_info=_key(MEMBER))
    with world.active(), patch.object(evals, "start_run", AsyncMock(return_value="run-1")):
        result = await evals.start_eval_run(auth, world.database, MagicMock(), tasks, None)

    assert result["run_id"] == "run-1"
    assert tasks.add_task.call_args.kwargs["user_id"] == OWNER


# --------------------------------------------------------------------------- #
# Intake: stored in the owner's tenant, uploaded_by = the member
# --------------------------------------------------------------------------- #
async def test_member_upload_is_stored_in_the_owner_tenant_and_attributed_to_the_member():
    world = _team_world()
    storage = MagicMock()
    storage.generate_key.return_value = "ws-1/abc/a.txt"
    storage.upload_file = AsyncMock()
    storage.build_storage_url.return_value = "s3://b/ws-1/abc/a.txt"
    storage._bucket = "b"
    mq = AsyncMock()
    world.database.get_document_id_by_content_hash = AsyncMock(return_value=None)
    world.database.get_document_id_by_filename = AsyncMock(return_value=None)
    world.database.create_or_reset_pending_document = AsyncMock()
    with (
        world.active(),
        patch.object(document_intake, "get_storage_service", return_value=storage),
        patch.object(document_intake, "get_mq_service", AsyncMock(return_value=mq)),
    ):
        await document_intake.intake_document(
            database=world.database,
            workspace_id="ws-1",
            user_id=MEMBER,
            content_bytes=b"hello",
            filename="a.txt",
            content_type="text/plain",
        )

    row = world.database.create_or_reset_pending_document.await_args.kwargs
    assert (row["user_id"], row["uploaded_by"]) == (OWNER, MEMBER)
    message = mq.publish.await_args.args[1]
    assert (message["user_id"], message["uploaded_by"]) == (OWNER, MEMBER)


async def test_member_conversation_turns_use_the_owner_tenant_and_carry_the_member():
    world = _team_world()
    mq = AsyncMock()
    turn = SimpleNamespace(
        turn_id="t1", role="user", text="hi", ts="2026-01-01T00:00:00Z", client=None
    )
    with (
        world.active(),
        patch.object(conversation_intake, "get_database", AsyncMock(return_value=world.database)),
        patch.object(conversation_intake, "get_mq_service", AsyncMock(return_value=mq)),
    ):
        await conversation_intake.intake_turns(
            workspace_id="ws-1", user_id=MEMBER, external_id="conv", turns=[turn]
        )

    message = mq.publish.await_args.args[1]
    assert (message["user_id"], message["uploaded_by"]) == (OWNER, MEMBER)


async def test_refresh_preserves_the_original_uploader():
    """A refresh replays the stored row: same tenant, same uploaded_by."""
    world = _team_world()
    fields = {
        "document_id": "d",
        "workspace_id": "ws-1",
        "user_id": OWNER,
        "uploaded_by": MEMBER,
        "filename": "f",
        "original_filename": "f",
        "content_type": "text/plain",
        "size_bytes": 1,
        "storage_backend": "s3",
        "storage_path": "p",
    }
    mq = AsyncMock()
    world.database.get_document_upload_fields = AsyncMock(return_value=fields)
    world.database.create_or_reset_pending_document = AsyncMock()
    document = SimpleNamespace(workspace_id="ws-1", name="f")
    with (
        world.active(),
        patch.object(
            mcp_server, "_resolve_document_for_user", AsyncMock(return_value=(document, [], None))
        ),
        patch("src.services.mq.get_mq_service", AsyncMock(return_value=mq)),
    ):
        await mcp_server._handle_refresh_stale_source(_key(ADMIN), {"document_id": "d"})

    row = world.database.create_or_reset_pending_document.await_args.kwargs
    assert (row["user_id"], row["uploaded_by"]) == (OWNER, MEMBER)


async def test_stranger_still_cannot_resolve_a_tenant_by_reaching_the_workspace():
    """Owner lookup is not an access grant: authorization is unchanged."""
    from src.services.auth import get_authorized_workspace_ids

    world = _team_world()
    with world.active():
        assert await get_authorized_workspace_ids(_key(STRANGER), world.database) == []
