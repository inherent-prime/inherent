# Configuration reference

Operator-facing environment variables, grouped by service. **Secret** marks
credentials. The release stack (`docker-compose.release.yml`) fails fast if
`POSTGRES_PASSWORD`, `INGESTION_API_KEY`, or `WEAVIATE_API_KEY` is unset,
and binds all datastore ports to `127.0.0.1`.

!!! warning "Shared names, different meanings"
    Do not set these globally in a shared `.env` — compose injects them
    per-service:

    - `SERVICE_MODE`: public-api accepts `api`/`mcp`/`both`; ingestion
      accepts `worker`/`standalone`/`migrate`. This selects the **stdio**
      MCP process only — the Streamable HTTP MCP transport (`POST /mcp`,
      #220) is mounted on the `api`/`both` REST app regardless of this
      setting; see the [MCP tools reference](mcp-tools.md#transports).
    - `API_PORT`: ingestion's standalone port (8000) vs public-api's
      override of `PORT` (8080).
    - `REDIS_URL`: ingestion's MQ backend vs public-api's distributed
      rate-limit store (public-api's MQ is `MQ_REDIS_URL`).

## inh-public-api-svc

### Core

| Variable | Default | Effect |
| --- | --- | --- |
| `SERVICE_MODE` | `both` | `api`, `mcp`, or `both`. `both`/`api` start REST **and mount the Streamable HTTP MCP transport at `POST /mcp`** on the same app/port (#220); `mcp` runs the separate **stdio** MCP process instead (self-hosters/internal dev) |
| `PORT` / `API_PORT` | `8080` / unset | HTTP port for REST **and** `/mcp` (they share one app/port) — `API_PORT` overrides `PORT` when set |
| `MCP_PORT` | `8001` | Reserved — still unused. The HTTP MCP transport is mounted on `PORT`/`API_PORT`, not a separate port (see the MCP tools reference) |
| `ENVIRONMENT` | `development` | `development`/`production`; gates HSTS and CORS behavior |
| `LOG_LEVEL` | `INFO` | Logging verbosity |

### Datastores

| Variable | Default | Effect | Secret |
| --- | --- | --- | --- |
| `DATABASE_URL` | `postgresql://postgres:postgres@localhost:5432/knowledge_base` | PostgreSQL connection (reads + document/eval writes) | yes |
| `MONGODB_URI` | `mongodb://localhost:27017` | Read-only Mongo for workspace/user ownership. Path segment is not the source of truth for the database — `MONGODB_DB_NAME` is | yes |
| `MONGODB_DB_NAME` | `main` | Mongo database name | no |
| `WEAVIATE_URL` | unset | Full Weaviate URL; overrides host/port below | no |
| `WEAVIATE_HOST` / `WEAVIATE_PORT` | `localhost` / `8080` | Weaviate address when `WEAVIATE_URL` unset | no |
| `WEAVIATE_API_KEY` | unset | Bearer key for Weaviate auth (required by the release stack) | yes |
| `AWS_S3_ENDPOINT` / `AWS_S3_BUCKET` / `AWS_S3_REGION` | `""` / `inherent-documents` / `us-east-1` | S3-compatible document storage. Bucket must match ingestion's `STORAGE_BUCKET` (#176); region must match ingestion's `AWS_REGION` (#132) — `AWS_S3_REGION` overrides it here if set, but a lone `AWS_REGION` configures this service too | no |
| `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` | `""` | S3 credentials | yes |

### MQ & rate limiting

| Variable | Default | Effect | Secret |
| --- | --- | --- | --- |
| `MQ_REDIS_URL` | `redis://localhost:6379` | Redis/Valkey URL for publishing upload events | yes |
| `MQ_UPLOAD_TOPIC` | `core.document.uploaded.v1` | Upload topic — must match the ingestion consumer | no |
| `MQ_CONVERSATION_TOPIC` | `core.conversation.turn.v1` | Conversation-turn topic (#306) — must match ingestion's `MQ_CONVERSATION_TOPIC`. One message per turn, never per batch | no |
| `REDIS_URL` | unset | Redis for distributed rate limiting; in-memory (per-process) fallback when unset | yes |
| `RATE_LIMIT_ENABLED` | `true` | Master toggle (CI/e2e sets `false`) | no |
| `RATE_LIMIT_WINDOW_SECONDS` | `60` | Window length | no |
| `RATE_LIMIT_DEFAULT` | `100` | Default per-key limit per window | no |
| `RATE_LIMIT_UNAUTHENTICATED` | `30` | Per-client-IP limit for requests without a valid key | no |
| `TRUSTED_PROXIES` | empty | Proxy IPs whose `X-Forwarded-For`/`X-Real-IP` are trusted | no |

### Search, freshness & embeddings

| Variable | Default | Effect |
| --- | --- | --- |
| `SEARCH_MAX_WORKSPACE_CONCURRENCY` | `8` | Max workspaces searched concurrently per multi-workspace request |
| `FRESHNESS_MAX_AGE_DAYS` | `90` | Evidence older than this is flagged `is_stale` (never filtered) |
| `EMBEDDING_PROVIDER` | `tei` | `tei` (default, non-negotiable) or `openai_compatible` — see [Embedding provider](#embedding-provider-model-identity-guard) below |
| `EMBEDDING_SERVICE_URL` | `http://text-embeddings-inference:80` | Base URL of the embedding endpoint |
| `EMBEDDING_API_KEY` | unset | `Authorization: Bearer <key>` sent to the provider. TEI works with none set; never logged (**secret**) |
| `EMBEDDING_MODEL_ID` | `BAAI/bge-small-en-v1.5` | Model identity — feeds the openai_compatible request body AND the model-identity guard. Must match what the provider actually serves |
| `EMBEDDING_DIM` | `384` | Embedding vector dimension |
| `EMBEDDING_TIMEOUT_S` | `5.0` | Per-request timeout (seconds) for the query embed. Deliberately smaller than inh-ingestion-svc's own default for the same var name (#311 PR #314 review finding 2) — see [Retry](#embedding-provider-model-identity-guard) below |
| `EMBEDDING_BATCH_MAX_RETRIES` | `2` | Retry attempts for the query embed. Smaller than inh-ingestion-svc's default for the same reason |
| `EMBEDDING_QUERY_RETRY_BUDGET_S` | `2.0` | Cumulative retry sleep budget (seconds) for the query embed |
| `ENABLE_RERANKER` / `ENABLE_GRAPHRAG_INDEX` / `ENABLE_HIERARCHY_INDEX` | `false` | EXPERIMENTAL retrieval scaffolding — off by default, not implemented |
| `ENABLE_DIVERSIFICATION` | `true` | Round-robin search results across `document_id` before truncating to page size, so one document can't crowd out every other result (#146). Set `false` to restore pre-2026-08-06 ranking. |
| `DIVERSIFICATION_OVER_FETCH_MULTIPLIER` | `5` | When `ENABLE_DIVERSIFICATION` is on, fetch up to `min(100, limit * this)` candidates to diversify across; ignored when off |

### Evals

| Variable | Default | Effect |
| --- | --- | --- |
| `EVAL_CAPTURE_ENABLED` | `true` | Capture search events for evals (opt-out) |
| `EVAL_RETENTION_DAYS` | `30` | Days raw events are kept before purge |
| `EVAL_CAPTURE_DISABLED_WORKSPACES` | empty | Comma-separated workspace IDs excluded from capture |
| `EVAL_MIN_SAMPLE_SIZE` | `50` | Labeled-case count below which the scorecard flags low confidence |
| `EVAL_RUN_CONCURRENCY` | `4` | Concurrent replay searches per eval run |
| `EVAL_RUN_K` | `5` | Ranking-metric cutoff (recall@k, nDCG@k) |

### Security, CORS & observability

| Variable | Default | Effect |
| --- | --- | --- |
| `API_KEY_HEADER_NAME` | `X-API-Key` | Header carrying the client API key |
| `ENABLE_HSTS` | `true` | Emit HSTS header in production |
| `ERROR_BASE_URL` | `https://api.inherent.sh/errors` | Base URL for every served RFC 7807 problem `type` URI (dev default should be `https://dev-api.inherent.sh/errors`). One setting, not a hardcoded domain (#222) |
| `CORS_ORIGINS` | inherent.sh origins | Allowed origins (wildcard in dev if unchanged) |
| `CORS_ALLOW_CREDENTIALS` / `CORS_ALLOW_METHODS` / `CORS_ALLOW_HEADERS` | `true` / all standard / `*` | CORS details (credentials forced off with wildcard origin) |
| `METRICS_ENABLED` / `METRICS_PATH` | `true` / `/metrics` | Prometheus endpoint |
| `DATABASE_HEALTH_CHECK_TIMEOUT_SECONDS` | `5.0` | Postgres health-check timeout, used by `GET /health/ready` (#203; replaces the dead `HEALTH_CHECK_TIMEOUT_SECONDS`) |
| `WEAVIATE_HEALTH_CHECK_TIMEOUT_SECONDS` | `5.0` | Weaviate health-check timeout, used by `GET /health/ready` (#203; replaces the dead `HEALTH_CHECK_TIMEOUT_SECONDS`) |
| `AUDIT_LOG_ENABLED` / `AUDIT_LOG_TOPIC` | `true` / `audit.log.write` | Audit logging + MQ topic |

## inh-ingestion-svc

### Core

| Variable | Default | Effect | Secret |
| --- | --- | --- | --- |
| `SERVICE_MODE` | `worker` | `worker`, `standalone`, or `migrate` (release init runs migrations) | no |
| `DATABASE_URL` | **required** | PostgreSQL (read/write; migration target). Boot fails if unset | yes |
| `WEAVIATE_URL` | **required** | Weaviate URL. Boot fails if unset | no |
| `WEAVIATE_API_KEY` | unset | Weaviate Bearer key | yes |
| `MONGODB_URI` / `MONGODB_DB_NAME` | `mongodb://localhost:27017` / `main` | Mongo for audit-log writes | yes / no |
| `LOG_LEVEL` | `INFO` | Logging verbosity | no |
| `INGESTION_API_KEY` | unset | Auth secret for the standalone HTTP API (release stack requires it) | yes |
| `API_HOST` / `API_PORT` | `0.0.0.0` / `8000` | Standalone HTTP API bind | no |
| `METRICS_PORT` | `9090` | Prometheus port in worker mode | no |

### Storage

| Variable | Default | Effect | Secret |
| --- | --- | --- | --- |
| `STORAGE_BACKEND` | `s3` | `s3` / `gcs` / `local` / `azure` | no |
| `STORAGE_BUCKET` | `inherent-documents` | Bucket name; must match public-api's `AWS_S3_BUCKET` (#176) — mostly a fallback, since uploads carry their own bucket in the event payload | no |
| `AWS_S3_ENDPOINT` / `AWS_REGION` | unset / `us-east-1` | S3-compatible endpoint + region. Region must match public-api's `AWS_S3_REGION` (#132) — public-api also reads this var directly | no |
| `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` | unset | S3 credentials | yes |
| `ALLOW_URL_BASED_INGESTION` | `false` | Gates `storage_backend="azure"` on `fetch_document`/`extract_text`. There is no real Azure Blob client in this codebase — `azure` means "fetch `storage_url` directly", which bypasses the #210 `storage_path`/`workspace_id` check entirely (#214). Off by default; enabling it leaves only #34's SSRF guard between a caller-supplied URL and the tenant's store | no |

### MQ

| Variable | Default | Effect | Secret |
| --- | --- | --- | --- |
| `MQ_BACKEND` | `redis` | `redis` / `pubsub` / `memory` | no |
| `REDIS_URL` | `redis://localhost:6379` | Valkey/Redis URL (when backend is `redis`) | yes |
| `MQ_UPLOAD_TOPIC` | `core.document.uploaded.v1` | Consumed upload topic — must match publisher | no |
| `MQ_COMPLETION_TOPIC` | `core.document.processed.v1` | Processed-document event topic | no |
| `MQ_CONSUMER_GROUP` | `ingestion-workers` | Consumer group | no |
| `MQ_CONVERSATION_TOPIC` | `core.conversation.turn.v1` | Consumed conversation-turn topic (#306) — must match public-api's `MQ_CONVERSATION_TOPIC`. Delivered to `ConversationMemoryWorkflow` via `signal_with_start` | no |
| `MQ_CONVERSATION_CONSUMER_GROUP` | `ingestion-conversation-workers` | Own consumer group — kept separate from `MQ_CONSUMER_GROUP` so a conversation turn is never interleaved with (or lost to) the document-upload cursor | no |
| `MQ_MAX_CONCURRENT` | `0` (→ `MAX_WORKERS`) | Backpressure: max in-flight workflow starts | no |

### Processing, embeddings & retries

| Variable | Default | Effect |
| --- | --- | --- |
| `CHUNKING_STRATEGY` | `sentences` | `tokens` / `sentences` / `paragraphs`. **#129:** only consulted for a content type with no registry entry — every currently-registered format resolves a `chunking_hint` instead (see below), so this var no longer governs chunking in practice for any of them. **No per-document override reaches the upload surface yet** (`DocumentIngestionInput.chunking_strategy` exists at the workflow layer, but neither `POST /v1/documents` nor the MCP `upload_document` tool expose it — tracked in [#198](https://github.com/inherent-prime/inherent/issues/198)); there is currently no way to force one strategy uniformly across formats after this change. |
| `MAX_CHUNK_SIZE` / `CHUNK_OVERLAP` | `1000` / `200` | Chunk sizing |
| `EMBEDDING_ENABLED` | `true` | Toggle embedding generation |
| `EMBEDDING_PROVIDER` | `tei` | `tei` (default, non-negotiable) or `openai_compatible` — see [Embedding provider](#embedding-provider-model-identity-guard) below |
| `EMBEDDING_SERVICE_URL` / `EMBEDDING_DIM` | `http://text-embeddings-inference:80` / `384` | Embedding endpoint base URL / vector dimension |
| `EMBEDDING_API_KEY` | unset | `Authorization: Bearer <key>` sent to the provider. TEI works with none set; never logged (**secret**) |
| `EMBEDDING_MODEL_ID` | `BAAI/bge-small-en-v1.5` | Model identity — feeds the openai_compatible request body AND the model-identity guard. Must match what the provider actually serves |
| `EMBEDDING_MAX_TOKENS` | `512` | Hard token budget per chunk (bge-small context window) |
| `EMBEDDING_BATCH_SIZE` / `EMBEDDING_TIMEOUT_S` | `32` / `30.0` | Texts per embedding call / per-request timeout |
| `EMBEDDING_MAX_CONCURRENCY` | `2` | In-flight batch POSTs per `embed_texts` call (#231 phase 1). The product of this and `TEMPORAL_MAX_CONCURRENT_ACTIVITIES` is the provider in-flight cap under bulk upload — raise carefully |
| `EMBEDDING_BATCH_MAX_RETRIES` | `3` | Per-batch HTTP retries with backoff+jitter before the activity fails (#229). Worst-case batch wall clock (`attempts * EMBEDDING_TIMEOUT_S + BATCH_RETRY_SLEEP_BUDGET_S` = 100s with these defaults) is included in `store_in_weaviate` StartToClose via `weaviate_store_budget.py` — see [Retry](#embedding-provider-model-identity-guard) below for why this is a DIFFERENT number from inh-public-api-svc's query path using the same var name |
| `EMBEDDING_ADOPT_UNSTAMPED_COLLECTIONS` | `false` | Ingestion-svc only. A NON-empty collection with no persisted embedding identity (created before #311) is refused by default rather than silently adopted (#311 PR #314 review finding 3) — see [Model-identity guard](#embedding-provider-model-identity-guard) below |
| `EMBEDDING_BATCH_SIZE` / `EMBEDDING_TIMEOUT_S` | `32` / `30.0` | Chunks per TEI call / per-request timeout |
| `EMBEDDING_MAX_CONCURRENCY` | `2` | In-flight TEI batch POSTs per `embed_texts` call (#231 phase 1). The product of this and `TEMPORAL_MAX_CONCURRENT_ACTIVITIES` is the TEI in-flight cap under bulk upload — raise carefully |
| `EMBEDDING_BATCH_MAX_RETRIES` | `3` | Per-batch HTTP retries with backoff+jitter before the activity fails (#229). Worst-case batch wall clock is included in both `store_in_weaviate`'s StartToClose (capped at 2h, was 15m — #298) and its `heartbeat_timeout` (~2× worst-case batch, #298) via `weaviate_store_budget.py` |
| `MAX_WORKERS` / `MAX_RETRIES` / `RETRY_DELAY_SECONDS` | `4` / `3` / `5` | Worker concurrency and retry policy |

#### Format-aware chunking (#129)

⚠️ **`CHUNKING_STRATEGY` above is the fallback, not the default.** Every
upload chunks by this precedence, resolved once per document inside the
`chunk_text` activity — and because every registered format resolves a
hint, `CHUNKING_STRATEGY` is effectively dead for normal uploads; it only
fires for a content type outside `FILE_TYPE_REGISTRY`:

1. **Per-document override** — `tokens` / `sentences` / `paragraphs` set
   directly on `DocumentIngestionInput.chunking_strategy`. Wins outright;
   format-aware dispatch below never runs. Exists at the workflow/activity
   layer only — **not yet reachable from either upload surface** (REST or
   MCP; tracked in [#198](https://github.com/inherent-prime/inherent/issues/198)).
2. **Registry `chunking_hint`** — looked up from the document's content type
   against [`FILE_TYPE_REGISTRY`](file-types.md) (`prose` / `tabular` /
   `structured` / `media`). Maps to one of three shape-aware strategies:
   - `tabular` (csv, xlsx) → row-based chunking. Never splits a row in half;
     every chunk carries the table's header row (and XLSX's `## Sheet: <name>`
     heading, when present).
   - `structured` (json, pptx) → section-based chunking, split at the
     extractor's own `## ` boundaries (PPTX slide headings). Falls back to
     size-based chunking when no such markers exist (JSON has none).
   - `prose` (txt, markdown, docx, eml, epub, rtf, odt, pdf, html) →
     unchanged sentence chunking, UNLESS the text opens with a `Key: value`
     header block (an `.eml`'s From/To/Cc/Date/Subject) — that block is then
     carried into every chunk, not just the first.
   - `media` (png) → plain size-based chunking (OCR/placeholder output has no
     structure worth preserving).
3. **`CHUNKING_STRATEGY` (this table)** — used only when neither of the above
   applies (no content type resolvable to a registry entry).

Every chunk records which strategy actually produced it in
`metadata.chunking_strategy` (`rows` / `sections` / `prose_header` /
`sentences` / `paragraphs` / `tokens`) for eval attribution. See
`services/inh-ingestion-svc/src/temporal/activities/chunk.py`'s module
docstring for the full design rationale and cost tradeoffs.

### Redaction (#307)

| Variable | Default | Effect |
| --- | --- | --- |
| `REDACTION_PATTERNS_EXTRA` | `[]` (JSON array of regex strings) | Extra self-hosted patterns for the `redact_turns` Temporal activity, applied in addition to the built-in detector set (`services/inh-ingestion-svc/src/services/redaction_patterns.py`): common API-key prefixes, JWTs, PEM private-key blocks, connection strings with embedded credentials, and a high-entropy-token catch-all. Each string is compiled as its own regex; a match is replaced with `[redacted:custom]` |

`redact_turns` runs on every `ConversationMemoryWorkflow` flush
([#306](https://github.com/inherent-prime/inherent/issues/306)), before
`chunk_conversation` — unredacted turn text never reaches the embedder or
the vector store. `REDACTION_PATTERNS_EXTRA` is live from the moment a
conversation is ingested.

⚠️ **Best-effort, not a guarantee.** This is pattern matching — it will not
catch every credential shape (a secret with no recognizable prefix and low
apparent entropy can pass through unredacted). Do not represent this as a
complete guarantee to end users; see the module docstring in
`redaction_patterns.py` for the full "honest limits" statement.

### Conversation memory (#306)

| Variable | Default | Effect |
| --- | --- | --- |
| `CONVERSATION_FLUSH_CHAR_THRESHOLD` | `4000` | `ConversationMemoryWorkflow` flushes its buffer once buffered turn text reaches this many characters — the embedding-pipeline protection: one store batch per conversation per flush instead of one per turn |
| `CONVERSATION_FLUSH_IDLE_SECONDS` | `90` | ...or once this many seconds pass since the buffer started filling, whichever comes first (size-or-idle debounce) |
| `CONVERSATION_CONTINUE_AS_NEW_TURNS` | `500` | `continue_as_new` every N turns, to bound Temporal workflow history size for a long-lived conversation |
| `CONVERSATION_IDLE_FINALIZE_HOURS` | `24` | No new turns for this long finalizes the conversation: publish `core.document.processed.v1` and let the workflow run complete, instead of waiting indefinitely |

These are resolved once, outside the workflow (`conversation_trigger.py`),
and carried through `ConversationMemoryInput` rather than read inside
`@workflow.run` — a config value that could differ between the original run
and a later replay of the same history must never be read directly inside
workflow code (Temporal determinism).

⚠️ **Non-retryable.** `redact_turns` fails a turn's own redaction pass by
dropping that turn and writing an audit row (`redaction_audit` table,
migration `019_redaction_audit.sql`) rather than retrying — retrying risks
storing the raw turn on a later attempt. Whoever wires this into #306's
workflow must call it with `RetryPolicy(maximum_attempts=1)` and must read
downstream chunk text **only** from `redact_turns`'s own output, never from
the workflow's raw pre-redaction turn buffer — see the activity's module
docstring (`src/temporal/activities/redact.py`) for the full reasoning.

### Temporal & tenancy

| Variable | Default | Effect |
| --- | --- | --- |
| `TEMPORAL_ENABLED` | `false` | Enable Temporal orchestration |
| `TEMPORAL_HOST` / `TEMPORAL_NAMESPACE` / `TEMPORAL_TASK_QUEUE` | `localhost:7233` / `default` / `document-ingestion` | Temporal wiring |
| `TEMPORAL_MAX_CONCURRENT_ACTIVITIES` / `TEMPORAL_MAX_CONCURRENT_WORKFLOW_TASKS` | `10` / `10` | Concurrency caps |
| `TEMPORAL_AUDIT_NAMESPACE` / `TEMPORAL_AUDIT_TASK_QUEUE` | `audit` / `audit-writer-queue` | Audit workflow wiring |
| `TENANT_IDLE_DAYS` | `30` | Inactivity days before a Weaviate tenant is deactivatable |
| `AUTO_CREATE_TENANTS` | `true` | Auto-create tenants on first upload |
| `AUDIT_LOG_TOPIC` / `AUDIT_CONSUMER_GROUP` | `audit.log.write` / `ingestion-audit-writers` | Audit MQ wiring |

## Compose / infrastructure

Consumed by compose interpolation or upstream images, not the Python services
(`EMBEDDING_MODEL_ID` used to live here too, back when it only configured the
TEI sidecar's own `--model-id` — #311 made both Python services read it as
well, so it now lives in the per-service embedding rows above and must agree
with whatever this same var tells the sidecar to load):

| Variable | Default | Effect | Secret |
| --- | --- | --- | --- |
| `POSTGRES_USER` / `POSTGRES_DB` | `postgres` / `knowledge_base` | Postgres identity + DB | no |
| `POSTGRES_PASSWORD` | dev `postgres`; **release: required** | Postgres password (embedded into `DATABASE_URL`) | yes |
| `WEAVIATE_API_KEY` | dev `local-dev-weaviate-key`; **release: required** | Configures Weaviate's accepted keys AND both clients | yes |
| `INHERENT_REGISTRY` / `INHERENT_VERSION` | `ghcr.io/inherent-prime` / `latest` | Release-stack image source + tag | no |

## Embedding provider & model-identity guard

Both services build their embedding client through a shared
`EmbeddingProvider` abstraction (`services/inh-contracts/src/inh_contracts/
embedding/`, #311) — switching providers, adding auth, or changing the model
is an **env-only** change; no call site in either service's code changes.

**Provider selection.** `EMBEDDING_PROVIDER` picks the backend: `tei`
(default — non-negotiable, `make up`/docker-compose with no new env vars
behaves exactly as before #311) or `openai_compatible` (any endpoint
implementing the OpenAI `/v1/embeddings` shape). Wire formats:

- **tei**: `POST /embed` with `{"inputs": [...], "truncate": true}` → a bare
  JSON list of vectors in request order. `truncate: true` tells TEI to
  silently truncate inputs longer than the model's `max_input_length`
  instead of returning 413 and crashing the whole batch.
- **openai_compatible**: `POST /v1/embeddings` with `{"model": ..., "input":
  [...]}` → `{"data": [{"index": ..., "embedding": [...]}, ...]}`. The
  adapter sorts the response by each entry's `index` rather than assuming
  the API preserves request order.

**Auth.** `EMBEDDING_API_KEY`, when set, is sent as `Authorization: Bearer
<key>` — never logged, and a key accidentally embedded in
`EMBEDDING_SERVICE_URL` itself is stripped before that URL is logged, too.
TEI accepts the header but does not require it, so an unset key is fully
supported (zero-config local dev).

**Retry.** Both the ingestion write path (`embed_texts`/`embed_text`) and
the public-api query path (`embed_query` — previously had **zero** retry,
the exact divergence #311 closes) retry transient failures (timeouts,
connection errors, 429, 5xx) with exponential backoff + jitter; 4xx other
than 429 fails fast. The total time spent SLEEPING across every retry of one
call is capped by a retry-budget constant (`BATCH_RETRY_SLEEP_BUDGET_S`,
10s, on the ingestion batch path) — an enforced ceiling, not an estimate.

**This bounds sleep, not wall clock (#311 PR #314 review finding 2).** Each
*attempt* can still independently take up to `EMBEDDING_TIMEOUT_S` before
the retry budget is even consulted — the sleep cap alone does NOT keep
retries from blowing past a caller's real timeout. The honest worst case for
one call is:

```
attempts * EMBEDDING_TIMEOUT_S + <retry sleep budget>
```

the same formula `inh-ingestion-svc/src/temporal/weaviate_store_budget.py`
has used since #228 to size the `store_in_weaviate` Temporal StartToClose
budget (100s with the batch path's defaults: 3 attempts × 30s + 10s). That
formula is also why the query path does **not** share the batch path's
numbers: `inh-public-api-svc`'s `embed_query` sits inside a synchronous,
user-facing search request with a real caller-side deadline (the #311
issue's own incident cites a 15s consumer timeout on interactive chat
search), so it uses smaller, independently-configured defaults —
`EMBEDDING_TIMEOUT_S=5` / `EMBEDDING_BATCH_MAX_RETRIES=2` /
`EMBEDDING_QUERY_RETRY_BUDGET_S=2` — whose worst case (2×5+2 = 12s) actually
fits under that ceiling, instead of the ~91.5s worst case the batch path's
defaults produced when reused here pre-fix. `inh_contracts.embedding.retry
.max_wall_clock_s` computes this formula and is what both paths' tests pin
against.

**Model-identity guard.** Weaviate collections are created with
`Configure.Vectorizer.none()` and never declare a dimension — Weaviate just
pins vector width at first insert. Querying with model A against a
collection built with model B returns plausible-looking noise with **no
error anywhere**. To prevent that, the active provider's identity
(`EMBEDDING_MODEL_ID` + `EMBEDDING_DIM`) is persisted as the Weaviate
collection's `description` and checked on every write (`inh-ingestion-svc`,
`WeaviateService`) and every vector query (`inh-public-api-svc`,
`SearchService`):

- **Matching identity** — request proceeds normally.
- **Mismatched identity** (model_id and/or dimension differ) — **hard
  error**, always, never a warning: `EmbeddingIdentityMismatchError` raises
  and the write/search fails. On the public-api query path this now
  propagates all the way out to a failed request — including from a
  multi-workspace fan-out, where a mismatch in even one workspace fails the
  whole search instead of degrading to partial results for the others
  (#311 PR #314 review finding 1). Recovery is a deliberate migration (a
  shadow-collection backfill + cutover, tracked as a #311 follow-up, not yet
  built) — not retrying the same request.
- **Unstamped collection, EMPTY** (created before #311, or by a script that
  never wrote a description, and holding zero objects) — **adopted
  silently**: the write path stamps it with the active identity the first
  time it's touched and proceeds. There is nothing yet that could be
  wrong, so this is what keeps a fresh deployment working with zero manual
  migration.
- **Unstamped collection, NOT empty** (#311 PR #314 review finding 3) —
  **refused by default**. The vectors already in it were written by
  *something*, and adopting them as the current provider's would silently
  CERTIFY that as correct without ever checking — the exact scenario an
  operator hits by upgrading through #311 and switching embedding providers
  in the **same** deploy (this PR's own MiniLM/bge-small anecdote shows how
  easily two same-dimension models go unnoticed). The write path raises
  `EmbeddingIdentityAdoptionRequiredError` (a subclass of
  `EmbeddingIdentityMismatchError`, so every existing
  `except EmbeddingIdentityMismatchError: raise` guard already catches it)
  naming the collection, unless `EMBEDDING_ADOPT_UNSTAMPED_COLLECTIONS=true`
  is set — in which case it adopts anyway and logs a WARNING
  (`embedding_identity_adopted_unstamped_nonempty_collection`) noting that
  the existing vectors were never verified. **Operational rule:** when
  upgrading through #311 on an existing deployment, deploy on the
  **existing** embedding model first (so every collection gets stamped by a
  normal write with nothing to verify) and change
  `EMBEDDING_PROVIDER`/`EMBEDDING_MODEL_ID` in a **later**, separate deploy
  — never both at once, and never with the opt-in on as a substitute for
  that sequencing.
- **Unstamped collection, query path** — the query path never adopts or
  writes Weaviate schema (public-api has no business PATCHing schema from a
  read path), and cannot cheaply prove a multi-tenant collection empty
  across every tenant the way the write path can, so it does not apply the
  write path's empty-collection rule either. It logs a WARNING
  (`querying_unstamped_legacy_collection`) once per collection and then
  proceeds — **visible, not silent** (PR #314 review finding 3), but not a
  hard failure: query has no way to fix what it finds, and by the time a
  collection is actually being queried, the write path will *usually*
  already have stamped it — this covers the window, until the next write
  touches that workspace, where a read-mostly legacy collection would
  otherwise go unbounded time with no signal that it's unguarded.

If you see `EmbeddingIdentityMismatchError` (or its
`EmbeddingIdentityAdoptionRequiredError` subclass) in logs: either
`EMBEDDING_PROVIDER`/`EMBEDDING_MODEL_ID`/`EMBEDDING_DIM` changed without a
deliberate migration (e.g. someone changed the TEI sidecar's `--model-id`,
or repointed `EMBEDDING_SERVICE_URL` at a different model, without updating
these vars to match), or two different embedding configs are pointed at the
same Weaviate instance. Fix the config to match the vector space the target
collection was actually built with, or plan a real re-embed migration if the
model genuinely needs to change. For the adoption-required case
specifically: confirm the collection's *existing* vectors actually match the
active provider before setting `EMBEDDING_ADOPT_UNSTAMPED_COLLECTIONS=true`
— the flag does not verify that for you, it only removes the guard.

## Not configurable via environment

Hard-coded in `services/inh-public-api-svc/src/config/constants.py` (change
requires a code change): max upload size (50 MB), search/pagination bounds.
Per-key rate limits are set on the `ApiKey` record itself (`rate_limit`,
default 100 — see `RATE_LIMIT_DEFAULT` above), not via a plan/tier table.
Allowed MIME types are derived from the
[file-type registry](file-types.md) (`services/inh-contracts`) rather than
hard-coded in `constants.py` directly — add a format there, not here.
