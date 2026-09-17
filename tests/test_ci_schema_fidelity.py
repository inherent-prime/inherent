"""Repo-level guard: CI provisions inh-ingestion-svc's Postgres schema via the
real migrations, not just `DatabaseService.ensure_schema()`.

Background (see `.superpowers/sdd/ingestion-db-test-failures-2026-08-13.md`
and `.superpowers/sdd/ci-schema-fidelity-report.md`): `ensure_schema()`
(`services/inh-ingestion-svc/src/services/database.py`) builds the schema
from SQLAlchemy `Table` metadata via `metadata.create_all()`. That metadata
does not (or previously did not) declare every constraint the raw SQL
migrations in `scripts/migrations/*.sql` add -- e.g. migration 012 adds
`fk_workspace_tenant`, a foreign key the SQLAlchemy model omitted. Before
this guard, `ci.yml`'s `inh-ingestion-svc` job relied solely on
`ensure_schema()` for its Postgres service container, so CI's schema
structurally lacked constraints that dev/staging/prod all have (both are
provisioned via `docker-compose.yml`'s `postgres-init` service or the
in-image `SERVICE_MODE=migrate` runner, which apply
`scripts/migrations/*.sql` in filename order). Any test relying on a
migration-only constraint would pass in CI regardless of whether the
constraint actually held -- a whole class of bug CI could never catch.

These tests pin that `ci.yml` applies the migrations, in order, before the
ingestion service's tests run, and fails loudly if a migration errors -- so
this gap cannot silently regress.
"""

from __future__ import annotations

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]

CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

MIGRATIONS_DIR = REPO_ROOT / "services" / "inh-ingestion-svc" / "scripts" / "migrations"


def _ci_text() -> str:
    return CI_WORKFLOW.read_text()


def _migration_step_block() -> str:
    """Return the `Apply DB migrations` step's YAML block.

    Reads the raw YAML rather than parsing it, matching the house style in
    `test_local_postgres_init.py` / `test_integration_workflow_guards.py`:
    the root suite has no project of its own to declare a YAML dependency
    for one lookup.
    """
    text = _ci_text()
    start = text.index("- name: Apply DB migrations")
    # The next step starts at the same indentation ("      - name:"); the
    # block runs from this step's header to the next one (or EOF).
    nxt = re.search(r"\n      - name:", text[start + 1 :])
    return text[start : start + 1 + nxt.start()] if nxt else text[start:]


def test_ci_ingestion_job_applies_migrations() -> None:
    """`ci.yml` must run a step that applies `scripts/migrations/*.sql`.

    Without this, CI's Postgres service container is provisioned solely by
    `ensure_schema()`'s SQLAlchemy metadata -- which has already silently
    diverged from the raw SQL migrations once (fk_workspace_tenant) and can
    diverge again for any future migration-only constraint.
    """
    text = _ci_text()

    assert "Apply DB migrations" in text, (
        "ci.yml must have a step that applies services/inh-ingestion-svc/"
        "scripts/migrations/*.sql against the Postgres service container -- "
        "ensure_schema() alone does not create every constraint the real "
        "migrations do (see fk_workspace_tenant, migration 012)."
    )

    block = _migration_step_block()
    assert "scripts/migrations/*.sql" in block, (
        f"migration step does not reference scripts/migrations/*.sql: {block}"
    )


def test_ci_migration_step_is_scoped_to_ingestion_service() -> None:
    """The migration step must only run for the `inh-ingestion-svc` matrix
    entry -- `inh-public-api-svc` and `inh-contracts` don't ship this
    migrations directory relative to their own `matrix.path`.
    """
    block = _migration_step_block()

    condition = re.search(r"if: (.+)", block)
    assert condition is not None, f"migration step has no `if:` guard: {block}"
    assert "matrix.service == 'inh-ingestion-svc'" in condition.group(1), (
        f"migration step must be scoped to matrix.service == 'inh-ingestion-svc', "
        f"found: {condition.group(1)}"
    )


def test_ci_migration_step_fails_loudly_on_error() -> None:
    """A migration error must fail the CI step, not be silently swallowed.

    `psql` only surfaces a non-zero exit for a failed statement when told to
    (`ON_ERROR_STOP=1`); the shell loop around it must also stop at the
    first failing migration (`set -e`).
    """
    block = _migration_step_block()

    assert "set -e" in block, f"migration step must `set -e`: {block}"
    assert "ON_ERROR_STOP=1" in block, f"migration step must set psql's ON_ERROR_STOP=1: {block}"


def test_ci_migration_step_runs_before_tests() -> None:
    """The schema must be migrated before pytest runs, for every matrix
    entry that has the step -- otherwise the constraints it adds don't exist
    yet when the Test step's `db_service` fixture connects.
    """
    text = _ci_text()

    migrations_pos = text.index("- name: Apply DB migrations")
    test_pos = text.index("- name: Test\n")

    assert migrations_pos < test_pos, (
        "the 'Apply DB migrations' step must appear before the 'Test' step "
        "in ci.yml's service-checks job"
    )


def test_ci_migration_glob_order_matches_migrations_directory_convention() -> None:
    """`scripts/migrations/*.sql` file names are zero-padded (000, 001, ...)
    so a plain shell glob sorts them in the same order
    `docker-compose.yml`'s `postgres-init` service and the in-image
    `SERVICE_MODE=migrate` runner (`src/services/migrations.py`, which
    explicitly sorts the glob) both apply them in. This pins that
    assumption: if a migration file is ever added without a zero-padded,
    lexicographically-ordered prefix, alphabetical (CI's implicit glob
    order) and numeric order would diverge.
    """
    filenames = sorted(p.name for p in MIGRATIONS_DIR.glob("*.sql"))
    numeric_order = sorted(filenames, key=lambda name: int(name.split("_", 1)[0]))

    assert filenames == numeric_order, (
        "migration filenames must sort identically by plain alphabetical "
        "order and by their leading numeric prefix, or CI's shell glob "
        f"would apply them out of order. Got: {filenames}"
    )
