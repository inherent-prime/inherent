-- Migration 014: Ingestion events table for data lineage (#89)
--
-- src/temporal/lineage.py records every pipeline step (tenant_ready,
-- document_fetched, text_extracted, ...) into ingestion_events, and the
-- public API's lineage endpoint reads it back — but no migration ever
-- created the table, so every write failed with UndefinedTable and lineage
-- shipped broken by default.
--
-- Shape mirrors the SQLAlchemy definition in DatabaseService (database.py),
-- including the index names, so create_all-provisioned test databases and
-- migration-provisioned databases stay identical. Idempotent (IF NOT EXISTS).

CREATE TABLE IF NOT EXISTS ingestion_events (
    id BIGSERIAL PRIMARY KEY,
    workflow_run_id VARCHAR(255) NOT NULL,   -- Temporal workflow run ID
    document_id VARCHAR(255) NOT NULL,
    workspace_id VARCHAR(255),               -- NULL for steps with no workspace context
    event_type VARCHAR(50) NOT NULL,         -- pipeline step name (e.g. 'text_chunked')
    status VARCHAR(20) NOT NULL,             -- 'started' | 'succeeded' | 'failed'
    duration_ms INTEGER,
    metadata JSONB,                          -- extra context (error messages, counts)
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Lineage API reads by document; step lookups and cleanup go by workflow run.
CREATE INDEX IF NOT EXISTS idx_ingestion_events_workflow_run_id ON ingestion_events(workflow_run_id);
CREATE INDEX IF NOT EXISTS idx_ingestion_events_document_id ON ingestion_events(document_id);
CREATE INDEX IF NOT EXISTS idx_ingestion_events_event_type ON ingestion_events(event_type);
