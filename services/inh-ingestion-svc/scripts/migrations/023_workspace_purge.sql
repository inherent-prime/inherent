-- Migration 023: workspace purge state + receipts (inherent#395)
--
-- Two new, additive tables backing the operator-triggered PurgeWorkspaceWorkflow:
--
-- workspace_purge_state -- one row per workspace CURRENTLY being (or having
--   been) purged. Its `status` ('purging' | 'purged') is the gate
--   `ensure_workspace_ready` checks before letting a new document into a
--   workspace, and the marker the purge workflow's cancel step uses to know
--   which workspace is off-limits for new ingest while it walks each store.
--   Deliberately NOT a column on `workspace_metadata`: that table's row is
--   deleted outright by the existing `delete_workspace_data` (see
--   database.py) as part of the purge, so purge state has to live somewhere
--   that survives the workspace row itself being gone.
--
-- workspace_purge_receipts -- durable, queryable proof that a purge ran and
--   what it found. Holds per-store row COUNTS only (before/after), never
--   document content, so it can be shown to a customer or auditor as
--   evidence a data-deletion commitment (pilot end, deletion request) was
--   honored. `verified` is true only when every "after" count is zero.
--
-- Idempotent (IF NOT EXISTS everywhere); safe to re-run.

CREATE TABLE IF NOT EXISTS workspace_purge_state (
    workspace_id VARCHAR(100) PRIMARY KEY,
    status       VARCHAR(20) NOT NULL DEFAULT 'purging', -- purging | purged
    requested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_workspace_purge_state_status CHECK (status IN ('purging', 'purged'))
);

CREATE TABLE IF NOT EXISTS workspace_purge_receipts (
    -- Keyed by the Temporal workflow id (deterministic, one purge run per
    -- id) so re-running the verification/receipt activity for the SAME
    -- workflow is an idempotent upsert, never a duplicate row.
    purge_workflow_id VARCHAR(255) PRIMARY KEY,
    workspace_id      VARCHAR(100) NOT NULL,
    operator          VARCHAR(255) NOT NULL,
    retain_audit_logs BOOLEAN NOT NULL DEFAULT FALSE,
    counts_before     JSONB NOT NULL DEFAULT '{}',
    counts_after      JSONB NOT NULL DEFAULT '{}',
    verified          BOOLEAN NOT NULL DEFAULT FALSE,
    requested_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at      TIMESTAMPTZ,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_workspace_purge_receipts_workspace_id
    ON workspace_purge_receipts (workspace_id, created_at DESC);
