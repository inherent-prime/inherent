"""Tests for dead-letter queue (DE-S021)."""

from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.config.settings import Settings
from src.services.database import DatabaseService


@pytest.fixture(autouse=True)
async def cleanup_test_data():
    yield


@pytest.fixture()
def db_service():
    yield None


class TestDeadLetterTriggerIntegration:
    """Test that workflow failures are recorded in dead_letter_jobs."""

    async def test_classify_error_extraction(self):
        """Test error classification helper exists on trigger."""
        from src.temporal.trigger import TemporalWorkflowTrigger

        trigger = TemporalWorkflowTrigger.__new__(TemporalWorkflowTrigger)
        assert hasattr(trigger, "_classify_error")

    async def test_classify_error_returns_string(self):
        """Test _classify_error returns a reasonable error type."""
        from src.temporal.trigger import TemporalWorkflowTrigger

        trigger = TemporalWorkflowTrigger.__new__(TemporalWorkflowTrigger)
        result = trigger._classify_error("Failed to extract text from PDF")
        assert isinstance(result, str)
        assert len(result) > 0


class TestDeadLetterDBTableExists:
    """Test dead_letter_jobs table is defined in DatabaseService."""

    def test_dead_letter_table_defined(self):
        """Verify dead_letter_jobs table exists in schema."""
        from sqlalchemy import MetaData

        db = DatabaseService.__new__(DatabaseService)
        db.metadata = MetaData()
        db._define_tables()
        assert hasattr(db, "dead_letter_jobs")
        col_names = [c.name for c in db.dead_letter_jobs.columns]
        assert "document_id" in col_names
        assert "workspace_id" in col_names
        assert "error_message" in col_names
        assert "original_message" in col_names
        assert "status" in col_names
        assert "retry_count" in col_names

    def test_dead_letter_methods_exist(self):
        """Verify CRUD methods exist on DatabaseService."""
        assert hasattr(DatabaseService, "add_dead_letter_job")
        assert hasattr(DatabaseService, "get_dead_letter_jobs")
        assert hasattr(DatabaseService, "get_dead_letter_job")
        assert hasattr(DatabaseService, "update_dead_letter_status")
        assert hasattr(DatabaseService, "increment_dead_letter_retry")


def _db_with_session(session) -> DatabaseService:
    """Build a DatabaseService whose get_session() yields a given mock
    session, without needing a real PostgreSQL connection (same pattern as
    tests/test_dead_letter_dedup.py's ``_db`` helper)."""
    db = DatabaseService.__new__(DatabaseService)
    DatabaseService.__init__(db, Settings.model_construct())
    db.engine = MagicMock()

    @contextmanager
    def _gs():
        yield session

    db.get_session = _gs
    return db


class TestResolveDeadLetterJobsForDocument:
    """DatabaseService.resolve_dead_letter_jobs_for_document (#249, #287).

    A successful ingestion of document X must resolve X's outstanding
    dead-letter rows -- both 'retrying' (a retry that has now succeeded,
    #249) and 'pending' (a failure nobody ever pressed Retry on, which a
    later successful ingestion of the same document_id has superseded,
    #287). 'abandoned' rows (an operator explicitly gave up) and already
    -'resolved' rows must be left alone.

    These tests pin the WHERE/SET clause shape via a mocked SQLAlchemy
    session rather than a live database.
    """

    def test_method_exists(self):
        assert hasattr(DatabaseService, "resolve_dead_letter_jobs_for_document")

    @pytest.mark.asyncio
    async def test_scopes_update_to_document_and_unresolved_statuses(self):
        session = MagicMock()
        result = MagicMock()
        result.rowcount = 1
        session.execute.return_value = result

        db = _db_with_session(session)
        count = await db.resolve_dead_letter_jobs_for_document("doc-249")

        assert count == 1
        assert session.execute.call_count == 1
        # Inspect the compiled UPDATE statement to confirm it is scoped to
        # this document_id AND the two UNRESOLVED statuses -- NOT a blanket
        # update of every row for the document (which would wrongly flip
        # 'abandoned' rows to 'resolved' too).
        #
        # Asserting only that the substrings "'retrying'" and "'resolved'"
        # both appear *somewhere* in the compiled statement is satisfied
        # just as well by a SET/WHERE swap (SET status='retrying' ... WHERE
        # status='resolved') as by the correct statement -- it does not pin
        # which clause each value lives in. Assert on the statement's own
        # SET values and WHERE clause instead, which a swap cannot satisfy.
        stmt = session.execute.call_args[0][0]

        # SET: status must be set to 'resolved' (not 'retrying'/'pending').
        assert stmt._values["status"].value == "resolved"

        # WHERE: must match rows currently 'pending' OR 'retrying' for this
        # document_id -- compiling the where-clause in isolation (rather
        # than the whole statement) means the string cannot accidentally
        # contain 'resolved' by leaking in from the SET clause.
        where_compiled = str(stmt.whereclause.compile(compile_kwargs={"literal_binds": True}))
        assert "dead_letter_jobs.document_id = 'doc-249'" in where_compiled
        assert "'retrying'" in where_compiled
        # #287: a 'pending' row for a document that has since ingested
        # successfully is factually resolved. Before this fix the default
        # `GET /dead-letter` listing (status='pending') kept showing the
        # document as broken forever, and Retry on that row would re-publish
        # the stale payload over the now-healthy document.
        assert "'pending'" in where_compiled

    @pytest.mark.asyncio
    async def test_does_not_touch_abandoned_or_already_resolved_rows(self):
        """The widening in #287 stops at 'pending' -- it must not become a
        blanket "resolve everything for this document_id".

        'abandoned' encodes an explicit operator decision to stop working a
        failure; flipping it to 'resolved' would erase that decision and
        silently re-admit the row to the retry route's 409 guard
        (`app.py`, which permits a retry only for 'pending'/'retrying').
        """
        session = MagicMock()
        result = MagicMock()
        result.rowcount = 0
        session.execute.return_value = result

        db = _db_with_session(session)
        await db.resolve_dead_letter_jobs_for_document("doc-287")

        stmt = session.execute.call_args[0][0]
        where_compiled = str(stmt.whereclause.compile(compile_kwargs={"literal_binds": True}))
        assert "abandoned" not in where_compiled
        # Also guards the SET/WHERE swap: 'resolved' is the value being
        # written, so it must never appear in the predicate.
        assert "resolved" not in where_compiled

    @pytest.mark.asyncio
    async def test_returns_zero_when_nothing_was_unresolved(self):
        session = MagicMock()
        result = MagicMock()
        result.rowcount = 0
        session.execute.return_value = result

        db = _db_with_session(session)
        count = await db.resolve_dead_letter_jobs_for_document("doc-no-retry")

        assert count == 0

    @pytest.mark.asyncio
    async def test_raises_when_not_connected(self):
        db = DatabaseService.__new__(DatabaseService)
        DatabaseService.__init__(db, Settings.model_construct())
        db.engine = None

        with pytest.raises(RuntimeError, match="not connected"):
            await db.resolve_dead_letter_jobs_for_document("doc-1")


