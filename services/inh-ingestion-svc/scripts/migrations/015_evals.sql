-- Migration 015: Evals v1 — traffic-mined retrieval evals (design spec: evals-v1).
--
-- Five tenant-scoped tables:
--   eval_query_events — raw search capture. Short-lived: purged after the
--                       retention window (EVAL_RETENTION_DAYS, default 30) by
--                       the capture write-behind and DELETE /v1/evals/events.
--   eval_feedback     — agent verdicts keyed by event_id. Intentionally NO FK
--                       to eval_query_events: events are TTL-purged, feedback
--                       aggregates must survive the purge.
--   eval_cases        — labeled cases promoted from positive feedback. One row
--                       per (workspace, normalized query); persistent.
--   eval_runs         — mode-comparison run metadata + aggregate metrics.
--   eval_run_results  — per-case, per-mode metrics; dies with its run.
--
-- Tenancy: every table carries workspace_id and every query filters on it.
-- Workspace deletion must delete eval rows by workspace_id (app-level, like
-- the other per-workspace tables). Idempotent: safe to re-run.

CREATE TABLE IF NOT EXISTS eval_query_events (
    event_id         VARCHAR(100) PRIMARY KEY,
    workspace_id     VARCHAR(100) NOT NULL,
    user_id          VARCHAR(100),
    query_text       TEXT NOT NULL,
    search_mode      VARCHAR(20) NOT NULL,
    result_doc_ids   JSONB NOT NULL DEFAULT '[]',   -- ordered, best match first
    result_chunk_ids JSONB NOT NULL DEFAULT '[]',   -- ordered, best match first
    top_score        DOUBLE PRECISION,
    quality_verdict  VARCHAR(40),                   -- sufficient | insufficient_evidence | low_confidence
    latency_ms       DOUBLE PRECISION,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_eval_events_ws_created
    ON eval_query_events (workspace_id, created_at DESC);

CREATE TABLE IF NOT EXISTS eval_feedback (
    event_id         VARCHAR(100) PRIMARY KEY,      -- one verdict per event; upsert = last write wins
    workspace_id     VARCHAR(100) NOT NULL,
    verdict          VARCHAR(20) NOT NULL,          -- answered | partial | not_relevant
    useful_chunk_ids JSONB NOT NULL DEFAULT '[]',
    query_text       TEXT NOT NULL,                 -- denormalized so gap reports survive event purge
    note             TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_eval_feedback_ws_created
    ON eval_feedback (workspace_id, created_at DESC);

CREATE TABLE IF NOT EXISTS eval_cases (
    case_id          VARCHAR(100) PRIMARY KEY,
    workspace_id     VARCHAR(100) NOT NULL,
    query_text       TEXT NOT NULL,
    expected_doc_ids JSONB NOT NULL DEFAULT '[]',
    relevance_grade  INTEGER NOT NULL DEFAULT 1,    -- answered=2, partial=1
    active           BOOLEAN NOT NULL DEFAULT TRUE, -- soft delete / disable
    source_event_id  VARCHAR(100),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
-- One case per normalized query per workspace; re-feedback updates in place.
CREATE UNIQUE INDEX IF NOT EXISTS ux_eval_cases_ws_query
    ON eval_cases (workspace_id, md5(lower(query_text)));

CREATE TABLE IF NOT EXISTS eval_runs (
    run_id       VARCHAR(100) PRIMARY KEY,
    workspace_id VARCHAR(100) NOT NULL,
    status       VARCHAR(20) NOT NULL DEFAULT 'running',  -- running | completed | failed
    case_count   INTEGER NOT NULL DEFAULT 0,
    k            INTEGER NOT NULL DEFAULT 5,
    aggregates   JSONB NOT NULL DEFAULT '{}',  -- {mode: {recall_at_k, mrr, ndcg_at_k}}
    error        TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at  TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS ix_eval_runs_ws_created
    ON eval_runs (workspace_id, created_at DESC);

CREATE TABLE IF NOT EXISTS eval_run_results (
    id          BIGSERIAL PRIMARY KEY,
    run_id      VARCHAR(100) NOT NULL REFERENCES eval_runs(run_id) ON DELETE CASCADE,
    case_id     VARCHAR(100) NOT NULL,
    query_text  TEXT NOT NULL,
    mode        VARCHAR(20) NOT NULL,
    recall_at_k DOUBLE PRECISION NOT NULL,
    mrr         DOUBLE PRECISION NOT NULL,
    ndcg_at_k   DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_eval_run_results_run ON eval_run_results (run_id);
