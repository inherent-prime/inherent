# inh-public-api-svc

Public API service for the Inherent knowledge base OSS core.

This service exposes a REST API over indexed documents stored by the ingestion service. It can also run an MCP server from the same codebase, but the default local Compose flow is centered on the REST API.

## Service Modes

Configure `SERVICE_MODE` with one of these values:

| Mode | Description |
| --- | --- |
| `api` | Run the REST API only. |
| `mcp` | Run the MCP server over stdio. |
| `both` | Current implementation path used by local Compose. Starts the REST API entrypoint. |

## Local Development

### Dependencies

- Python 3.11+
- `uv`
- PostgreSQL
- Weaviate
- MongoDB
- Valkey
- optional S3-compatible storage when using document upload flows

The root `docker-compose.yml` starts the shared local dependency stack.

### Install

```bash
uv sync --extra dev --group dev
```

### Run the REST API

```bash
SERVICE_MODE=api \
PORT=8080 \
DATABASE_URL=postgresql://postgres:postgres@localhost:15432/knowledge_base \
WEAVIATE_URL=http://localhost:18080 \
MONGODB_URI=mongodb://localhost:27018/main \
MQ_REDIS_URL=redis://localhost:16379 \
REDIS_URL=redis://localhost:16379 \
AWS_S3_ENDPOINT=http://localhost:19000 \
AWS_ACCESS_KEY_ID=S3RVER \
AWS_SECRET_ACCESS_KEY=S3RVER \
AWS_S3_BUCKET=inherent-documents \
EMBEDDING_SERVICE_URL=http://localhost:18088 \
uv run python -m src.main
```

### Run the MCP Server

```bash
SERVICE_MODE=mcp \
DATABASE_URL=postgresql://postgres:postgres@localhost:15432/knowledge_base \
WEAVIATE_URL=http://localhost:18080 \
MONGODB_URI=mongodb://localhost:27018/main \
uv run python -m src.main
```

The MCP server uses stdio rather than an HTTP transport in the current implementation.

## REST API Endpoints

| Endpoint | Method | Description |
| --- | --- | --- |
| `/health` | `GET` | Liveness check |
| `/health/ready` | `GET` | Readiness check for backing services |
| `/v1/search` | `POST` | Semantic, hybrid, or keyword search across indexed documents |
| `/v1/documents` | `GET` | List documents in accessible workspaces |
| `/v1/documents/{id}` | `GET` | Get document metadata |
| `/v1/documents` | `POST` | Upload a document and enqueue ingestion |
| `/v1/documents/{id}` | `DELETE` | Delete a document (vectors + chunks + stored bytes) |
| `/v1/documents/{id}/refresh` | `POST` | Re-ingest an already-uploaded document (clear staleness) |
| `/v1/documents/{id}/lineage` | `GET` | Explain a document's (or chunk's) provenance and freshness |
| `/v1/verify-claim` | `POST` | Verify a claim against supplied evidence passages |
| `/v1/chunks/{doc_id}` | `GET` | List chunks for a document |
| `/v1/chunks/{doc_id}/{chunk_id}` | `GET` | Fetch a single chunk by id |
| `/v1/chunks/{doc_id}/context` | `GET` | Reconstruct document context from chunks |
| `/v1/evals/feedback` | `POST` | Report a verdict on a captured search event; auto-promotes to a labeled eval case |
| `/v1/evals/scorecard` | `GET` | Workspace retrieval scorecard (answer rate, corpus gaps, labeled-case count) |
| `/v1/evals/cases` | `GET` | Page through labeled eval cases for the workspace |
| `/v1/evals/cases/{case_id}` | `PATCH` | Enable or disable an eval case |
| `/v1/evals/runs` | `POST` | Start a keyword/semantic/hybrid mode-comparison eval run (202, runs in background) |
| `/v1/evals/runs/{run_id}` | `GET` | Fetch an eval run's summary + per-case metrics |
| `/v1/evals/events` | `DELETE` | Purge captured search events for the workspace |

## MCP Tools

| Tool | Description |
| --- | --- |
| `search_documents` | Semantic, hybrid, or keyword search across one or more workspaces |
| `search_memory` | Search-shaped memory primitive (same knobs as `search_documents`) |
| `get_citations` | Retrieve citation objects for a query |
| `get_document` | Get document metadata by id |
| `get_document_context` | Retrieve the full content of a document |
| `list_documents` | List accessible documents |
| `list_chunks` | List a document's chunks |
| `upload_document` | Ingest a text document (markdown/plain text); binary formats stay REST-only |
| `verify_claim` | Verify a claim against supplied evidence passages |
| `explain_lineage` | Explain a document's (or chunk's) provenance and freshness |
| `refresh_stale_source` | Re-ingest an already-uploaded document (clear staleness) |
| `delete_document` | Permanently delete a document (vectors + chunks + stored bytes) |
| `report_feedback` | Record a verdict on a captured search event (closes the evals feedback loop) |
| `get_retrieval_health` | Retrieve the workspace's retrieval scorecard |

## Validation Commands

```bash
uv run ruff check src tests
uv run black --check src tests
uv run mypy src
uv run bandit -c pyproject.toml -r src
uv run pytest
```

## Project Layout

```text
src/
  api/         FastAPI routes
  config/      Settings and constants
  core/        Shared exceptions and response helpers
  mcp_server/  MCP server implementation
  middleware/  Auth, rate limiting, audit, and error handling
  models/      Request and response models
  services/    Database, search, MQ, storage, metrics, and auth logic
  utils/       Logging and validation helpers
```
