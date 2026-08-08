# Keeping content current

When a document changes, **re-upload it under the same filename into the same
workspace**. That reuses the existing `document_id` and reindexes in place:
the old chunks are deleted from Weaviate and PostgreSQL before the new ones
are written, so the superseded text stops being retrievable.

Uploading the new revision under a *different* filename creates a *second*
document. Both stay searchable, and the superseded one can outrank the current
one. That is the single most common cause of "the system returned the old
policy".

## How an upload is routed

`POST /v1/documents` takes a multipart `file` and the `X-Workspace-Id` header —
there is no `document_id` field on the request. The service decides whether the
upload is a new document or a reindex, in this order
(`services/inh-public-api-svc/src/services/document_intake.py`):

| Match against the workspace | Outcome |
| --- | --- |
| Same content (SHA-256 of the bytes) | **No-op.** Returns the existing document unchanged, message `"Identical content already ingested; returning existing document."` Nothing is re-embedded. (Exception: a `failed` document falls through and re-ingests.) |
| Same `original_filename`, different bytes | **Reindex in place.** Reuses the existing `document_id`, resets the row to `pending`, re-runs extract → chunk → embed → index. |
| Neither | **New document.** New `document_id`. |

Consequences to design around:

- **Filename is the document's logical identity.** Renaming a file on re-upload
  forks it into two documents. Keep the filename stable across revisions.
- **Editing whitespace only still no-ops** if the bytes are unchanged; the
  content hash, not the timestamp, decides.
- **The MCP `upload_document` tool follows the same rules** — same `filename`,
  same workspace, same routing. It has no `document_id` parameter either.

## What a reindex does to retrieval

The ingestion store activity deletes the document's existing chunks before
writing the new ones
(`services/inh-ingestion-svc/src/temporal/activities/store.py`):

1. Delete every Weaviate chunk matching `document_id` in the workspace
   collection and user tenant.
2. Write the new chunks.
3. PostgreSQL `document_chunks` rows for the document are deleted and
   re-inserted in the same activity, with a fresh `ingested_at`.

The delete step is deliberately graceful: if Weaviate errors during the delete,
the activity logs a warning and still writes the new chunks. A reindex that hits
a vector-store hiccup can therefore leave stale chunks behind. If a superseded
revision keeps surfacing after a successful reindex, delete the document
(`DELETE /v1/documents/{id}`) and upload it again.

### Re-indexing while a previous ingestion is still running

A document is processed by one Temporal workflow run at a time, addressed by
a fixed id (`ingest-{document_id}`). What happens when a reindex or refresh
is triggered again while the prior run for that document is still open
depends on which surface you use — this is per-call-site, not a blanket
guarantee:

- **Edited-content re-upload (`POST /v1/documents`) and
  `POST /v1/documents/{id}/refresh`** (including the MCP twin
  `refresh_stale_source`): the new run **supersedes** the old one — the stale
  run is terminated and the fresh request runs from a clean start. The
  newest request always wins; there is no queuing behind the older run and
  no error back to the caller for this case. If you need the *outcome* of a
  specific re-index rather than just its acceptance, poll
  `GET /v1/documents/{id}` (or the workflow status endpoint) until `status`
  leaves `pending`/`processing` rather than assuming the first request you
  sent is the one that finished. Firing several re-indexes in quick
  succession means only the *last* one's content survives — an in-flight
  ingestion can be terminated by an unrelated actor's re-index for the same
  document, not just your own.
- **`POST /ingest` on the ingestion service itself** (lower-level than the
  two above; not the normal upload path) rejects instead of superseding: a
  collision returns `409 already_running`. With `?wait=true`, a run can also
  be superseded *by one of the calls above* while you're waiting on it — that
  returns `409 superseded_by_newer_request` instead of the result you were
  waiting for.
