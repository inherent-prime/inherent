-- Migration 013: Create dead_letter_jobs table + dedup unique index (#24)
--
-- The dead_letter_jobs table was previously defined only in SQLAlchemy
-- metadata (database.py) and was created by create_all() at runtime.  Since
-- the project now manages all schema through migrations (the init script no
-- longer calls create_all -- see database.py), we must create it here.
--
-- The unique index prevents record-retry duplicates: dead_letter_jobs had no
-- unique constraint, so a record-retry (insert commits but loses its ack)
-- created a second row for the same run. The dead-letter retry API could then
-- re-publish / re-ingest the same document twice.
--
-- A plain unique index dedups non-null run ids while leaving NULL run ids
-- distinct (Postgres treats NULLs as distinct), which is what we want:
-- pre-workflow failures have no run to key on. All statements are idempotent
-- (IF NOT EXISTS).

CREATE TABLE IF NOT EXISTS dead_letter_jobs (
    id               BIGSERIAL    PRIMARY KEY,
    document_id      VARCHAR(255) NOT NULL,
    workspace_id     VARCHAR(255) NOT NULL,
    user_id          VARCHAR(255) NOT NULL,
    workflow_run_id  VARCHAR(255),
    original_message JSONB        NOT NULL,
    error_message    TEXT         NOT NULL,
    error_type       VARCHAR(100) NOT NULL,
    retry_count      INTEGER      NOT NULL DEFAULT 0,
    status           VARCHAR(20)  NOT NULL DEFAULT 'pending',
    created_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    resolved_at      TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_dead_letter_jobs_document_id
    ON dead_letter_jobs (document_id);
CREATE INDEX IF NOT EXISTS idx_dead_letter_jobs_workspace_id
    ON dead_letter_jobs (workspace_id);
CREATE INDEX IF NOT EXISTS idx_dead_letter_jobs_status
    ON dead_letter_jobs (status);

CREATE UNIQUE INDEX IF NOT EXISTS ux_dead_letter_jobs_document_run
    ON dead_letter_jobs (document_id, workflow_run_id);
