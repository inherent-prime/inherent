"""The fk_workspace_tenant constraint must be created by a migration (#26).

000_initial_schema pre-creates workspace_metadata without the FK, so 002's
CREATE TABLE IF NOT EXISTS no-ops and the constraint is absent on fresh DBs.
Migration 012 adds it idempotently.
"""

from __future__ import annotations

from pathlib import Path

_MIGRATIONS = Path(__file__).resolve().parents[1] / "scripts" / "migrations"


def test_migration_012_adds_fk_idempotently():
    sql = (_MIGRATIONS / "012_workspace_tenant_fk.sql").read_text()
    assert "ADD CONSTRAINT fk_workspace_tenant" in sql
    assert "REFERENCES tenants(user_id)" in sql
    assert "ON DELETE CASCADE" in sql
    # Guarded so re-running is a no-op.
    assert "pg_constraint" in sql and "IF NOT EXISTS" in sql
