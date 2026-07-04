-- Migration 011: Idempotency ledger for workspace stat increments (#7)
-- update_workspace_stats was a blind additive increment, so a Temporal retry
-- or a dead-letter reprocess of the same document double-counted the workspace
-- document/chunk/size totals (quota/billing drift that only ever grows).
--
-- This ledger records which workflow runs have already applied their deltas.
-- The stats activity inserts (workflow_run_id) with ON CONFLICT DO NOTHING and
-- only applies the increment when the insert wins the race, so each run counts
-- at most once. Additive + idempotent (safe to re-run).
CREATE TABLE IF NOT EXISTS workspace_stats_ledger (
    workflow_run_id TEXT PRIMARY KEY,
    workspace_id    TEXT NOT NULL,
    applied_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
