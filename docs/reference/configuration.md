# Configuration reference

Operator-facing environment variables, grouped by service. **Secret** marks
credentials. The release stack (`docker-compose.release.yml`) fails fast if
`POSTGRES_PASSWORD`, `INGESTION_API_KEY`, or `WEAVIATE_API_KEY` is unset,
and binds all datastore ports to `127.0.0.1`.

!!! warning "Shared names, different meanings"
    Do not set these globally in a shared `.env` — compose injects them
    per-service:

    - `SERVICE_MODE`: public-api accepts `api`/`mcp`/`both`; ingestion
      accepts `worker`/`standalone`/`migrate`.
    - `API_PORT`: ingestion's standalone port (8000) vs public-api's
      override of `PORT` (8080).
    - `REDIS_URL`: ingestion's MQ backend vs public-api's distributed
      rate-limit store (public-api's MQ is `MQ_REDIS_URL`).

## inh-public-api-svc

### Core

| Variable | Default | Effect |
| --- | --- | --- |
| `SERVICE_MODE` | `both` | `api`, `mcp`, or `both` (`both` starts REST; run MCP as a separate `mcp` process) |
| `PORT` / `API_PORT` | `8080` / unset | HTTP port; `API_PORT` overrides `PORT` when set |
| `MCP_PORT` | `8001` | Reserved — unused; MCP runs on stdio (see the MCP tools reference) |
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
| `EMBEDDING_SERVICE_URL` | `http://text-embeddings-inference:80` | TEI sidecar base URL |
| `EMBEDDING_DIM` | `384` | Embedding vector dimension |
| `EMBEDDING_TIMEOUT_S` | `30.0` | Per-request TEI timeout (seconds) |
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
| `CORS_ORIGINS` | inherent.systems origins | Allowed origins (wildcard in dev if unchanged) |
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
| `MQ_MAX_CONCURRENT` | `0` (→ `MAX_WORKERS`) | Backpressure: max in-flight workflow starts | no |

### Processing, embeddings & retries

| Variable | Default | Effect |
| --- | --- | --- |
| `CHUNKING_STRATEGY` | `sentences` | `tokens` / `sentences` / `paragraphs`. **#129:** only consulted for a content type with no registry entry — every currently-registered format resolves a `chunking_hint` instead (see below), so this var no longer governs chunking in practice for any of them. **No per-document override reaches the upload surface yet** (`DocumentIngestionInput.chunking_strategy` exists at the workflow layer, but neither `POST /v1/documents` nor the MCP `upload_document` tool expose it — tracked in [#198](https://github.com/inherent-prime/inherent/issues/198)); there is currently no way to force one strategy uniformly across formats after this change. |
| `MAX_CHUNK_SIZE` / `CHUNK_OVERLAP` | `1000` / `200` | Chunk sizing |
| `EMBEDDING_ENABLED` | `true` | Toggle embedding generation |
| `EMBEDDING_SERVICE_URL` / `EMBEDDING_DIM` | `http://text-embeddings-inference:80` / `384` | TEI sidecar |
| `EMBEDDING_MAX_TOKENS` | `512` | Hard token budget per chunk (bge-small context window) |
| `EMBEDDING_BATCH_SIZE` / `EMBEDDING_TIMEOUT_S` | `32` / `30.0` | Chunks per TEI call / per-request timeout |
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

Consumed by compose interpolation or upstream images, not the Python services:

| Variable | Default | Effect | Secret |
| --- | --- | --- | --- |
| `POSTGRES_USER` / `POSTGRES_DB` | `postgres` / `knowledge_base` | Postgres identity + DB | no |
| `POSTGRES_PASSWORD` | dev `postgres`; **release: required** | Postgres password (embedded into `DATABASE_URL`) | yes |
| `WEAVIATE_API_KEY` | dev `local-dev-weaviate-key`; **release: required** | Configures Weaviate's accepted keys AND both clients | yes |
| `EMBEDDING_MODEL_ID` | `BAAI/bge-small-en-v1.5` | Model the TEI sidecar loads | no |
| `INHERENT_REGISTRY` / `INHERENT_VERSION` | `ghcr.io/inherent-prime` / `latest` | Release-stack image source + tag | no |

## Not configurable via environment

Hard-coded in `services/inh-public-api-svc/src/config/constants.py` (change
requires a code change): max upload size (50 MB), search/pagination bounds.
Per-key rate limits are set on the `ApiKey` record itself (`rate_limit`,
default 100 — see `RATE_LIMIT_DEFAULT` above), not via a plan/tier table.
Allowed MIME types are derived from the
[file-type registry](file-types.md) (`services/inh-contracts`) rather than
hard-coded in `constants.py` directly — add a format there, not here.
