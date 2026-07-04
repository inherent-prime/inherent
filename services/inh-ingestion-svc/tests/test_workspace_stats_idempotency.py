"""Workspace stats must be idempotent per workflow run (#7).

update_workspace_stats was a blind additive increment, so a Temporal retry or a
dead-letter reprocess of the same document double-counted document/chunk/size.
A ledger keyed on workflow_run_id makes the increment apply at most once.
"""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest

from src.services.database import DatabaseService


def _make_db(session) -> DatabaseService:
    db = object.__new__(DatabaseService)  # bypass connect/table setup
    db.engine = MagicMock()

    @contextmanager
    def _gs():
        yield session

    db.get_session = _gs
    return db


@pytest.mark.asyncio
async def test_stats_skipped_when_run_already_applied():
    session = MagicMock()
    # Ledger insert hits the ON CONFLICT -> rowcount 0 -> already applied.
    session.execute.return_value = MagicMock(rowcount=0)
    db = _make_db(session)

    result = await db.update_workspace_stats(
        "ws", document_delta=1, chunk_delta=2, size_delta=3, workflow_run_id="run-1"
    )

    assert result is False
    # Only the ledger insert ran; the UPDATE was skipped (no double count).
    assert session.execute.call_count == 1


@pytest.mark.asyncio
async def test_stats_applied_once_for_a_new_run():
    session = MagicMock()
    # Ledger insert succeeds (rowcount 1), then the UPDATE runs (rowcount 1).
    session.execute.side_effect = [MagicMock(rowcount=1), MagicMock(rowcount=1)]
    db = _make_db(session)

    result = await db.update_workspace_stats("ws", document_delta=1, workflow_run_id="run-1")

    assert result is True
    assert session.execute.call_count == 2


@pytest.mark.asyncio
async def test_backward_compatible_without_run_id():
    session = MagicMock()
    session.execute.return_value = MagicMock(rowcount=1)
    db = _make_db(session)

    # No workflow_run_id -> old unconditional behaviour (single UPDATE, no ledger).
    result = await db.update_workspace_stats("ws", document_delta=1)

    assert result is True
    assert session.execute.call_count == 1
