# inh-ingestion-svc

Document ingestion service for the Inherent knowledge base OSS core.

This service processes uploaded documents, extracts text, chunks content, generates embeddings, and stores results in PostgreSQL and Weaviate. It is also responsible for consuming document upload events and orchestrating ingestion workflows through Temporal.

## Service Modes

Configure `SERVICE_MODE` with one of these values:

| Mode | Description |
| --- | --- |
| `worker` | Recommended default. Runs the Temporal worker, MQ subscriptions, metrics server, and HTTP API when `INGESTION_API_KEY` is set. |
| `standalone` | Runs the HTTP API for manual ingestion triggers. |

Legacy mode names such as `pubsub`, `temporal_worker`, `temporal_trigger`, and `temporal_all` are mapped internally to `worker` for backward compatibility, but new documentation and local setup should use `worker`.

## Local Development

### Dependencies

- Python 3.11+
- `uv`
- PostgreSQL
- Weaviate
- Valkey
- MongoDB
- Temporal
- optional S3-compatible storage when using `STORAGE_BACKEND=s3`

The root `docker-compose.yml` starts the full local dependency stack.

### Install

```bash
uv sync --extra dev --group dev
```

### Run in Standalone Mode

```bash
SERVICE_MODE=standalone \
DATABASE_URL=postgresql://postgres:postgres@localhost:15432/knowledge_base \
WEAVIATE_URL=http://localhost:18080 \
REDIS_URL=redis://localhost:16379 \
MONGODB_URI=mongodb://localhost:27018 \
TEMPORAL_ENABLED=true \
TEMPORAL_HOST=localhost:17233 \
INGESTION_API_KEY=dev-ingestion-key \
AWS_ACCESS_KEY_ID=S3RVER \
AWS_SECRET_ACCESS_KEY=S3RVER \
AWS_REGION=us-east-1 \
AWS_S3_ENDPOINT=http://localhost:19000 \
STORAGE_BACKEND=s3 \
STORAGE_BUCKET=inherent-documents \
EMBEDDING_SERVICE_URL=http://localhost:18088 \
uv run python -m src.main
```

### Run in Worker Mode

```bash
SERVICE_MODE=worker \
DATABASE_URL=postgresql://postgres:postgres@localhost:15432/knowledge_base \
WEAVIATE_URL=http://localhost:18080 \
REDIS_URL=redis://localhost:16379 \
MONGODB_URI=mongodb://localhost:27018 \
TEMPORAL_ENABLED=true \
TEMPORAL_HOST=localhost:17233 \
INGESTION_API_KEY=dev-ingestion-key \
AWS_ACCESS_KEY_ID=S3RVER \
AWS_SECRET_ACCESS_KEY=S3RVER \
AWS_REGION=us-east-1 \
AWS_S3_ENDPOINT=http://localhost:19000 \
STORAGE_BACKEND=s3 \
STORAGE_BUCKET=inherent-documents \
EMBEDDING_SERVICE_URL=http://localhost:18088 \
uv run python -m src.main
```

## HTTP API

When `INGESTION_API_KEY` is set, the service exposes an HTTP API on `API_PORT` (default `8000`;
mapped to `18002` in the local Compose stack). This is the **write/admin plane** — it owns writes to
PostgreSQL and Weaviate. All protected routes authenticate with `X-API-Key: $INGESTION_API_KEY`.

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `GET` | `/health` | none | Liveness + Temporal worker status |
| `GET` | `/metrics` | none | Prometheus metrics |
| `POST` | `/ingest` | yes | Trigger ingestion. Async: **202** + `workflow_id`; `?wait=true` → **200** result; **403** if `storage_path` isn't prefixed by this request's own `workspace_id` (#210 — consistency check, not caller entitlement, see #177); **409** if already running |
| `GET` | `/ingest/{document_id}/status?workspace_id=` | yes | Real-time workflow progress (`step`, `progress`, `chunks_created`). 404 if `document_id` isn't owned by `workspace_id` (#177) |
| `PATCH` | `/chunks/{document_id}/{chunk_index}?workspace_id=` | yes | Edit a chunk (updates PG + re-embeds in Weaviate). 404 if `document_id` isn't owned by `workspace_id` or `chunk_index` is out of range (#134); 500 if PG updated but the Weaviate re-embed didn't (recorded via `GET /lineage`) |
| `DELETE` | `/documents/{document_id}?workspace_id=&user_id=` | yes | Delete a document from PG + Weaviate (best-effort). 404 if `document_id` isn't owned by `workspace_id` (#175) |
| `GET` | `/lineage/{document_id}?workspace_id=` | yes | Ordered ingestion pipeline events for a document. 404 if `document_id` isn't owned by `workspace_id` (#177) |
| `GET` | `/dead-letter?workspace_id=` | yes | List failed-ingestion (dead-letter) jobs for `workspace_id` (**required**, #177); filters `status`, `limit` |
| `GET` | `/dead-letter/{job_id}?workspace_id=` | yes | Get a single dead-letter job. 404 if missing or not owned by `workspace_id` (#177) |
| `POST` | `/dead-letter/{job_id}/retry?workspace_id=` | yes | Re-publish a job's original message. 404 if not owned by `workspace_id` (#177); 409 if not retriable |
| `POST` | `/dead-letter/{job_id}/abandon?workspace_id=` | yes | Mark a dead-letter job permanently abandoned. 404 if not owned by `workspace_id` (#177) |

Every route above that takes a `document_id` or dead-letter `job_id` now also requires and
verifies `workspace_id` against PostgreSQL (#134, #175, #177) -- `verify_api_key` alone only
proves the caller holds the one shared `INGESTION_API_KEY`, not that it's entitled to a specific
workspace's data. This is workspace<->row *consistency*, not caller<->workspace *entitlement*;
see [`src/api/ownership.py`](src/api/ownership.py) for the full picture and why this API should
never be exposed outside the internal network.

Copy-paste curl examples for every endpoint (and a Postman collection) live in
[`docs/examples/README.md`](../../docs/examples/README.md).

### Health Check

```bash
curl http://localhost:8000/health
```

### Trigger Ingestion

Returns **202 Accepted** with a `workflow_id` (async). Append `?wait=true` to block until the
workflow finishes and receive the full result as **200 OK**.

```bash
curl -X POST http://localhost:8000/ingest \
  -H "X-API-Key: dev-ingestion-key" \
  -H "Content-Type: application/json" \
  -d '{
    "document_id": "doc_001",
    "workspace_id": "ws_001",
    "user_id": "user_001",
    "filename": "report.pdf",
    "original_filename": "report.pdf",
    "content_type": "application/pdf",
    "size_bytes": 102400,
    "storage_backend": "s3",
    "storage_path": "workspaces/ws_001/report.pdf"
  }'
```

## Validation Commands

```bash
uv run ruff check src tests
uv run black --check src tests
uv run pytest
```

## Project Layout

```text
src/
  api/         FastAPI app and auth helpers
  config/      Settings and environment handling
  connectors/  File source adapters
  models/      Pydantic models
  services/    Database, storage, embedding, MQ, and processing logic
  temporal/    Worker, trigger, and workflow-related components
  utils/       Logging and helpers
```
