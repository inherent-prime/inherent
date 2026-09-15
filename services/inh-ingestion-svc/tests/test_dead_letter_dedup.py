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


class TestDedupKey:
    """#363: `dedup_key` widens the dedup key from (document_id,
    workflow_run_id) to (document_id, workflow_run_id, dedup_key) -- see
    migration 021 -- so a workflow that can dead-letter MORE THAN ONCE per
    run (ConversationMemoryWorkflow) doesn't have its second failed batch
    silently collide with (and get dropped by) the first's row. Default ''
    must keep DocumentIngestionWorkflow's existing at-most-one-per-run
    dedup behavior byte-identical to before this parameter existed.
    """

    @pytest.mark.asyncio
    async def test_default_dedup_key_is_empty_string(self):
        """Callers that never pass `dedup_key` (every DocumentIngestionWorkflow
        call site, unchanged by #363) must insert '' -- not NULL, which
        would behave differently under Postgres's unique-index NULL
        semantics (distinct rows) than the pre-#363 (document_id,
        workflow_run_id)-only index intended."""
        session = MagicMock()
        inserted = MagicMock()
        inserted.scalar_one_or_none.return_value = 1
        session.execute.return_value = inserted

        await _add(_db(session))

        stmt = session.execute.call_args[0][0]
        assert stmt._values["dedup_key"].value == ""

    @pytest.mark.asyncio
    async def test_dedup_key_is_threaded_into_conflict_target_and_values(self):
        """A conversation-flush caller's `dedup_key` must reach both the
        inserted row's value AND the ON CONFLICT target -- otherwise two
        genuinely different batches on the same (document_id,
        workflow_run_id) would still collide on the OLD 2-column target."""
        session = MagicMock()
        inserted = MagicMock()
        inserted.scalar_one_or_none.return_value = 99
        session.execute.return_value = inserted

        db = _db(session)
        job_id = await db.add_dead_letter_job(
            document_id="conv-doc-1",
            workspace_id="w",
            user_id="u",
            workflow_run_id="run-1",
            original_message={},
            error_message="e",
            error_type="conversation_flush_redact_turns_failed",
            dedup_key="turn-abc",
        )

        assert job_id == 99
        stmt = session.execute.call_args[0][0]
        assert stmt._values["dedup_key"].value == "turn-abc"
        # ON CONFLICT target includes dedup_key -- a second call with a
        # DIFFERENT dedup_key (same document_id/workflow_run_id) targets a
        # different unique-index tuple and so cannot be dropped by this one.
        assert "dedup_key" in str(stmt._post_values_clause)

    @pytest.mark.asyncio
    async def test_conflict_lookup_is_scoped_by_dedup_key_too(self):
        """The ON-CONFLICT-DO-NOTHING fallback SELECT (used when THIS exact
        batch's write is retried) must also filter on dedup_key -- otherwise
        it could return a DIFFERENT batch's row (same document_id/
        workflow_run_id, different dedup_key) as if it were this one's."""
        session = MagicMock()
        insert_res = MagicMock()
        insert_res.scalar_one_or_none.return_value = None
        select_res = MagicMock()
        select_res.scalar_one_or_none.return_value = 5
        session.execute.side_effect = [insert_res, select_res]

        db = _db(session)
        await db.add_dead_letter_job(
            document_id="conv-doc-1",
            workspace_id="w",
            user_id="u",
            workflow_run_id="run-1",
            original_message={},
            error_message="e",
            error_type="t",
            dedup_key="turn-abc",
        )

        select_stmt = session.execute.call_args_list[1][0][0]
        where_compiled = str(
            select_stmt.whereclause.compile(compile_kwargs={"literal_binds": True})
        )
        assert "dead_letter_jobs.dedup_key = 'turn-abc'" in where_compiled