class TestRetryGuardMatchesResolveScope:
    """The retry route's 409 guard and the resolve scope must stay one set (#287).

    This is the invariant that actually closes #287's stale-replay hole. The
    workflow resolves a document's outstanding rows on success *so that* the
    retry route then refuses to replay them: a resolved row is no longer in
    the retriable set, so `POST /dead-letter/{id}/retry` 409s instead of
    re-publishing the superseded payload over a healthy document.

    If the two lists were maintained independently, widening one without the
    other would silently reopen the hole -- with every unit test still
    green, because each side is individually self-consistent. Reading both
    from `DEAD_LETTER_UNRESOLVED_STATUSES` is what makes that drift
    impossible; this test pins that they are in fact the same source.
    """

    def test_unresolved_statuses_constant_is_exactly_pending_and_retrying(self):
        assert set(DatabaseService.DEAD_LETTER_UNRESOLVED_STATUSES) == {"pending", "retrying"}

    @pytest.mark.parametrize(
        "status,expected",
        [
            ("pending", 200),
            ("retrying", 200),
            # The #287 hazard, stated as behaviour: once the row is resolved
            # (because the document was repaired another way), replaying it
            # would re-ingest the stale payload over the healthy document.
            ("resolved", 409),
            ("abandoned", 409),
        ],
    )
    def test_retry_route_admits_exactly_the_unresolved_statuses(self, status, expected):
        """Drive the real route rather than inspecting `app.py`'s source.

        A source-text assertion would go quietly green the moment anyone
        reformatted the guard; this fails if the ACTUAL admitted set ever
        stops matching `DEAD_LETTER_UNRESOLVED_STATUSES`.
        """
        mock_settings = MagicMock()
        mock_settings.ingestion_api_key = "secret"
        mock_settings.api_host = "127.0.0.1"
        mock_settings.api_port = 8000
        mock_settings.temporal_host = "localhost:7233"
        mock_settings.temporal_namespace = "default"
        mock_settings.temporal_task_queue = "document-ingestion"
        mock_settings.log_level = "INFO"

        job = {"id": 7, "document_id": "doc-287", "workspace_id": "ws-1", "status": status}

        mock_db = MagicMock()
        mock_db.increment_dead_letter_retry = AsyncMock(return_value=True)
        mock_db.update_dead_letter_status = AsyncMock(return_value=True)

        with (
            patch("src.api.app.TemporalWorkerManager") as mock_mgr,
            patch("src.api.auth.get_settings", return_value=mock_settings),
            patch("src.temporal.shared_services.get_db_service", return_value=mock_db),
            patch("src.api.app.resolve_owned_dead_letter_job", AsyncMock(return_value=job)),
        ):
            instance = mock_mgr.return_value
            instance.start = AsyncMock()
            instance.stop = AsyncMock()
            instance.get_client = AsyncMock()
            instance.is_running = True

            from fastapi.testclient import TestClient

            from src.api.app import create_app

            app = create_app(mock_settings)
            with TestClient(app) as client:
                # Stubbed INSIDE the context manager: the lifespan runs on
                # __enter__ and installs its own real trigger, which would
                # overwrite anything set beforehand. The retry route
                # re-publishes through app.state.trigger, so an admitted
                # retry needs this to reach 200 rather than a transport error.
                app.state.trigger = MagicMock()
                app.state.trigger.trigger_workflow_async = AsyncMock(return_value="wf-287")
                resp = client.post(
                    "/dead-letter/7/retry",
                    params={"workspace_id": "ws-1"},
                    headers={"X-API-Key": "secret"},
                )

        assert resp.status_code == expected, (
            f"retry of a {status!r} dead-letter job returned {resp.status_code}, "
            f"expected {expected} -- the admitted set must stay exactly "
            f"DEAD_LETTER_UNRESOLVED_STATUSES (#287); response: {resp.text}"
        )


