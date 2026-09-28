"""store_in_weaviate's chunk-reuse-detection wiring (inherent#394).

Confirms the opt-in gate (WORKSPACE_REUSE_DETECTION) and the best-effort
contract: a workspace not listed never calls reuse detection at all, an
opted-in workspace calls it with the right arguments, and a reuse-detection
failure never fails the activity (store_in_weaviate still reports success).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.temporal.models import StoreDocumentInput


@pytest.fixture(autouse=True)
def cleanup_test_data():
    """No-op override of the package-level DB-dependent autouse fixture --
    this module mocks every service boundary."""
    yield


def _store_input() -> StoreDocumentInput:
    return StoreDocumentInput(
        workflow_run_id="wf_1",
        document_id="doc_1",
        workspace_id="ws_reuse",
        user_id="user_1",
        filename="f.txt",
        original_filename="f.txt",
        content_type="text/plain",
        size_bytes=10,
        storage_backend="local",
        storage_path="storage/f.txt",
        text_length=10,
        processing_time_ms=5,
    )


def _wire_common_mocks(mock_get_staging, mock_get_weaviate, mock_get_db):
    mock_staging = MagicMock()
    mock_staging.read_chunks.return_value = [
        {
            "document_id": "doc_1",
            "content": "chunk text long enough to not be skipped as tiny",
            "chunk_index": 0,
            "start_char": 0,
            "end_char": 10,
        }
    ]
    mock_get_staging.return_value = mock_staging

    weaviate = MagicMock()
    weaviate.is_connected.return_value = True
    weaviate.delete_document_chunks_graceful = AsyncMock(return_value=(True, 0))
    weaviate.store_chunks_with_tenant = AsyncMock(return_value=None)
    mock_get_weaviate.return_value = weaviate

    mock_db = MagicMock()
    mock_db.record_ingestion_event = AsyncMock(return_value=None)
    mock_db.is_active_run = AsyncMock(return_value=True)
    mock_get_db.return_value = mock_db
    return weaviate, mock_db


@patch("src.temporal.shared_services.get_settings")
@patch("src.temporal.shared_services.get_db_service")
@patch("src.temporal.shared_services.get_weaviate_service")
@patch("src.temporal.shared_services.get_staging_service")
@pytest.mark.asyncio
async def test_reuse_detection_skipped_for_workspace_not_opted_in(
    mock_get_staging, mock_get_weaviate, mock_get_db, mock_get_settings
):
    from src.temporal.activities.store import store_in_weaviate

    _wire_common_mocks(mock_get_staging, mock_get_weaviate, mock_get_db)

    settings = MagicMock()
    settings.workspace_reuse_detection = set()  # nothing opted in
    mock_get_settings.return_value = settings

    with patch(
        "src.services.reuse_detection.detect_and_record_chunk_reuse", new=AsyncMock()
    ) as mock_detect:
        result = await store_in_weaviate(_store_input())

    assert result.success is True
    mock_detect.assert_not_called()


@patch("src.temporal.shared_services.get_settings")
@patch("src.temporal.shared_services.get_db_service")
@patch("src.temporal.shared_services.get_weaviate_service")
@patch("src.temporal.shared_services.get_staging_service")
@pytest.mark.asyncio
async def test_reuse_detection_runs_for_opted_in_workspace(
    mock_get_staging, mock_get_weaviate, mock_get_db, mock_get_settings
):
    from src.temporal.activities.store import store_in_weaviate

    weaviate, db = _wire_common_mocks(mock_get_staging, mock_get_weaviate, mock_get_db)

    settings = MagicMock()
    settings.workspace_reuse_detection = {"ws_reuse"}
    settings.reuse_similarity_threshold = 0.92
    settings.reuse_text_similarity_threshold = 0.7
    settings.reuse_top_k = 5
    settings.reuse_min_chunk_chars = 40
    mock_get_settings.return_value = settings

    with patch(
        "src.services.reuse_detection.detect_and_record_chunk_reuse",
        new=AsyncMock(return_value=1),
    ) as mock_detect:
        result = await store_in_weaviate(_store_input())

    assert result.success is True
    mock_detect.assert_awaited_once()
    _, kwargs = mock_detect.call_args
    assert kwargs["workspace_id"] == "ws_reuse"
    assert kwargs["document_id"] == "doc_1"
    assert kwargs["weaviate_service"] is weaviate
    assert kwargs["db_service"] is db


@patch("src.temporal.shared_services.get_settings")
@patch("src.temporal.shared_services.get_db_service")
@patch("src.temporal.shared_services.get_weaviate_service")
@patch("src.temporal.shared_services.get_staging_service")
@pytest.mark.asyncio
async def test_reuse_detection_failure_never_fails_ingestion(
    mock_get_staging, mock_get_weaviate, mock_get_db, mock_get_settings
):
    """Best-effort contract (#394): store_in_weaviate must still report
    success even if reuse detection blows up."""
    from src.temporal.activities.store import store_in_weaviate

    _wire_common_mocks(mock_get_staging, mock_get_weaviate, mock_get_db)

    settings = MagicMock()
    settings.workspace_reuse_detection = {"ws_reuse"}
    settings.reuse_similarity_threshold = 0.92
    settings.reuse_text_similarity_threshold = 0.7
    settings.reuse_top_k = 5
    settings.reuse_min_chunk_chars = 40
    mock_get_settings.return_value = settings

    with patch(
        "src.services.reuse_detection.detect_and_record_chunk_reuse",
        new=AsyncMock(side_effect=RuntimeError("boom")),
    ):
        result = await store_in_weaviate(_store_input())

    assert result.success is True
