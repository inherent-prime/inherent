# REST API reference

Base URL: `http://localhost:18000` on the local compose stack. All
application routes live under `/v1`. Swagger UI (`/docs`) and ReDoc
(`/redoc`) are exposed in development mode only.

Runnable curl examples for every endpoint: [API examples](../examples/README.md).

## Authentication

Send an API key on every `/v1` request, either header works:

```bash
curl -H "X-API-Key: <key>" http://localhost:18000/v1/documents
curl -H "Authorization: Bearer <key>" http://localhost:18000/v1/documents
```

- Missing/invalid/expired key → `401` (`WWW-Authenticate: ApiKey`).
- Missing permission → `403`. Errors return RFC 7807 problem-details JSON.
- A key carries a subset of three permissions: `read`, `search`, `write`
  (default `read, search`). Membership is exact — `write` does NOT imply
  `read` or `search`.

## Workspace scoping

Select a workspace with the `X-Workspace-Id` header.

- **Workspace-scoped key**: bound to its workspace; a mismatching
  `X-Workspace-Id` → `403`.
- **User-scoped key**: `X-Workspace-Id` must name a workspace the user owns
  (else `403`). Without the header, read/search fan out across all owned
  workspaces; write endpoints require an unambiguous workspace (`400` when
  the user owns zero or several).
- Cross-workspace documents read as `404`, not `403`.

The workspace is the only access-control boundary — there is no document-level
ACL or clearance parameter. See the
[access-control model](../access-control.md).

## Endpoints

