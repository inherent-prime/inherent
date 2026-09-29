-- Migration 025: Add uploaded_by to processed_documents (workspace members, prime#331)
--
-- processed_documents.user_id is the DATA-PLANE identity: the Weaviate tenant
-- the document's vectors live in. Since workspace members landed it is always
-- the workspace OWNER's id, so every member of a workspace sees the whole
-- workspace. uploaded_by records who actually performed the upload (the
-- caller: an owner, or a member), so member uploads stay attributable.
--
-- Idempotent & additive (follows the migration-safety conventions in README.md):
--   - IF NOT EXISTS guard makes re-runs a no-op.
--   - The column is NULLABLE, so existing rows are unaffected; documents
--     uploaded before this migration simply have no uploaded_by (their
--     uploader is still their user_id).
--   - No data is dropped or rewritten.

ALTER TABLE processed_documents
    ADD COLUMN IF NOT EXISTS uploaded_by VARCHAR(100);
