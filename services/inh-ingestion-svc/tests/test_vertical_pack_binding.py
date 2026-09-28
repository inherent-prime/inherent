"""Workspace -> vertical pack binding at workflow-start time (inherent#390
follow-up).

Resolution happens in plain application code (trigger.py / api/app.py), from
the operator-configured WORKSPACE_VERTICAL_PACKS mapping -- never inside
workflow code (Temporal determinism, #38). Precedence: an explicit
vertical_pack already on the input (not exercised by the MQ trigger path
today, since DocumentUploadMessage has no such field -- these test the
resolution helper itself) > the mapping > None.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.temporal.trigger import TemporalWorkflowTrigger


# Override the package-level DB-dependent autouse fixture (tests/conftest.py)
# with a no-op -- this module's tests are pure/mocked, same pattern as
# tests/test_temporal_trigger.py.
@pytest.fixture(autouse=True)
def cleanup_test_data():
    yield


def _make_settings(workspace_vertical_packs: dict[str, str] | None = None):
    settings = MagicMock()
    settings.temporal_host = "localhost:7233"
    settings.temporal_namespace = "default"
    settings.temporal_task_queue = "ingestion"
    settings.workspace_vertical_packs = workspace_vertical_packs or {}
    return settings


def _ready_trigger(settings) -> TemporalWorkflowTrigger:
    trigger = TemporalWorkflowTrigger(settings)
    trigger._initialized = True
    trigger._client = MagicMock()
    trigger._client.start_workflow = AsyncMock(return_value=MagicMock())
    return trigger


@pytest.mark.asyncio
async def test_async_trigger_resolves_vertical_pack_from_mapping(sample_upload_message):
    sample_upload_message["workspace_id"] = "ws_abc"
    settings = _make_settings({"ws_abc": "handbook"})
    trigger = _ready_trigger(settings)

    await trigger.trigger_workflow_async(sample_upload_message)

    args, _kwargs = trigger._client.start_workflow.call_args
    workflow_input = args[1]
    assert workflow_input.vertical_pack == "handbook"


@pytest.mark.asyncio
async def test_async_trigger_unmapped_workspace_gets_none(sample_upload_message):
    sample_upload_message["workspace_id"] = "ws_unmapped"
    settings = _make_settings({"ws_abc": "handbook"})
    trigger = _ready_trigger(settings)

    await trigger.trigger_workflow_async(sample_upload_message)

    args, _kwargs = trigger._client.start_workflow.call_args
    workflow_input = args[1]
    assert workflow_input.vertical_pack is None


@pytest.mark.asyncio
async def test_async_trigger_empty_mapping_is_feature_off(sample_upload_message):
    """Default (no WORKSPACE_VERTICAL_PACKS) -> every workspace unaffected."""
    settings = _make_settings({})
    trigger = _ready_trigger(settings)

    await trigger.trigger_workflow_async(sample_upload_message)

    args, _kwargs = trigger._client.start_workflow.call_args
    workflow_input = args[1]
    assert workflow_input.vertical_pack is None


@pytest.mark.asyncio
async def test_sync_trigger_workflow_also_resolves_from_mapping(sample_upload_message):
    """trigger_workflow (the sync, wait-for-result path) -- covers trigger.py's
    OTHER DocumentIngestionInput construction site (api/app.py's own site is
    exercised by app.py's own test suite)."""
    settings = _make_settings({"ws_xyz": "support"})
    trigger = TemporalWorkflowTrigger(settings)
    trigger._initialized = True
    trigger._client = MagicMock()
    mock_handle = MagicMock()
    mock_handle.result = AsyncMock(return_value=MagicMock())
    trigger._client.start_workflow = AsyncMock(return_value=mock_handle)

    sample_upload_message["workspace_id"] = "ws_xyz"

    await trigger.trigger_workflow(sample_upload_message)

    args, _kwargs = trigger._client.start_workflow.call_args
    workflow_input = args[1]
    assert workflow_input.vertical_pack == "support"


@pytest.mark.asyncio
async def test_async_trigger_passes_through_source_url(sample_upload_message):
    """source_url (inherent#391) reaches DocumentIngestionInput unchanged --
    already sanitized by DocumentUploadMessage's own validator, so no
    resanitization happens here."""
    sample_upload_message["source_url"] = "https://drive.google.com/file/d/abc/view"
    settings = _make_settings({})
    trigger = _ready_trigger(settings)

    await trigger.trigger_workflow_async(sample_upload_message)

    args, _kwargs = trigger._client.start_workflow.call_args
    workflow_input = args[1]
    assert workflow_input.source_url == "https://drive.google.com/file/d/abc/view"


@pytest.mark.asyncio
async def test_sync_trigger_also_passes_through_source_url(sample_upload_message):
    """trigger_workflow's OWN DocumentIngestionInput construction site also
    threads source_url (see the sync/async pairing above)."""
    settings = _make_settings({})
    trigger = TemporalWorkflowTrigger(settings)
    trigger._initialized = True
    trigger._client = MagicMock()
    mock_handle = MagicMock()
    mock_handle.result = AsyncMock(return_value=MagicMock())
    trigger._client.start_workflow = AsyncMock(return_value=mock_handle)

    sample_upload_message["source_url"] = "https://drive.google.com/file/d/abc/view"

    await trigger.trigger_workflow(sample_upload_message)

    args, _kwargs = trigger._client.start_workflow.call_args
    workflow_input = args[1]
    assert workflow_input.source_url == "https://drive.google.com/file/d/abc/view"


@pytest.mark.asyncio
async def test_async_trigger_missing_source_url_defaults_to_none(sample_upload_message):
    """Every existing MQ message (no source_url at all) is unaffected."""
    settings = _make_settings({})
    trigger = _ready_trigger(settings)

    await trigger.trigger_workflow_async(sample_upload_message)

    args, _kwargs = trigger._client.start_workflow.call_args
    workflow_input = args[1]
    assert workflow_input.source_url is None
