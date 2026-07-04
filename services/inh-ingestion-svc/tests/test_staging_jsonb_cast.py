"""Staging chunk writes must cast to jsonb explicitly (#41).

write_chunks bound a json.dumps() string into the JSONB json_data column via a
plain text parameter, relying on an implicit text->jsonb cast that isn't
guaranteed across driver/PG combinations. Use an explicit CAST.
"""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock

from src.services.staging import StagingService


def _staging(session) -> StagingService:
    svc = StagingService.__new__(StagingService)
    db = MagicMock()

    @contextmanager
    def _gs():
        yield session

    db.get_session = _gs
    svc._db = db
    return svc


def test_write_chunks_uses_explicit_jsonb_cast():
    session = MagicMock()
    _staging(session).write_chunks("wf-1", [{"a": 1}])
    sql = str(session.execute.call_args.args[0])
    assert "CAST(:data AS jsonb)" in sql
