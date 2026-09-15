-- Migration 021: dead_letter_jobs.dedup_key -- per-batch dedup for workflows
-- that can dead-letter MORE THAN ONCE per run (#363).
--
-- Migration 013's `ux_dead_letter_jobs_document_run` unique index on
-- (document_id, workflow_run_id) assumes at most one dead-letter row per
-- run, true for DocumentIngestionWorkflow (one terminal failure ends the
-- run). ConversationMemoryWorkflow (#306) is long-lived -- ONE run can flush
-- (and potentially dead-letter, #363) many times -- so document_id and
-- workflow_run_id are BOTH constant across every flush of that run, and the
-- old index would make the second failed flush's `add_dead_letter_job` call
-- hit ON CONFLICT DO NOTHING and silently return the FIRST batch's row
-- (see conversation_memory.py's `_dead_letter_flush`), which is exactly the
-- "loses turns silently" failure #363 exists to close.
--
-- `dedup_key` distinguishes multiple dead-letter rows within the same
-- (document_id, workflow_run_id): callers with at most one dead-letter per
-- run (DocumentIngestionWorkflow) leave it at the column default '' and get
-- byte-identical dedup behavior to before this migration; conversation
-- flushes pass a value stable across RETRIES of the SAME failed batch (so
-- record_dead_letter's own activity retries stay idempotent) but distinct
-- across DIFFERENT batches (so two independently-failed flushes both get
-- recorded). All statements are idempotent (IF NOT EXISTS).

ALTER TABLE dead_letter_jobs
    ADD COLUMN IF NOT EXISTS dedup_key VARCHAR(255) NOT NULL DEFAULT '';

DROP INDEX IF EXISTS ux_dead_letter_jobs_document_run;

CREATE UNIQUE INDEX IF NOT EXISTS ux_dead_letter_jobs_document_run_dedup
    ON dead_letter_jobs (document_id, workflow_run_id, dedup_key);