- **Dead-letter retry** (`POST /dead-letter/{job_id}/retry`) also rejects
  instead of superseding: replaying a dead-lettered payload never terminates
  a healthy run for the same document, since the payload being replayed may
  be exactly the stale content that healthy run is correcting. A collision
  there resets the job to `pending` and returns `500`.

### Correcting a corpus that already forked

If old and new revisions were uploaded as separate documents:

1. `DELETE /v1/documents/{id}` on the superseded document — this removes its
   vectors, chunks, and stored bytes.
2. Re-upload the current revision under the filename you intend to keep.

## Refreshing without new bytes

`POST /v1/documents/{id}/refresh` (permission: `write`, and `read` is also
required) re-enqueues the **already-stored** file for re-ingestion. It returns
`status: "pending"`.

Use it when the pipeline changed and the source did not — new chunking
settings, a new embedding model, a failed ingestion to retry, or to clear
`is_stale`. It does **not** accept new bytes: the stored object referenced by
`storage_path` must still exist, and refreshing never changes content. To
change content, re-upload. The MCP twin is `refresh_stale_source`.

## `is_stale` is a flag, not a filter

`is_stale` is derived at read time by comparing a chunk's `ingested_at` against
`FRESHNESS_MAX_AGE_DAYS` (default `90`). It is surfaced on search results,
citations, and `GET /v1/documents/{id}/lineage`.

- **It measures time since ingestion, not the document's own age or authority.**
  A freshly uploaded ten-year-old policy is not stale; an unchanged, still-current
  document re-ingested a year ago is.
- **It never filters, drops, or re-ranks.** Ranking is by score alone. A stale
  chunk and a current chunk with the same score sort identically.
- **A reindex or refresh clears it**, because `ingested_at` is re-stamped.

Acting on the flag is the consuming application's job. Recency- and
authority-aware ranking is tracked as
[#161](https://github.com/inherent-prime/inherent/issues/161) — an open request,
not current behavior.

## No version history

There is no point-in-time query, version list, diff, or rollback. Migration
`004_document_versioning.sql` created versioning tables, and
`007_drop_versioning.sql` drops them; no service code reads or writes them. A
reindex is destructive in place — the previous revision's chunks are gone.

Keep your own copy of the source bytes if you need to restore a prior revision;
the object store holds only the most recent upload for a given `document_id`.

## Eval hygiene

Retrieval evaluations misread the system in predictable ways. Before running
one:

- **Use a dedicated workspace.** It bounds the candidate set to the corpus
  under test, and it keeps eval traffic out of a production workspace's mined
  eval set. Exclude it from capture with `EVAL_CAPTURE_DISABLED_WORKSPACES`, or
  purge afterwards with `DELETE /v1/evals/events`.
- **Ingest only the corpus.** A manifest CSV, a question list, a README, or any
  other operational file becomes searchable chunks like everything else.
  Manifests are especially damaging: they usually contain the eval questions
  themselves, so they match strongly on keyword and hybrid modes and displace
  the real answers. There are no ingestion exclusion rules today — the
  candidate set is exactly what you uploaded. Tracked as
  [#162](https://github.com/inherent-prime/inherent/issues/162).
- **Upload one revision per document.** If the corpus contains superseded
  material, either upload only the current revision or expect both to be
  retrievable. Shorter documents can outrank longer ones on the same topic
  under BM25 length normalization, so a terse legacy memo can beat a detailed
  current policy in `keyword` and `hybrid` modes.
- **Check the workspace layout before filing an access-control finding.** A
  single workspace holding several clearance tiers, queried with a fully
  privileged key, returns all tiers by design. See the
  [access-control model](access-control.md).

## See also

- [REST API reference](reference/rest-api.md#documents) — upload, delete,
  refresh, lineage
- [MCP tools reference](reference/mcp-tools.md) — `upload_document`,
  `refresh_stale_source`, `delete_document`
- [Configuration reference](reference/configuration.md) —
  `FRESHNESS_MAX_AGE_DAYS`, eval capture settings
- [Access-control model](access-control.md) — the workspace boundary
