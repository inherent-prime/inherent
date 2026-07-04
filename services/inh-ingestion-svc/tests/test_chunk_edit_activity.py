"""Chunk-edit activity must keep provenance consistent (#9).

Editing a chunk previously updated only ``content`` and a naive word-count
``token_count``, leaving the stored ``content_hash`` (sha256 of the content,
the #41 verifiable-evidence hash) stale — so any re-hash check would flag a
legitimately edited chunk as tampered. The edit must recompute ``content_hash``
and use the same ``estimate_tokens`` as the store path.
"""

from __future__ import annotations

import hashlib
from unittest.mock import MagicMock, patch

import pytest

from src.temporal.activities.chunk import estimate_tokens
from src.temporal.activities.chunk_edit import update_chunk_postgresql
from src.temporal.models import ChunkEditInput


@pytest.mark.asyncio
async def test_update_recomputes_content_hash_and_token_count():
    content = "The quick brown fox was edited into something longer."

    conn = MagicMock()
    result = MagicMock()
    result.rowcount = 1
    conn.execute.return_value = result

    cm = MagicMock()
    cm.__enter__.return_value = conn
    cm.__exit__.return_value = False
    db = MagicMock()
    db.engine.connect.return_value = cm

    with patch("src.temporal.shared_services.get_db_service", return_value=db):
        await update_chunk_postgresql(
            ChunkEditInput(document_id="doc-1", chunk_index=0, content=content)
        )

    sql, params = conn.execute.call_args.args
    assert "content_hash" in str(sql), "UPDATE must set content_hash"
    assert params["content"] == content
    assert params["content_hash"] == hashlib.sha256(content.encode("utf-8")).hexdigest()
    # token_count must match the store-path estimator, not a naive word split.
    assert params["token_count"] == estimate_tokens(content)
