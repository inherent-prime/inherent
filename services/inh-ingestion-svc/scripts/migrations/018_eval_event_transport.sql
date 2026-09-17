-- Migration 018: Add transport column to eval_query_events (#241)
--
-- Evals v1 captured every search into eval_query_events, but only the REST
-- handler (POST /v1/search) ever called record_query_event -- the MCP search
-- path (search_documents / search_memory) minted no event at all, so
-- analytics had no way to tell an MCP-originated event apart from a REST one
-- once MCP started capturing too (see #241: the MCP fix routes capture
-- through the same shared helper REST already used, so both transports now
-- write here).
--
-- transport records which surface produced the row: 'rest' | 'mcp'. NOT
-- NULL with a default so existing rows (all captured before MCP could ever
-- capture anything) backfill as 'rest' -- the only transport that could have
-- written them -- without a data migration, and so every future INSERT must
-- state its transport explicitly rather than silently defaulting.
ALTER TABLE eval_query_events
    ADD COLUMN IF NOT EXISTS transport VARCHAR(20) NOT NULL DEFAULT 'rest';

-- Lets "how many events came from MCP vs REST this week" be answered without
-- a full table scan, mirroring ix_eval_events_ws_created's shape.
CREATE INDEX IF NOT EXISTS ix_eval_events_ws_transport
    ON eval_query_events (workspace_id, transport);