### Health & observability (no auth)

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/health`, `/health/live` | Liveness — returns immediately |
| GET | `/health/ready` | Readiness — checks PostgreSQL + Weaviate; `503` when unhealthy, `degraded` stays `200` |
| GET | `/metrics` | Prometheus metrics (when `METRICS_ENABLED=true`, default) |

### Search & verification

| Method | Path | Permission | Purpose |
| --- | --- | --- | --- |
| POST | `/v1/search` | `search` | Semantic / hybrid / keyword search. Request: `query` (1–1000 chars), `limit` (1–100, default 10), `min_score`, `document_ids[]`, `include_context`, `context_window` (0–5), `search_mode` (`semantic`/`hybrid`/`keyword`), `alpha` (0–1). Response: `results[]` (score provenance, `citation`, `is_stale`, `content_risk`), `quality_verdict`, `performed_fallback`, `event_id` (for eval feedback) |
| POST | `/v1/verify-claim` | `read` | Offline lexical claim-vs-evidence check. Request: `claim` (1–2000 chars), `evidence[]`. Response: `support_level` (`strong`/`weak`/`none`), `score`, `reason` |

### Documents

| Method | Path | Permission | Purpose |
| --- | --- | --- | --- |
| POST | `/v1/documents` | `write` | Upload (multipart `file`) for async ingestion. `201` with `document_id`, `status: "pending"`. Workspace required. Identical content dedupes to the existing document |
| GET | `/v1/documents` | `read` | List documents. Query: `page`, `page_size` (1–100, default 20) |
| GET | `/v1/documents/{id}` | `read` | Document metadata (`status`, `chunk_count`, `mime_type`, timestamps). `404` if not found |
| DELETE | `/v1/documents/{id}` | `write` | Delete document + vectors + chunks + stored bytes. `204`; `404` if already gone; `503` on vector-store outage (document left intact, retry safe) |
| GET | `/v1/documents/{id}/lineage` | `read` | Provenance + freshness: `source_uri`, `content_hash`, `ingested_at`, `is_stale`. Optional `chunk_id` query param |
| POST | `/v1/documents/{id}/refresh` | `write` + `read` | Re-ingest an uploaded document to clear staleness. `404` if missing; `503` on DB/MQ failure. On MQ failure a retried compensation marks the document `failed`; if retries exhaust it can remain `pending` (CRITICAL log + `document_compensation_exhausted_total` metric) — check document status before retrying |

Re-uploading the same filename into the same workspace reuses the existing
`document_id` and reindexes in place, removing the superseded chunks from
retrieval — see [Keeping content current](../keeping-content-current.md).

### Chunks

| Method | Path | Permission | Purpose |
| --- | --- | --- | --- |
| GET | `/v1/chunks/{document_id}` | `read` | All chunks (`content`, `chunk_index`, `token_count`, `metadata`) — unbounded, unlike `/context` below |
| GET | `/v1/chunks/{document_id}/context` | `read` | Document metadata + a bounded window of chunks + combined `full_text`. Query: `max_chars` (1–100,000, default 20,000), `offset` (chars, default 0). Response adds `truncated`, `total_chars`, `offset`, `next_offset` — see below |
| GET | `/v1/chunks/{document_id}/{chunk_id}` | `read` | Single chunk. Cross-tenant chunk reads as `404` |

`GET /v1/chunks/{document_id}/context` bounds BOTH `full_text` and `chunks`
to the same `[offset, offset + max_chars)` window over the document's
combined text (#219) — a document that used to return every chunk plus a
fully concatenated `full_text` in one call (measured at 298 KB / 117,086
chars / ~29,300 tokens for a 169-chunk PDF) is now capped by default. `chunks`
is therefore "the chunks contributing to this page", not "every chunk in the
document"; a chunk straddling a page boundary can legitimately appear on two
consecutive pages. **Behavior change**: callers that relied on one call
returning the entire document must now check `truncated` and page with
`offset=next_offset` until it is `false`. `max_chars=0` or negative, or above
100,000, is a `422`.

### Evals (traffic-mined retrieval quality)

| Method | Path | Permission | Purpose |
| --- | --- | --- | --- |
| POST | `/v1/evals/feedback` | `search` | Report a verdict (`answered`/`partial`/`not_relevant`) on a search `event_id`; positive verdicts auto-promote the query to a labeled eval case |
| GET | `/v1/evals/scorecard` | `search` | Workspace retrieval health: `answer_rate`, `verdict_distribution`, `corpus_gaps`, `eval_case_count`, `low_confidence`, `last_run` |
| GET | `/v1/evals/cases` | `search` | Page labeled cases (`limit` 1–200, `offset`) |
| PATCH | `/v1/evals/cases/{case_id}` | `write` | Enable/disable a case (`{"active": bool}`) |
| POST | `/v1/evals/runs` | `write` | Start a keyword-vs-semantic-vs-hybrid comparison run over the workspace's active cases, optionally scoped (see below). `202` with `run_id`; `409` when no active cases (after scoping); `404` when a `case_ids` entry doesn't belong to the caller's workspace |
| GET | `/v1/evals/runs/{run_id}` | `search` | Run report: per-mode recall@k / MRR / nDCG aggregates + per-case metrics |
| DELETE | `/v1/evals/events` | `write` | Purge the workspace's captured search events. `?include_cases=true` also purges labeled `eval_cases` (opt-in; default leaves cases intact) |

**`POST /v1/evals/runs` optional scoping (#250).** The JSON body is optional
and every field on it is optional; omitting the body (or sending `{}`) keeps
the original behavior — replay every active case for the workspace, the
accumulate-over-time default from [ADR 0003](../adr/0003-traffic-mined-retrieval-evals.md).
Two independent, AND-able filters narrow the replay set instead:

- `case_ids: string[]` — replay only these cases. Every id must belong to the
  caller's workspace (active or disabled); an id that's unknown or belongs to
  a different workspace rejects the **whole request** with `404` — it is
  never silently dropped (which would quietly narrow the run) or silently
  accepted across tenants (which would leak existence). This is the same
  cross-tenant-id convention the rest of the API uses (e.g. chunk/document
  reads 404 on a foreign id).
- `since: string` (ISO 8601 datetime) — replay only cases created at/after
  that instant.

```bash
# Unscoped (default): replay everything ever promoted for the workspace.
curl -X POST -H "X-API-Key: $KEY" -H "X-Workspace-Id: $WS" \
  http://localhost:18000/v1/evals/runs

# Scoped: replay only the cases just promoted from this trial session.
curl -X POST -H "X-API-Key: $KEY" -H "X-Workspace-Id: $WS" \
  -H "Content-Type: application/json" \
  -d '{"case_ids": ["case_abc123", "case_def456"]}' \
  http://localhost:18000/v1/evals/runs
```

**`DELETE /v1/evals/events?include_cases=true` (#250).** By default this
endpoint only purges `eval_query_events` (raw, ephemeral search capture) —
promoted `eval_cases` are durable by design and survive the purge, matching
ADR 0003's "raw events ephemeral, labeled cases durable" contract. Passing
`include_cases=true` additionally deletes the workspace's `eval_cases`
(there is no supported way to purge cases without also purging events).
This purges captured events and labeled cases only -- `eval_feedback`,
`eval_runs`, and `eval_run_results` (run history) are left intact, so a
scorecard can report `eval_case_count: 0` next to a completed prior run
after the purge; it is not a full reset of the workspace's evals data.
Response: `{"deleted": <events>, "cases_deleted": <cases, 0 unless include_cases=true>}`.

## Rate limiting

Per-key limits (default 100 requests / 60 s window; key-specific overrides
supported). Unauthenticated or invalid-key traffic is limited per client IP
(`RATE_LIMIT_UNAUTHENTICATED`, default 30). See the
[configuration reference](configuration.md) for the toggles.
