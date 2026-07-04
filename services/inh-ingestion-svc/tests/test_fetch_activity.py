"""fetch_document must not download remote objects just to get their size (#22).

The activity only validates existence and returns a size the workflow discards;
extract_text reads the content immediately afterwards. Reading the whole S3/GCS
object here doubles egress. It must use a stat/HEAD-based size instead.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.temporal.activities.fetch import fetch_document
from src.temporal.models import FetchDocumentInput


def _track_event_cm():
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=None)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


@patch("src.temporal.activities.fetch.track_event")
@patch("src.temporal.shared_services.get_storage_service")
@pytest.mark.asyncio
async def test_s3_fetch_uses_stat_not_full_read(mock_get_storage, mock_track):
    mock_track.return_value = _track_event_cm()

    backend = MagicMock()
    backend.file_exists.return_value = True
    backend.get_size.return_value = 4096
    backend.read_file = MagicMock(side_effect=AssertionError("must not download content"))
    storage = MagicMock()
    storage.get_backend.return_value = backend
    mock_get_storage.return_value = storage

    out = await fetch_document(
        FetchDocumentInput(
            document_id="d",
            storage_backend="s3",
            storage_path="p",
            storage_bucket="b",
            workflow_run_id="wf",
            workspace_id="ws",
        )
    )

    assert out.size_bytes == 4096
    backend.get_size.assert_called_once()
    backend.read_file.assert_not_called()
