-- Migration 016: Add active_run_id fencing column to processed_documents (#110)
--
-- #110's fix lets a fresh re-index/refresh TERMINATE a still-open prior
-- ingestion workflow run for the same document_id (Temporal
-- id_conflict_policy=TERMINATE_EXISTING) instead of colliding with it.
-- Termination stops the WORKFLOW but not any ACTIVITY it already
-- dispatched: without a heartbeat, an in-flight store_in_postgresql /
-- store_in_weaviate activity from the terminated (superseded) run keeps
-- running to completion on the worker, unaware anything happened, and its
-- eventual write can land AFTER the newer run already committed --
-- silently reverting the document to stale content while reporting
-- status='processed'.
--
-- active_run_id is a fencing token (see Kleppmann, "How to do distributed
-- locking"): each workflow run claims the document by stamping its own
-- Temporal run id here as the FIRST thing it does (create_pending_document,
-- src/temporal/activities/status.py), and the store activities
-- (src/services/database.py::store_processed_document) only commit when
-- the row's active_run_id still matches the run doing the write. A
-- superseded run's claim gets overwritten by the newer run's claim before
-- the superseded run's slower store step finishes, so its write is
-- rejected instead of applied. See docs/developer/learnings.md.
--
-- Nullable, no default: existing rows have no claim recorded, which the
-- fencing check treats as "unclaimed" (permitted to write) so this
-- migration never blocks a document that predates it.

ALTER TABLE processed_documents
    ADD COLUMN IF NOT EXISTS active_run_id VARCHAR(255);
