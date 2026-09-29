"""Documents are stored under the workspace OWNER's tenant (prime#331).

Both producers must end up in the owner's tenant: the public API already
sends the owner as ``user_id`` (plus ``uploaded_by``); the platform's own
``document.uploaded`` events send the uploading member as ``user_id`` and
ingestion resolves the owner from Mongo ``workspaces.user_id``. A workspace
missing from Mongo (a standalone deployment) keeps the event's ``user_id``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.services.tenant_owner import resolve_tenant_user_id
from src.temporal.trigger import TemporalWorkflowTrigger

OWNER, MEMBER = "user-owner", "user-member"


@pytest.fixture(autouse=True)
def cleanup_test_data():
    """No-op override of the package-level DB fixture: pure/mocked tests."""
    yield


def _settings(*, lookup: bool = True):
    settings = MagicMock()
    settings.temporal_host = "localhost:7233"
    settings.temporal_namespace = "default"
    settings.temporal_task_queue = "ingestion"
    settings.workspace_vertical_packs = {}
    settings.mongodb_uri = "mongodb://mongo"
    settings.mongodb_db_name = "main"
    settings.workspace_owner_lookup_enabled = lookup
    return settings


def _mongo(workspace_doc):
    """A stand-in motor client whose `workspaces.find_one` returns `workspace_doc`."""
    collection = MagicMock()
    collection.find_one = AsyncMock(return_value=workspace_doc)
    db = MagicMock()
    db.__getitem__.return_value = collection
    client = MagicMock()
    client.__getitem__.return_value = db
    return client, collection


def _patch_mongo(workspace_doc=None, *, error: Exception | None = None):
    client, collection = _mongo(workspace_doc)
    if error is not None:
        collection.find_one = AsyncMock(side_effect=error)
    return patch("src.services.tenant_owner.get_mongo_client", return_value=client)


async def test_member_event_resolves_to_the_workspace_owner():
    with _patch_mongo({"_id": "ws", "user_id": OWNER}):
        assert await resolve_tenant_user_id(_settings(), "ws", MEMBER) == OWNER


async def test_owner_event_resolves_to_itself():
    with _patch_mongo({"_id": "ws", "user_id": OWNER}):
        assert await resolve_tenant_user_id(_settings(), "ws", OWNER) == OWNER


async def test_object_id_owner_is_stringified():
    from bson import ObjectId

    oid = ObjectId()
    with _patch_mongo({"_id": "ws", "user_id": oid}):
        assert await resolve_tenant_user_id(_settings(), "ws", MEMBER) == str(oid)


async def test_workspace_missing_from_mongo_keeps_the_event_user():
    with _patch_mongo(None):
        assert await resolve_tenant_user_id(_settings(), "ws", MEMBER) == MEMBER


async def test_lookup_disabled_never_touches_mongo():
    with patch("src.services.tenant_owner.get_mongo_client", side_effect=AssertionError):
        assert await resolve_tenant_user_id(_settings(lookup=False), "ws", MEMBER) == MEMBER


async def test_mongo_failure_propagates_instead_of_filing_into_a_private_tenant():
    with _patch_mongo(error=RuntimeError("mongo down")):
        with pytest.raises(RuntimeError):
            await resolve_tenant_user_id(_settings(), "ws", MEMBER)


def _ready_trigger(settings) -> TemporalWorkflowTrigger:
    trigger = TemporalWorkflowTrigger(settings)
    trigger._initialized = True
    trigger._client = MagicMock()
    handle = MagicMock()
    handle.result = AsyncMock(return_value=MagicMock())
    trigger._client.start_workflow = AsyncMock(return_value=handle)
    return trigger


def _workflow_input(trigger):
    args, _kwargs = trigger._client.start_workflow.call_args
    return args[1]


@pytest.mark.parametrize("method", ["trigger_workflow_async", "trigger_workflow"])
async def test_platform_event_from_a_member_lands_in_the_owner_tenant(
    method, sample_upload_message
):
    """Producer = the platform: event user_id is the uploading member."""
    sample_upload_message["user_id"] = MEMBER
    trigger = _ready_trigger(_settings())
    with _patch_mongo({"_id": "ws", "user_id": OWNER}):
        await getattr(trigger, method)(sample_upload_message)

    workflow_input = _workflow_input(trigger)
    assert workflow_input.user_id == OWNER  # tenant
    assert workflow_input.uploaded_by == MEMBER  # attribution


@pytest.mark.parametrize("method", ["trigger_workflow_async", "trigger_workflow"])
async def test_public_api_event_already_in_owner_tenant_keeps_its_uploader(
    method, sample_upload_message
):
    """Producer = public API: user_id is already the owner, uploaded_by the member."""
    sample_upload_message["user_id"] = OWNER
    sample_upload_message["uploaded_by"] = MEMBER
    trigger = _ready_trigger(_settings())
    with _patch_mongo({"_id": "ws", "user_id": OWNER}):
        await getattr(trigger, method)(sample_upload_message)

    workflow_input = _workflow_input(trigger)
    assert workflow_input.user_id == OWNER
    assert workflow_input.uploaded_by == MEMBER


@pytest.mark.parametrize("method", ["trigger_workflow_async", "trigger_workflow"])
async def test_standalone_workspace_keeps_todays_behaviour(method, sample_upload_message):
    """No control-plane record: tenant is the event's user, uploader the same."""
    sample_upload_message["user_id"] = MEMBER
    trigger = _ready_trigger(_settings())
    with _patch_mongo(None):
        await getattr(trigger, method)(sample_upload_message)

    workflow_input = _workflow_input(trigger)
    assert workflow_input.user_id == MEMBER
    assert workflow_input.uploaded_by == MEMBER


def test_ingest_route_resolves_the_owner_and_keeps_the_body_user_as_uploader():
    from tests.test_api import (
        _INGEST_PAYLOAD,
        VALID_API_KEY,
        _FakeWorkflowResult,
        _make_mock_settings,
    )

    settings = _make_mock_settings()
    temporal_client = AsyncMock()
    handle = AsyncMock()
    handle.result = AsyncMock(return_value=_FakeWorkflowResult())
    temporal_client.start_workflow = AsyncMock(return_value=handle)

    with (
        patch("src.api.app.TemporalWorkerManager") as manager_cls,
        patch("src.api.auth.get_settings", return_value=settings),
        patch("src.api.app.resolve_tenant_user_id", AsyncMock(return_value=OWNER)),
    ):
        manager = manager_cls.return_value
        manager.start = AsyncMock()
        manager.stop = AsyncMock()
        manager.get_client = AsyncMock(return_value=temporal_client)
        manager.is_running = True

        from src.api.app import create_app

        with TestClient(create_app(settings)) as client:
            response = client.post(
                "/ingest",
                json={**_INGEST_PAYLOAD, "user_id": MEMBER},
                headers={"X-API-Key": VALID_API_KEY},
            )

    assert response.status_code == 202
    workflow_input = temporal_client.start_workflow.call_args.args[1]
    assert workflow_input.user_id == OWNER
    assert workflow_input.uploaded_by == MEMBER
