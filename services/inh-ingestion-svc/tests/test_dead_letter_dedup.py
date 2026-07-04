"""Dead-letter jobs must be deduplicated per (document_id, workflow_run_id) (#24).

A record-retry (insert commits, ack lost, retry) must not create a second row
for the same run — the retry API could otherwise re-ingest the document twice.
"""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest

from src.config.settings import Settings
from src.services.database import DatabaseService


def _db(session) -> DatabaseService:
    db = DatabaseService.__new__(DatabaseService)
    DatabaseService.__init__(db, Settings.model_construct())
    db.engine = MagicMock()

    @contextmanager
    def _gs():
        yield session

    db.get_session = _gs
    return db


async def _add(db):
    return await db.add_dead_letter_job(
        document_id="d",
        workspace_id="w",
        user_id="u",
        workflow_run_id="run-1",
        original_message={},
        error_message="e",
        error_type="t",
    )


@pytest.mark.asyncio
async def test_returns_new_id_on_insert():
    session = MagicMock()
    inserted = MagicMock()
    inserted.scalar_one_or_none.return_value = 42
    session.execute.return_value = inserted

    assert await _add(_db(session)) == 42
    assert session.execute.call_count == 1  # insert only, no conflict lookup


@pytest.mark.asyncio
async def test_conflict_returns_existing_id_without_second_row():
    session = MagicMock()
    insert_res = MagicMock()
    insert_res.scalar_one_or_none.return_value = None  # ON CONFLICT DO NOTHING
    select_res = MagicMock()
    select_res.scalar_one_or_none.return_value = 7  # existing row's id
    session.execute.side_effect = [insert_res, select_res]

    assert await _add(_db(session)) == 7
    assert session.execute.call_count == 2  # insert (no-op) + existing-id lookup