class TestDeadLetterAPIRoutes:
    """Test dead-letter API endpoint existence."""

    def test_list_requires_auth(self):
        mock_settings = MagicMock()
        mock_settings.ingestion_api_key = "secret"
        mock_settings.api_host = "127.0.0.1"
        mock_settings.api_port = 8000
        mock_settings.temporal_host = "localhost:7233"
        mock_settings.temporal_namespace = "default"
        mock_settings.temporal_task_queue = "document-ingestion"
        mock_settings.log_level = "INFO"

        with (
            patch("src.api.app.TemporalWorkerManager") as mock_mgr,
            patch("src.api.auth.get_settings", return_value=mock_settings),
        ):
            instance = mock_mgr.return_value
            instance.start = AsyncMock()
            instance.stop = AsyncMock()
            instance.get_client = AsyncMock()
            instance.is_running = True

            from fastapi.testclient import TestClient

            from src.api.app import create_app

            app = create_app(mock_settings)
            with TestClient(app) as client:
                # No auth header
                resp = client.get("/dead-letter")
                assert resp.status_code == 401

    @patch("src.temporal.shared_services.get_db_service")
    def test_list_returns_jobs(self, mock_get_db):
        mock_db = MagicMock()
        mock_db.get_dead_letter_jobs = AsyncMock(return_value=[])
        mock_get_db.return_value = mock_db

        mock_settings = MagicMock()
        mock_settings.ingestion_api_key = "secret"
        mock_settings.api_host = "127.0.0.1"
        mock_settings.api_port = 8000
        mock_settings.temporal_host = "localhost:7233"
        mock_settings.temporal_namespace = "default"
        mock_settings.temporal_task_queue = "document-ingestion"
        mock_settings.log_level = "INFO"

        with (
            patch("src.api.app.TemporalWorkerManager") as mock_mgr,
            patch("src.api.auth.get_settings", return_value=mock_settings),
        ):
            instance = mock_mgr.return_value
            instance.start = AsyncMock()
            instance.stop = AsyncMock()
            instance.get_client = AsyncMock()
            instance.is_running = True

            from fastapi.testclient import TestClient

            from src.api.app import create_app

            app = create_app(mock_settings)
            with TestClient(app) as client:
                # workspace_id is REQUIRED (#177) -- omitting it used to
                # return dead-letter rows across every workspace.
                resp = client.get(
                    "/dead-letter?workspace_id=ws_001", headers={"X-API-Key": "secret"}
                )
                assert resp.status_code == 200
                assert resp.json()["jobs"] == []
                mock_db.get_dead_letter_jobs.assert_awaited_once_with(
                    workspace_id="ws_001", status="pending", limit=50
                )

    @patch("src.temporal.shared_services.get_db_service")
    def test_list_requires_workspace_id(self, mock_get_db):
        """#177: workspace_id is no longer an optional filter -- omitting it
        used to leak dead-letter rows (document_id/workspace_id/user_id
        triples) across every tenant. Must now 422, not 200."""
        mock_settings = MagicMock()
        mock_settings.ingestion_api_key = "secret"
        mock_settings.api_host = "127.0.0.1"
        mock_settings.api_port = 8000
        mock_settings.temporal_host = "localhost:7233"
        mock_settings.temporal_namespace = "default"
        mock_settings.temporal_task_queue = "document-ingestion"
        mock_settings.log_level = "INFO"

        with (
            patch("src.api.app.TemporalWorkerManager") as mock_mgr,
            patch("src.api.auth.get_settings", return_value=mock_settings),
        ):
            instance = mock_mgr.return_value
            instance.start = AsyncMock()
            instance.stop = AsyncMock()
            instance.get_client = AsyncMock()
            instance.is_running = True

            from fastapi.testclient import TestClient

            from src.api.app import create_app

            app = create_app(mock_settings)
            with TestClient(app) as client:
                resp = client.get("/dead-letter", headers={"X-API-Key": "secret"})
                assert resp.status_code == 422
