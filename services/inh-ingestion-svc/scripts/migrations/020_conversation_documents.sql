-- Migration 020: conversation documents (#306)
--
-- `processed_documents` (migration 001) is file-shaped: `content_type`,
-- `storage_path` are NOT NULL and `size_bytes` is CHECKed > 0, and there is
-- no `document_type`/`external_id` pair for the issue's described
-- `(workspace_id, external_id)` upsert -- that lookup key simply does not
-- exist in this repo before this migration. This migration adds it, without
-- touching any existing NOT NULL/CHECK constraint: a conversation document
-- still supplies synthetic-but-real, non-null `content_type`/`storage_path`
-- and a `size_bytes` > 0 (the flushed batch's byte length), so it satisfies
-- the file-shaped constraints as-is rather than weakening them for every
-- other document (see conversation_memory.py / store.py for what those
-- synthetic values are).
--
-- `document_type` distinguishes a conversation's one `processed_documents`
-- row (`document_type='conversation'`, ONE row per conversation, grown via
-- the `append` extension to store_processed_document/store_chunks_with_tenant
-- rather than replaced on every flush) from an ordinary file
-- (`document_type='file'`, the default -- every existing row backfills to
-- this so DocumentIngestionWorkflow's behaviour is unchanged).
--
-- `external_id` is the caller-supplied conversation identifier from
-- `POST /v1/conversations/{external_id}/turns` -- NULL for ordinary file
-- documents (they have no such caller-chosen identity), unique per
-- workspace when present so GET/DELETE /v1/conversations/{external_id} can
-- resolve it to a `processed_documents` row the same way file documents are
-- resolved by `document_id`.
--
-- #306 numbering note: 018 is taken on this branch's base
-- (018_eval_event_transport.sql, from origin/feat/241-mcp-search-event-capture)
-- and 019 is taken on this branch's own base
-- (019_redaction_audit.sql, #337/#307) -- 020 is the first free number here.

ALTER TABLE processed_documents
    ADD COLUMN IF NOT EXISTS document_type VARCHAR(20) NOT NULL DEFAULT 'file';

ALTER TABLE processed_documents
    ADD COLUMN IF NOT EXISTS external_id VARCHAR(255);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'chk_document_type'
    ) THEN
        ALTER TABLE processed_documents
            ADD CONSTRAINT chk_document_type CHECK (document_type IN ('file', 'conversation'));
    END IF;
END $$;

-- One row per (workspace_id, external_id) -- the upsert key GET/DELETE
-- /v1/conversations/{external_id} resolve against. Partial (WHERE
-- external_id IS NOT NULL) so the near-totality of rows (ordinary file
-- documents, external_id always NULL) never participate in this uniqueness
-- check at all.
CREATE UNIQUE INDEX IF NOT EXISTS uq_processed_documents_workspace_external_id
    ON processed_documents(workspace_id, external_id)
    WHERE external_id IS NOT NULL;

-- Lookups by document_type alone (e.g. an ops query listing all
-- conversations in a workspace) reuse idx_processed_documents_workspace_id
-- plus this filter; a dedicated index keeps that filter cheap without
-- widening the existing workspace_id index.
CREATE INDEX IF NOT EXISTS idx_processed_documents_document_type
    ON processed_documents(document_type);
