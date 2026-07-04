"""Chunking config is resolved in the activity, not the workflow (#38).

The workflow called get_settings() inside @workflow.run — a Temporal
determinism anti-pattern. Config resolution now happens in the chunk_text
activity, which already reads settings; the workflow just passes the raw
(nullable) overrides through.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.temporal.activities.chunk import _chunk_text_inner
from src.temporal.models import ChunkTextInput


@pytest.mark.asyncio
async def test_activity_resolves_config_defaults_when_none():
    staging = MagicMock()
    staging.read_text.return_value = "Sentence one. Sentence two. Sentence three."
    staging.write_chunks = MagicMock()

    settings = MagicMock()
    settings.chunking_strategy = "tokens"
    settings.max_chunk_size = 100
    settings.chunk_overlap = 10
    settings.embedding_max_tokens = 256

    with (
        patch("src.temporal.shared_services.get_staging_service", return_value=staging),
        patch("src.config.settings.get_settings", return_value=settings),
    ):
        out = await _chunk_text_inner(
            ChunkTextInput(
                workflow_run_id="wf",
                document_id="d",
                strategy=None,
                max_chunk_size=None,
                chunk_overlap=None,
            )
        )

    assert out.chunk_count >= 1
    staging.write_chunks.assert_called_once()
