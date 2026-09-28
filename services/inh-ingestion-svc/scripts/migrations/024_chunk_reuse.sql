-- Migration 024: usage-based ranking boost -- chunk reuse tracking (inherent#394)
--
-- Adds the storage a chunk needs to know "how many times has my content
-- reappeared in a NEWER document in this same workspace" (a near-duplicate
-- detected at ingest time, opt-in per workspace via WORKSPACE_REUSE_DETECTION
-- -- see inh-ingestion-svc's reuse_detection.py):
--
--   document_chunks.reuse_count    -- additive counter, defaults to 0 so
--                                      every existing chunk reads as "never
--                                      reused" until a later ingest detects
--                                      one. Mirrored onto the Weaviate object
--                                      as a numeric property (same pattern
--                                      as #390's `tags`) so the public API's
--                                      ranking boost can read it without a
--                                      DB join.
--   document_chunks.last_reused_at -- nullable; when reuse_count was last
--                                      bumped.
--
--   chunk_reuse_events             -- one row per (source_chunk, reusing
--                                      document, reusing document's CURRENT
--                                      content) triple. This is the
--                                      idempotency/dedupe key (#394's own
--                                      requirement): re-ingesting the SAME
--                                      unchanged document produces the same
--                                      per-chunk content_hash, so the
--                                      UNIQUE constraint below makes the
--                                      repeat insert a no-op (ON CONFLICT DO
--                                      NOTHING) instead of double-counting.
--                                      A genuinely edited chunk gets a new
--                                      content_hash and is free to be
--                                      re-evaluated as reuse of the same (or
--                                      a different) source chunk.
--
-- Idempotent & additive (follows the migration-safety conventions in
-- README.md): IF NOT EXISTS guards throughout; reuse_count defaults to 0 so
-- no existing row's ranking changes until a later ingest actually detects
-- reuse (and even then, only for a workspace that opted in AND has
-- WORKSPACE_REUSE_BOOST configured -- see search.py). No data is dropped or
-- rewritten.

ALTER TABLE document_chunks
    ADD COLUMN IF NOT EXISTS reuse_count INTEGER NOT NULL DEFAULT 0;

ALTER TABLE document_chunks
    ADD COLUMN IF NOT EXISTS last_reused_at TIMESTAMPTZ;

CREATE TABLE IF NOT EXISTS chunk_reuse_events (
    id BIGSERIAL PRIMARY KEY,
    -- The OLDER chunk that got reused -- CASCADE so a reprocessed/deleted
    -- source document's stale reuse history disappears with it (its
    -- document_chunks rows are always fully deleted+reinserted on
    -- reprocessing, so their reuse_count already resets to 0 the same way;
    -- see store_processed_document's non-append delete+insert path).
    source_chunk_id BIGINT NOT NULL REFERENCES document_chunks(id) ON DELETE CASCADE,
    reusing_document_id VARCHAR(100) NOT NULL,
    -- sha256 of the REUSING chunk's own content (document_chunks.content_hash
    -- formula, #41) -- the idempotency key described above.
    reusing_chunk_content_hash VARCHAR(64) NOT NULL,
    workspace_id VARCHAR(100) NOT NULL,
    -- The vector similarity (Weaviate `certainty`) that triggered this
    -- match, kept for observability/debugging -- never used for ranking.
    similarity DOUBLE PRECISION NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_chunk_reuse_dedupe
        UNIQUE (source_chunk_id, reusing_document_id, reusing_chunk_content_hash)
);

CREATE INDEX IF NOT EXISTS idx_chunk_reuse_events_source_chunk
    ON chunk_reuse_events(source_chunk_id);
CREATE INDEX IF NOT EXISTS idx_chunk_reuse_events_workspace_id
    ON chunk_reuse_events(workspace_id);
