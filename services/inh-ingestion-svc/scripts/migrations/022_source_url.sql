-- Migration 022: Add source_url to processed_documents and document_chunks (inherent#391)
-- Adds one nullable, additive column to each table: the connector's link back
-- to the ORIGINAL file in its source system (e.g. a Drive webViewLink).
-- Distinct from storage_url/source_uri (already present), which point at
-- THIS engine's own stored copy, not the source system.
--
-- Idempotent & additive (follows the migration-safety conventions in README.md):
--   - IF NOT EXISTS guards make re-runs a no-op.
--   - Both columns are NULLABLE, so existing rows are unaffected and the change
--     is backward-compatible. Pre-existing documents/chunks simply have a NULL
--     source_url until re-ingested.
--   - No data is dropped or rewritten.

ALTER TABLE processed_documents
    ADD COLUMN IF NOT EXISTS source_url VARCHAR(2000);

ALTER TABLE document_chunks
    ADD COLUMN IF NOT EXISTS source_url VARCHAR(2000);
