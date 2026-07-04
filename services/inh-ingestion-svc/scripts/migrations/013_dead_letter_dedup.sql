-- Migration 013: Deduplicate dead-letter jobs per (document_id, workflow_run_id) (#24)
--
-- dead_letter_jobs had no unique constraint, so a record-retry (the insert
-- commits but then loses its ack and retries) created a second row for the same
-- run. The dead-letter retry API could then re-publish / re-ingest the same
-- document twice.
--
-- A plain unique index dedups non-null run ids while leaving NULL run ids
-- distinct (Postgres treats NULLs as distinct), which is what we want:
-- pre-workflow failures have no run to key on. Idempotent (IF NOT EXISTS).
CREATE UNIQUE INDEX IF NOT EXISTS ux_dead_letter_jobs_document_run
    ON dead_letter_jobs (document_id, workflow_run_id);
