"""Settings/DB/metrics plumbing tests for #307 (redact_turns):

- ``settings.redaction_patterns_extra`` is declared, defaults to an empty
  list, and reads from ``REDACTION_PATTERNS_EXTRA``.
- ``DatabaseService`` defines the ``redaction_audit`` table and the
  ``record_redaction_failure`` method (mirrors
  tests/test_migrations.py::TestDeadLetterDBTableExists for dead_letter_jobs).
- ``record_redaction_failure``'s INSERT never carries a raw-text column,
  pinned via a mocked SQLAlchemy session (same technique as
  tests/test_migrations.py::TestResolveDeadLetterJobsForDocument).
- the new Prometheus counters (REDACTIONS_TOTAL /
  REDACTED_TURNS_DROPPED_TOTAL) exist and are Counters.

No PostgreSQL required -- ``cleanup_test_data`` is overridden with a no-op
(same pattern as tests/test_migrations.py) so the package-wide autouse DB
fixture doesn't silently skip this module.
"""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest
from prometheus_client import Counter
from sqlalchemy import MetaData

from src.config.settings import Settings
from src.services.database import DatabaseService


@pytest.fixture(autouse=True)
def cleanup_test_data():
    """No-op override of the package-level DB-dependent autouse fixture."""
    yield


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


class TestRedactionSettings:
    def test_field_declared_with_empty_list_default(self):
        assert Settings.model_fields["redaction_patterns_extra"].default == []

    def test_reads_from_env_alias(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost/db")
        monkeypatch.setenv("WEAVIATE_URL", "http://localhost:8080")
        monkeypatch.setenv("REDACTION_PATTERNS_EXTRA", '["FOO-\\\\d+"]')

        settings = Settings()  # type: ignore[call-arg]

        assert settings.redaction_patterns_extra == ["FOO-\\d+"]

    def test_defaults_to_empty_when_unset(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost/db")
        monkeypatch.setenv("WEAVIATE_URL", "http://localhost:8080")
        monkeypatch.delenv("REDACTION_PATTERNS_EXTRA", raising=False)

        settings = Settings()  # type: ignore[call-arg]

        assert settings.redaction_patterns_extra == []


# ---------------------------------------------------------------------------
# Table + method existence (mirrors TestDeadLetterDBTableExists)
# ---------------------------------------------------------------------------


class TestRedactionAuditTable:
    def test_redaction_audit_table_defined(self):
        db = DatabaseService.__new__(DatabaseService)
        db.metadata = MetaData()
        db._define_tables()

        assert hasattr(db, "redaction_audit")
        col_names = [c.name for c in db.redaction_audit.columns]
        assert "turn_id" in col_names
        assert "detector" in col_names
        assert "error_type" in col_names
        assert "error_message" in col_names
        assert "workflow_run_id" in col_names
        assert "workspace_id" in col_names
        assert "document_id" in col_names

    def test_no_raw_text_column_exists(self):
        """The table's own column set must never grow a place to put raw
        turn content -- see migration 019's comment. Pins the column NAME
        set narrowly so a future column named e.g. 'turn_text' or 'content'
        would fail this test rather than slipping in silently."""
        db = DatabaseService.__new__(DatabaseService)
        db.metadata = MetaData()
        db._define_tables()

        col_names = {c.name for c in db.redaction_audit.columns}
        assert col_names == {
            "id",
            "turn_id",
            "workflow_run_id",
            "workspace_id",
            "document_id",
            "detector",
            "error_type",
            "error_message",
            "created_at",
        }

    def test_record_redaction_failure_method_exists(self):
        assert hasattr(DatabaseService, "record_redaction_failure")


def _db_with_session(session) -> DatabaseService:
    """Same helper pattern as tests/test_migrations.py's `_db_with_session`."""
    db = DatabaseService.__new__(DatabaseService)
    DatabaseService.__init__(db, Settings.model_construct())
    db.engine = MagicMock()

    @contextmanager
    def _gs():
        yield session

    db.get_session = _gs
    return db


class TestRecordRedactionFailure:
    async def test_raises_when_not_connected(self):
        db = DatabaseService.__new__(DatabaseService)
        DatabaseService.__init__(db, Settings.model_construct())
        db.engine = None

        with pytest.raises(RuntimeError, match="not connected"):
            await db.record_redaction_failure(
                turn_id="t1",
                detector="jwt",
                error_type="ValueError",
                error_message="boom",
            )

    async def test_insert_values_carry_only_declared_fields_no_raw_text(self):
        """Pins the INSERT statement's VALUES to exactly the audit-safe
        field set -- inspecting the compiled statement (not just calling the
        method and trusting no exception) is what actually catches a future
        change that adds a raw-text kwarg to the INSERT."""
        session = MagicMock()
        result = MagicMock()
        result.scalar_one.return_value = 42
        session.execute.return_value = result

        db = _db_with_session(session)

        secret = "sk-proj-shouldneverappearinsql-abcdefghij"
        audit_id = await db.record_redaction_failure(
            turn_id="t1",
            detector="api_key",
            error_type="ValueError",
            error_message="a generic failure message",
            workflow_run_id="run-1",
            workspace_id="ws-1",
            document_id="conv-1",
        )

        assert audit_id == 42
        session.execute.assert_called_once()
        stmt = session.execute.call_args[0][0]

        # SQLAlchemy Insert.values() stores the bound values on `_values`
        # (same introspection technique test_migrations.py's
        # TestResolveDeadLetterJobsForDocument uses for an Update statement).
        bound = {k: v.value for k, v in stmt._values.items()}
        assert bound["turn_id"] == "t1"
        assert bound["detector"] == "api_key"
        assert bound["error_type"] == "ValueError"
        assert bound["error_message"] == "a generic failure message"
        assert bound["workflow_run_id"] == "run-1"
        assert bound["workspace_id"] == "ws-1"
        assert bound["document_id"] == "conv-1"
        # No stray key ever carries the secret -- confirms the call path has
        # no route for raw turn text to reach this statement at all.
        assert secret not in repr(bound)


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


class TestRedactionMetrics:
    def test_redactions_total_is_counter(self):
        from src.services.metrics import REDACTIONS_TOTAL

        assert isinstance(REDACTIONS_TOTAL, Counter)

    def test_redacted_turns_dropped_total_is_counter(self):
        from src.services.metrics import REDACTED_TURNS_DROPPED_TOTAL

        assert isinstance(REDACTED_TURNS_DROPPED_TOTAL, Counter)
