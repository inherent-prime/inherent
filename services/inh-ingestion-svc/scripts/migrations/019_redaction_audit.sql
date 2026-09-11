-- Migration 019: redaction_audit table for the redact_turns activity (#307)
--
-- redact_turns (src/temporal/activities/redact.py) runs per-turn credential
-- redaction before conversation chunking. A turn whose redaction pass
-- raises is DROPPED rather than stored unredacted, and the drop is recorded
-- here for observability -- "a credential-shaped turn silently vanished"
-- must be debuggable without ever having to look at (or store) the raw text
-- that caused it.
--
-- Deliberately narrow schema: turn_id, which detector fired, and an error
-- class/message. NO raw turn text, NO document content -- this table exists
-- specifically so it CANNOT become a second place a leaked credential ends
-- up (see redact.py's module docstring, and src/services/database.py's
-- record_redaction_failure, which is the only writer and only accepts these
-- fields).
--
-- Shape mirrors the SQLAlchemy definition in DatabaseService (database.py),
-- including index names, the same convention as migration 014's
-- ingestion_events table. Idempotent (IF NOT EXISTS).
--
-- #307 numbering note: 018 is taken on origin/feat/241-mcp-search-event-capture
-- (018_eval_event_transport.sql) but does not exist on origin/main, so 019 is
-- the first free number on both as of this migration's authoring.

CREATE TABLE IF NOT EXISTS redaction_audit (
    id BIGSERIAL PRIMARY KEY,
    turn_id VARCHAR(255) NOT NULL,
    workflow_run_id VARCHAR(255),            -- NULL if the caller had none yet
    workspace_id VARCHAR(255),
    document_id VARCHAR(255),
    detector VARCHAR(100) NOT NULL,          -- which detector raised (e.g. 'jwt', 'high_entropy_token', 'custom')
    error_type VARCHAR(255) NOT NULL,        -- exception class name (e.g. 'ValueError')
    error_message TEXT,                      -- str(exception) -- never raw turn text, see comment above
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Lookups by turn (debugging one dropped turn) and by workflow run (auditing
-- a whole conversation flush), same access pattern as ingestion_events.
CREATE INDEX IF NOT EXISTS idx_redaction_audit_turn_id ON redaction_audit(turn_id);
CREATE INDEX IF NOT EXISTS idx_redaction_audit_workflow_run_id ON redaction_audit(workflow_run_id);
CREATE INDEX IF NOT EXISTS idx_redaction_audit_document_id ON redaction_audit(document_id);
