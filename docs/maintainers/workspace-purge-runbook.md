---
search:
  exclude: true
---

# Runbook: Full Workspace Purge (#395)

An operator-triggered, idempotent purge of **everything** a workspace left
behind — for data-deletion commitments (a pilot ending, a customer's
deletion request). It removes the workspace's data from every store it
touched and hands back a report proving zero residue.

## What gets purged

| Store | What | How |
|---|---|---|
| PostgreSQL | `processed_documents` (+ cascaded `document_chunks`), `workspace_metadata` | reuses the existing `DatabaseService.delete_workspace_data` |
| PostgreSQL | `dead_letter_jobs`, `ingestion_events`, `redaction_audit`, `workspace_stats_ledger` | `DatabaseService.delete_workspace_side_tables` |
| PostgreSQL | `eval_query_events`, `eval_feedback`, `eval_cases`, `eval_runs` (+ cascaded `eval_run_results`) | `DatabaseService.delete_workspace_eval_data` (these tables are written by `inh-public-api-svc`'s eval capture, but live in the same database ingestion already connects to) |
| Weaviate | the workspace's whole collection | reuses the existing `WeaviateService.delete_workspace_collection` |
| MongoDB | `audit_logs` referencing the workspace | **purged by default** — see "Audit log retention" below |
| API keys | every active key scoped to the workspace | **revoked, not deleted** — see "Why revoke, not delete" below |

Nothing to purge today for: per-workspace object storage (this codebase has
none — `StorageService` only fetches from a caller-supplied source URL, it
holds nothing durable of its own to delete) and Redis/Valkey (the message
queue is a transient, topic-scoped stream with no per-workspace keys).
`ingestion_staging` is keyed by `workflow_run_id`, not `workspace_id`, and
already self-cleans on workflow completion; cancelling in-flight ingestion
(below) stops it from growing further for this workspace.

## Ordering: stop writes before deleting anything

1. **Mark the workspace `purging`.** `ensure_workspace_ready` (the first
   step of every ingestion) now checks this marker and refuses new writes
   for a workspace that is purging or already purged.
2. **Cancel in-flight ingestion.** Any document still `pending`/`processing`
   in this workspace has its Temporal ingestion workflow cancelled, so a
   slow run can't write data back in after later steps have already counted
   the workspace as empty.
3. **Delete, store by store** (the table above).
4. **Revoke API keys.**
5. **Mark the workspace `purged`** (terminal state).
6. **Verify**: count what's left, per store. This is the proof-of-purge
   report.
7. **Record a durable receipt** (see below).

Every step is idempotent — re-running the whole purge after a partial
failure just deletes zero rows for the steps that already ran.

## How to trigger a purge

```bash
curl -X POST "$INGESTION_URL/admin/workspaces/<workspace_id>/purge" \
  -H "X-API-Key: $INGESTION_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"operator": "you@example.com"}'
# => 202 {"purge_workflow_id": "purge-<workspace_id>", "workspace_id": "<workspace_id>"}

curl "$INGESTION_URL/admin/workspaces/<workspace_id>/purge/purge-<workspace_id>" \
  -H "X-API-Key: $INGESTION_API_KEY"
# => {"status": "purging", ...}                          (while running)
# => {"status": "completed", "residue": {...}, "verified": true, "receipt": {...}}
```

Calling `POST .../purge` again for the same workspace while a purge is
already running (or after it completed) attaches to the SAME job — the
workflow id is deterministic (`purge-<workspace_id>`) — so triggering twice
is safe.

**Auth**: the same `X-API-Key` (`INGESTION_API_KEY`) that guards every other
mutating ingestion route. This is ingestion's internal/operator secret —
no per-tenant customer ever holds it — so no new auth mechanism was
introduced for this endpoint.

## Audit log retention

By default, purging **deletes** the workspace's Mongo `audit_logs` too. The
data-deletion commitment this feature exists for is described as covering
"index and logs" — so audit-log deletion is the default, not an opt-in.

Pass `"retain_audit_logs": true` in the purge request only when a
retention requirement on the audit trail needs to outlive the workspace's
own data. The trade-off: retained audit logs still name the (now-deleted)
`workspace_id` and can be read later, but they contain no document content
themselves (see `docs/reference/audit-log.md`) — they are query
metadata/attribution, not the underlying index.

## Why revoke, not delete, API keys

A revoked key still shows who had access to a now-purged workspace and when
access was cut off — audit value that a hard delete would throw away. Every
key lookup already treats a non-`active` status as unusable, so revoking is
sufficient to stop all further use; nothing can still authenticate with a
revoked key.

## Reading the receipt

`GET .../purge/<purge_workflow_id>` returns the durable receipt once the
purge has completed:

```json
{
  "purge_workflow_id": "purge-ws_abc",
  "workspace_id": "ws_abc",
  "operator": "you@example.com",
  "retain_audit_logs": false,
  "counts_before": {"processed_documents": 42, "...": "..."},
  "counts_after": {"processed_documents": 0, "...": "..."},
  "verified": true,
  "requested_at": "...",
  "completed_at": "..."
}
```

`verified: true` means every "after" count was zero — this IS the proof a
customer or auditor can be shown. The receipt holds **counts only, never
document content** — it is safe to share as evidence a deletion commitment
was honored. If `verified` is `false`, re-run the purge (step 1's `POST`
again): every step is idempotent, so re-running is always safe, and it will
finish the job.

## CLI

`inh-cli` has no admin command group today (`documents`, `search`,
`connect`, `identity`, `eval` only) — adding `inherent admin purge-workspace
<id>` would mean inventing that category from scratch, which is out of
scope here. Operators use the HTTP endpoint above (or a direct `curl`/script
against it) until an admin CLI surface exists.
