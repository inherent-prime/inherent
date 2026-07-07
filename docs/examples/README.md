# Inherent — Request Examples

Use Inherent to make your documents searchable, run queries over your knowledge base, and manage indexed content via REST. This page maps common user goals to the API calls that accomplish them, then gives ready-to-run curl snippets for every endpoint.

Prefer a GUI? The same endpoints are also available as a [Postman Collection](#postman-collection) and a [Bruno Collection](#bruno-collection) alongside this file.

## Prerequisites

- Stack running locally (`docker compose up --build`)
- [`jq`](https://jqlang.github.io/jq/) installed (used to pretty-print responses in all snippets)

Verify stack is up:

```bash
curl http://localhost:18000/health
curl http://localhost:18002/health
```

## Bootstrap: Create a Dev API Key

The database ships with no seed keys. API keys **must start with `ink_`** — any other prefix is rejected immediately.

Run once after `docker compose up --build`:

```bash
make dev-seed
```

Safe to re-run — skips if key already exists. Expected output: `Done. API key ready: ink_dev_local_key_001`

## Variables

All examples use these shell variables. Set them once:

```bash
export API_BASE="http://localhost:18000"
export INGEST_BASE="http://localhost:18002"
export API_KEY="ink_dev_local_key_001"  # created by make dev-seed
export WORKSPACE_ID="ws_local_001"      # matches workspace seeded above
export INGEST_KEY="dev-ingestion-key"   # set via INGESTION_API_KEY in docker-compose
```

---

## Common Workflows

Not sure which endpoint to call? Start here.

| Goal | What to call |
|------|-------------|
| Make a document searchable | [Upload](#2-upload-a-document) → [Trigger ingestion](#7-trigger-ingestion-manually) → [Poll status](#8-get-ingestion-status) |
| Search your knowledge base | [Search](#5-search) — semantic (default), hybrid, or keyword |
| Inspect indexed content | [Fetch chunks](#6-fetch-chunks) — list chunks or get the full reconstructed text |
| Fix or refine a chunk | [Edit a chunk](#9-edit-a-chunk) — replaces content and re-embeds in Weaviate |
| Debug why a document wasn't indexed | [Ingestion status](#8-get-ingestion-status) → [Lineage](#10-document-lineage) → [Dead-letter jobs](#12-dead-letter-jobs) |
| Retry a failed ingestion | [Dead-letter jobs → Retry](#12-dead-letter-jobs) |
| Remove a document | [Delete a document](#11-delete-a-document) — clears PostgreSQL and Weaviate |

---

## 1. Health

### Liveness (public API)

```bash
curl -s "$API_BASE/health" | jq .
```

**Expected response:**

```json
{
  "status": "healthy",
  "service": "inh-public-api-svc"
}
```

### Readiness (public API — checks PostgreSQL + Weaviate)

```bash
curl -s "$API_BASE/health/ready" | jq .
```

**Expected response:**

```json
{
  "status": "healthy",
  "timestamp": "2024-01-15T10:00:00.000000+00:00",
  "version": "0.1.0",
  "service": "inh-public-api-svc",
  "checks": {
    "database": { "status": "healthy", "latency_ms": 4.2 },
    "weaviate":  { "status": "healthy", "latency_ms": 12.7 }
  }
}
```

**Degraded example** (Weaviate slow but database OK):

```json
{
  "status": "degraded",
  "checks": {
    "database": { "status": "healthy",  "latency_ms": 3.1 },
    "weaviate":  { "status": "degraded", "latency_ms": 612.0, "message": "High latency detected" }
  }
}
```

### Liveness (ingestion API)

```bash
curl -s "$INGEST_BASE/health" | jq .
```

---

## 2. Upload a Document

Upload registers a file in storage and queues it for ingestion. The returned `document_id` is what every other endpoint uses to reference this document.

Allowed MIME types: `text/plain`, `text/markdown`, `text/csv`, `text/html`,
`application/pdf`, `application/json`,
`application/vnd.openxmlformats-officedocument.wordprocessingml.document`.
Max size: 50 MB.

### Upload plain text

```bash
curl -s -X POST "$API_BASE/v1/documents" \
  -H "X-API-Key: $API_KEY" \
  -H "X-Workspace-Id: $WORKSPACE_ID" \
  -F "file=@docs/examples/sample-documents/sample.txt;type=text/plain" \
  | jq .
```

### Upload Markdown

```bash
curl -s -X POST "$API_BASE/v1/documents" \
  -H "X-API-Key: $API_KEY" \
  -H "X-Workspace-Id: $WORKSPACE_ID" \
  -F "file=@docs/examples/sample-documents/sample.md;type=text/markdown" \
  | jq .
```

### Upload JSON

```bash
curl -s -X POST "$API_BASE/v1/documents" \
  -H "X-API-Key: $API_KEY" \
  -H "X-Workspace-Id: $WORKSPACE_ID" \
  -F "file=@docs/examples/sample-documents/sample.json;type=application/json" \
  | jq .
```

### Upload CSV

```bash
curl -s -X POST "$API_BASE/v1/documents" \
  -H "X-API-Key: $API_KEY" \
  -H "X-Workspace-Id: $WORKSPACE_ID" \
  -F "file=@docs/examples/sample-documents/sample.csv;type=text/csv" \
  | jq .
```

### Upload HTML

```bash
curl -s -X POST "$API_BASE/v1/documents" \
  -H "X-API-Key: $API_KEY" \
  -H "X-Workspace-Id: $WORKSPACE_ID" \
  -F "file=@docs/examples/sample-documents/sample.html;type=text/html" \
  | jq .
```

### Upload PDF

```bash
curl -s -X POST "$API_BASE/v1/documents" \
  -H "X-API-Key: $API_KEY" \
  -H "X-Workspace-Id: $WORKSPACE_ID" \
  -F "file=@docs/examples/sample-documents/sample.pdf;type=application/pdf" \
  | jq .
```

### Upload DOCX

```bash
curl -s -X POST "$API_BASE/v1/documents" \
  -H "X-API-Key: $API_KEY" \
  -H "X-Workspace-Id: $WORKSPACE_ID" \
  -F "file=@docs/examples/sample-documents/sample.docx;type=application/vnd.openxmlformats-officedocument.wordprocessingml.document" \
  | jq .
```

**Expected response (201):**

```json
{
  "document_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "name": "sample.txt",
  "workspace_id": "ws_local_001",
  "storage_url": "s3://inherent-documents/ws_local_001/3fa85f64-5717-4562-b3fc-2c963f66afa6/sample.txt",
  "mime_type": "text/plain",
  "size_bytes": 842,
  "status": "pending",
  "message": "Document uploaded successfully. Processing will begin shortly."
}
```

Save the document ID for later examples:

```bash
export DOC_ID="3fa85f64-5717-4562-b3fc-2c963f66afa6"  # replace with your value
```

### Upload error cases

**Unsupported file type (400):**

```bash
curl -s -X POST "$API_BASE/v1/documents" \
  -H "X-API-Key: $API_KEY" \
  -H "X-Workspace-Id: $WORKSPACE_ID" \
  -F "file=@/etc/hosts;type=application/octet-stream" \
  | jq .
```

```json
{
  "type": "https://api.inherent.systems/errors/bad-request",
  "title": "Bad Request",
  "status": 400,
  "detail": "Unsupported file type 'application/octet-stream'. Allowed types: text/plain, text/markdown, text/csv, text/html, application/pdf, application/json, application/vnd.openxmlformats-officedocument.wordprocessingml.document"
}
```

**Missing API key (401):**

```bash
curl -s -X POST "$API_BASE/v1/documents" \
  -H "X-Workspace-Id: $WORKSPACE_ID" \
  -F "file=@docs/examples/sample-documents/sample.txt;type=text/plain" \
  | jq .
```

```json
{
  "detail": "API key required. Provide X-API-Key header or Authorization: Bearer <key>"
}
```

---

## 3. List Documents

Lists all documents in your workspace. The `status` field shows whether a document is still in the pipeline (`pending`) or ready to search (`processed`).

```bash
curl -s "$API_BASE/v1/documents" \
  -H "X-API-Key: $API_KEY" \
  -H "X-Workspace-Id: $WORKSPACE_ID" \
  | jq .
```

### With pagination

```bash
curl -s "$API_BASE/v1/documents?page=1&page_size=5" \
  -H "X-API-Key: $API_KEY" \
  -H "X-Workspace-Id: $WORKSPACE_ID" \
  | jq .
```

**Expected response:**

```json
{
  "documents": [
    {
      "id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
      "name": "sample.txt",
      "workspace_id": "ws_local_001",
      "source_type": "upload",
      "mime_type": "text/plain",
      "size_bytes": 842,
      "chunk_count": 4,
      "status": "processed",
      "created_at": "2024-01-15T10:00:00+00:00",
      "updated_at": "2024-01-15T10:00:05+00:00",
      "metadata": null
    }
  ],
  "total": 1,
  "page": 1,
  "page_size": 5
}
```

---

## 4. Get Document by ID

Check a single document's current status and metadata. Use this to poll until `status` is `processed` before running a search.

```bash
curl -s "$API_BASE/v1/documents/$DOC_ID" \
  -H "X-API-Key: $API_KEY" \
  -H "X-Workspace-Id: $WORKSPACE_ID" \
  | jq .
```

**Expected response:**

```json
{
  "id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "name": "sample.txt",
  "workspace_id": "ws_local_001",
  "source_type": "upload",
  "mime_type": "text/plain",
  "size_bytes": 842,
  "chunk_count": 4,
  "status": "processed",
  "created_at": "2024-01-15T10:00:00+00:00",
  "updated_at": "2024-01-15T10:00:05+00:00",
  "metadata": null
}
```

**Document not found (404):**

```bash
curl -s "$API_BASE/v1/documents/does-not-exist" \
  -H "X-API-Key: $API_KEY" \
  -H "X-Workspace-Id: $WORKSPACE_ID" \
  | jq .
```

```json
{
  "detail": "Document not found"
}
```

---

## 5. Search

Query your indexed knowledge base. Semantic search matches by meaning (default); hybrid blends semantic and keyword scoring; keyword matches exact terms. All modes return ranked chunks with relevance scores.

Wait for ingestion to complete (`status: "processed"` in list/get) before searching.

### Semantic search (default)

```bash
curl -s -X POST "$API_BASE/v1/search" \
  -H "X-API-Key: $API_KEY" \
  -H "X-Workspace-Id: $WORKSPACE_ID" \
  -H "Content-Type: application/json" \
  -d '{
    "query": "what retrieval modes does Inherent support",
    "limit": 5
  }' \
  | jq .
```

### Hybrid search

```bash
curl -s -X POST "$API_BASE/v1/search" \
  -H "X-API-Key: $API_KEY" \
  -H "X-Workspace-Id: $WORKSPACE_ID" \
  -H "Content-Type: application/json" \
  -d '{
    "query": "document ingestion pipeline",
    "limit": 5,
    "search_mode": "hybrid",
    "alpha": 0.6
  }' \
  | jq .
```

### Keyword search

```bash
curl -s -X POST "$API_BASE/v1/search" \
  -H "X-API-Key: $API_KEY" \
  -H "X-Workspace-Id: $WORKSPACE_ID" \
  -H "Content-Type: application/json" \
  -d '{
    "query": "Weaviate PostgreSQL",
    "limit": 5,
    "search_mode": "keyword"
  }' \
  | jq .
```

### Search with context window

Includes surrounding chunks for each result — useful for MCP-style context retrieval.

```bash
curl -s -X POST "$API_BASE/v1/search" \
  -H "X-API-Key: $API_KEY" \
  -H "X-Workspace-Id: $WORKSPACE_ID" \
  -H "Content-Type: application/json" \
  -d '{
    "query": "supported file formats",
    "limit": 3,
    "include_context": true,
    "context_window": 2
  }' \
  | jq .
```

### Search filtered to specific documents

```bash
curl -s -X POST "$API_BASE/v1/search" \
  -H "X-API-Key: $API_KEY" \
  -H "X-Workspace-Id: $WORKSPACE_ID" \
  -H "Content-Type: application/json" \
  -d "{
    \"query\": \"embeddings\",
    \"limit\": 5,
    \"document_ids\": [\"$DOC_ID\"]
  }" \
  | jq .
```

**Expected response:**

```json
{
  "results": [
    {
      "chunk_id": "chunk-uuid-here",
      "document_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
      "document_name": "sample.txt",
      "content": "Section 2: Key Concepts\nInherent is a backend system...",
      "score": 0.87,
      "metadata": {
        "chunk_index": 1,
        "token_count": 64
      },
      "context_before": null,
      "context_after": null
    }
  ],
  "query": "what retrieval modes does Inherent support",
  "total_results": 1,
  "processing_time_ms": 42.3,
  "search_mode": "semantic",
  "total_tokens": 0
}
```

**Empty results (no matching documents):**

```json
{
  "results": [],
  "query": "xyzzy nonexistent term",
  "total_results": 0,
  "processing_time_ms": 18.1,
  "search_mode": "semantic",
  "total_tokens": 0
}
```

---

## 6. Fetch Chunks

View the individual text segments Inherent split your document into. Use the context endpoint to get all chunks reassembled into the full document text in one call.

### List all chunks for a document

```bash
curl -s "$API_BASE/v1/chunks/$DOC_ID" \
  -H "X-API-Key: $API_KEY" \
  -H "X-Workspace-Id: $WORKSPACE_ID" \
  | jq .
```

**Expected response:**

```json
[
  {
    "id": "1",
    "document_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
    "content": "Inherent Knowledge Base — Sample Text Document\n\nThis is a plain text file...",
    "chunk_index": 0,
    "token_count": 141,
    "metadata": null
  }
]
```

### Get document context (all chunks as full text)

Returns metadata + all chunks combined into `full_text`. Use this for MCP-style full-document context retrieval.

```bash
curl -s "$API_BASE/v1/chunks/$DOC_ID/context" \
  -H "X-API-Key: $API_KEY" \
  -H "X-Workspace-Id: $WORKSPACE_ID" \
  | jq .
```

**Expected response:**

```json
{
  "document": {
    "id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
    "name": "sample.txt",
    "workspace_id": "ws_local_001",
    "source_type": "upload",
    "mime_type": "text/plain",
    "size_bytes": 842,
    "chunk_count": 4,
    "status": "processed",
    "created_at": "2024-01-15T10:00:00+00:00",
    "updated_at": "2024-01-15T10:00:05+00:00",
    "metadata": null
  },
  "chunks": [
    {
      "id": "1",
      "document_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
      "content": "Inherent Knowledge Base — Sample Text Document...",
      "chunk_index": 0,
      "token_count": 141,
      "metadata": null
    }
  ],
  "full_text": "Inherent Knowledge Base — Sample Text Document\n\n...\n\nSection 4: Retrieval Modes\n..."
}
```

---

## 7. Trigger Ingestion Manually

> **Ingestion / write plane.** Sections 7–12 hit the **ingestion service** (`$INGEST_BASE`,
> port 18002), which owns writes to PostgreSQL + Weaviate. They authenticate with `$INGEST_KEY`
> (the `INGESTION_API_KEY`, default `dev-ingestion-key`) and take **no** `X-Workspace-Id` header.
> The public API (port 18000, sections 1–6) is read + upload only.

Use when you need to re-ingest a file already in S3 without going through the upload endpoint.

Ingestion runs as a Temporal workflow, so this endpoint is **asynchronous by default**: it returns
**202 Accepted** immediately with a `workflow_id`. Poll progress with section 8.

```bash
curl -s -X POST "$INGEST_BASE/ingest" \
  -H "X-API-Key: $INGEST_KEY" \
  -H "Content-Type: application/json" \
  -d "{
    \"document_id\": \"$DOC_ID\",
    \"workspace_id\": \"$WORKSPACE_ID\",
    \"user_id\": \"user_local_001\",
    \"filename\": \"sample.txt\",
    \"original_filename\": \"sample.txt\",
    \"content_type\": \"text/plain\",
    \"size_bytes\": 842,
    \"storage_backend\": \"s3\",
    \"storage_path\": \"$WORKSPACE_ID/$DOC_ID/sample.txt\"
  }" \
  | jq .
```

**Expected response (202):**

```json
{
  "workflow_id": "ingest-3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "document_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "status": "started"
}
```

### Block until done (`?wait=true`)

Append `?wait=true` to block until the workflow finishes and get the full result as **200 OK**:

```bash
curl -s -X POST "$INGEST_BASE/ingest?wait=true" \
  -H "X-API-Key: $INGEST_KEY" \
  -H "Content-Type: application/json" \
  -d "{
    \"document_id\": \"$DOC_ID\",
    \"workspace_id\": \"$WORKSPACE_ID\",
    \"user_id\": \"user_local_001\",
    \"filename\": \"sample.txt\",
    \"original_filename\": \"sample.txt\",
    \"content_type\": \"text/plain\",
    \"size_bytes\": 842,
    \"storage_backend\": \"s3\",
    \"storage_path\": \"$WORKSPACE_ID/$DOC_ID/sample.txt\"
  }" \
  | jq .
```

**Expected response (200):**

```json
{
  "workflow_id": "ingest-3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "document_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "success": true,
  "chunks_created": 4,
  "processing_time_ms": 1820,
  "error": null
}
```

Returns **409** if a workflow is already running for this `document_id`, **503** if Temporal is unavailable.

---

## 8. Get Ingestion Status

Query the real-time progress of a running (or recently completed) ingestion workflow.

```bash
curl -s "$INGEST_BASE/ingest/$DOC_ID/status" \
  -H "X-API-Key: $INGEST_KEY" \
  | jq .
```

**Expected response:**

```json
{
  "workflow_id": "ingest-3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "document_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "step": "embedding",
  "progress": 60,
  "chunks_created": 4
}
```

Returns **404** if no workflow exists for the document (or it is no longer queryable).

---

## 9. Edit a Chunk

Replace the content of a single chunk. Runs a Temporal workflow that updates PostgreSQL and
re-embeds the chunk in Weaviate. The path index is the chunk's `chunk_index` (0-based).

```bash
curl -s -X PATCH "$INGEST_BASE/chunks/$DOC_ID/0" \
  -H "X-API-Key: $INGEST_KEY" \
  -H "Content-Type: application/json" \
  -d '{ "content": "Updated chunk text goes here." }' \
  | jq .
```

**Expected response:**

```json
{
  "document_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "chunk_index": 0,
  "updated": true
}
```

Returns **409** if an edit is already in progress for that chunk, **500** if the edit fails.

---

## 10. Document Lineage

Get the ordered list of ingestion pipeline events for a document — what happened, step by step,
during its ingestion.

```bash
curl -s "$INGEST_BASE/lineage/$DOC_ID" \
  -H "X-API-Key: $INGEST_KEY" \
  | jq .
```

**Expected response:**

```json
{
  "document_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "events": [
    {
      "id": 1,
      "workflow_run_id": "ingest-3fa85f64-5717-4562-b3fc-2c963f66afa6",
      "document_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
      "workspace_id": "ws_local_001",
      "event_type": "document_fetched",
      "status": "completed",
      "duration_ms": 120,
      "metadata": null,
      "created_at": "2024-01-15T10:00:01+00:00"
    },
    {
      "id": 2,
      "workflow_run_id": "ingest-3fa85f64-5717-4562-b3fc-2c963f66afa6",
      "document_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
      "workspace_id": "ws_local_001",
      "event_type": "chunks_embedded",
      "status": "completed",
      "duration_ms": 1620,
      "metadata": { "chunks_created": 4 },
      "created_at": "2024-01-15T10:00:05+00:00"
    }
  ]
}
```

---

## 11. Delete a Document

> **Destructive.** Removes the document from PostgreSQL and its chunks from Weaviate.

Both `workspace_id` and `user_id` query params are **required**. Weaviate cleanup is best-effort:
if it fails, the PostgreSQL delete still succeeds and `weaviate_cleaned` is `false`.

```bash
curl -s -X DELETE \
  "$INGEST_BASE/documents/$DOC_ID?workspace_id=$WORKSPACE_ID&user_id=user_local_001" \
  -H "X-API-Key: $INGEST_KEY" \
  | jq .
```

**Expected response:**

```json
{
  "deleted": true,
  "document_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "weaviate_cleaned": true
}
```

Returns **404** if the document is not found in PostgreSQL.

---

## 12. Dead-letter Jobs

Failed ingestion messages land in a dead-letter table for inspection and recovery. These
endpoints live on the ingestion service (write/admin plane) and use `$INGEST_KEY`.

### List dead-letter jobs

Optional filters: `workspace_id`, `status` (default `pending`), `limit` (1–200, default 50).

```bash
curl -s "$INGEST_BASE/dead-letter?status=pending&limit=50" \
  -H "X-API-Key: $INGEST_KEY" \
  | jq .
```

**Expected response:**

```json
{
  "jobs": [
    {
      "id": 1,
      "document_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
      "workspace_id": "ws_local_001",
      "user_id": "user_local_001",
      "workflow_run_id": "ingest-3fa85f64-5717-4562-b3fc-2c963f66afa6",
      "original_message": { "document_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6", "workspace_id": "ws_local_001" },
      "error_message": "Embedding service timeout",
      "error_type": "TimeoutError",
      "retry_count": 0,
      "status": "pending",
      "created_at": "2024-01-15T10:05:00+00:00",
      "updated_at": "2024-01-15T10:05:00+00:00"
    }
  ],
  "total": 1
}
```

### Get a single job

```bash
curl -s "$INGEST_BASE/dead-letter/1" \
  -H "X-API-Key: $INGEST_KEY" \
  | jq .
```

Returns **404** if job `1` does not exist.

### Retry a job

Re-publishes the job's original message. Only jobs with status `pending` or `retrying` can be retried.

```bash
curl -s -X POST "$INGEST_BASE/dead-letter/1/retry" \
  -H "X-API-Key: $INGEST_KEY" \
  | jq .
```

**Expected response:**

```json
{
  "retried": true,
  "job_id": 1,
  "new_workflow_id": "ingest-3fa85f64-5717-4562-b3fc-2c963f66afa6"
}
```

Returns **409** if the job is not in a retriable status, **404** if missing, **500** if the re-trigger fails.

### Abandon a job

Mark a job as permanently abandoned (no further retries).

```bash
curl -s -X POST "$INGEST_BASE/dead-letter/1/abandon" \
  -H "X-API-Key: $INGEST_KEY" \
  | jq .
```

**Expected response:**

```json
{
  "abandoned": true,
  "job_id": 1
}
```

---

## 13. Common Error Reference

| HTTP Status | Service | Cause | Fix |
|---|---|---|---|
| 400 | Public API | Unsupported MIME type | Use an allowed type (see Upload section) |
| 400 | Public API | Empty file | Upload a non-empty file |
| 400 | Public API | File exceeds 50 MB | Split or compress the file |
| 400 | Public API | Missing workspace ID | Add `X-Workspace-Id` header |
| 401 | Both | Missing API key | Set `X-API-Key` header |
| 403 | Both | Invalid key / lacks required permission | Use a valid key with the needed permission (ingestion: match `INGESTION_API_KEY`) |
| 404 | Public API | Document not found in workspace | Check document ID and workspace |
| 404 | Ingestion | Document not in PostgreSQL (delete), no queryable workflow (status), or dead-letter job not found | Check the document ID / job ID; for status, ensure a workflow has run |
| 409 | Ingestion | Workflow or chunk edit already running, or dead-letter job not in a retriable status | Wait for the in-flight workflow to finish, or check the job status, then retry |
| 429 | Public API | Rate limit exceeded | Wait and retry; check `Retry-After` header |
| 503 | Both | Backing service unavailable (Public API: PostgreSQL/Weaviate; Ingestion: Temporal) | Check `GET /health/ready` (public) or `GET /health` (ingestion) |

---

## Eval feedback examples

`eval_trial.py` — interactive retrieval-quality labeling. Ask questions about
your corpus, mark which results were relevant, and each answer is filed as real
eval feedback. Twenty questions gives you enough labeled cases for a first
eval run. Requires `API_BASE`, `API_KEY`, `WORKSPACE_ID` env vars.

`langgraph_feedback_agent.py` — the same feedback loop from a real agent:
LangGraph tool wrappers that search Inherent and auto-report which chunks
answered the question. Requires `langgraph`/`langchain-core` in your own
environment (not a repo dependency).

---

## Postman Collection

Same endpoints as a Postman collection, for GUI-based testing.

| File | Purpose |
|---|---|
| `inherent.postman_collection.json` | All 23 requests across Health, Documents, Search, Chunks, Ingestion. Every request asserts its expected status code in the Postman test runner and carries a saved example response. |
| `inherent.postman_environment.json` | "Inherent Local" environment with default local values |

The **Ingestion** folder covers the write/admin plane (sections 7–12 above): trigger ingestion
(async 202, or `?wait=true` → 200), get status, edit a chunk, get lineage, delete a document, and a
**Dead-letter** sub-folder (list / get / retry / abandon failed jobs). It authenticates with
`{{ingest_key}}` (set at the folder level) instead of `{{api_key}}`.

### Import

1. Postman → **Import** (top-left, next to the workspace name) → drag both files in, or **Files** → browse to them → **Import**.
2. Top-right environment dropdown (defaults to "No Environment") → select **Inherent Local**. Now `{{api_base}}`, `{{api_key}}`, etc. resolve.
3. _(Optional)_ Click the eye icon (👁) next to the dropdown to verify values (`api_key=ink_dev_local_key_001`, `workspace_id=ws_local_001`, …).

To change a value later, open **Environments → Inherent Local** and edit the **Current Value**
column (not Initial Value), then save. Heads-up: import fills **Initial Value**, while requests and
the Upload test script read/write **Current Value** — and Current Value stays local to your machine,
so it is not included when you export the environment. (The collection's own description repeats this
under "Editing the environment".)

### Environment variables

| Variable | Default | Notes |
|---|---|---|
| `api_base` | `http://localhost:18000` | Public API base URL |
| `ingest_base` | `http://localhost:18002` | Ingestion API base URL |
| `api_key` | `ink_dev_local_key_001` | Created by `make dev-seed`; must start with `ink_` |
| `workspace_id` | `ws_local_001` | Seeded workspace |
| `ingest_key` | `dev-ingestion-key` | `INGESTION_API_KEY` from docker-compose; used by the Ingestion folder |
| `user_id` | `user_local_001` | Required by Trigger Ingestion and Delete Document |
| `document_id` | _(empty)_ | Auto-filled after running **Upload Document** |

`X-API-Key` is set at the collection level (`{{api_key}}`) and overridden to `{{ingest_key}}` on the
Ingestion folder, so individual requests don't repeat the auth header. Public requests still carry an
explicit `X-Workspace-Id` header.

### Usage

1. `docker compose up --build`, then `make dev-seed`.
2. Run **Documents → Upload Document** first. Pick a file in the request's **Body → form-data → file** field (e.g. `docs/examples/sample-documents/sample.txt`) — file paths can't be committed in the collection.
3. The Upload request's test script saves the returned `document_id` into the environment, so **Get Document**, **Chunks**, and **Search filtered to documents** work without manual edits.
4. Wait until the document shows `status: processed` (List/Get) before running **Search**.

---

## Bruno Collection

Prefer [Bruno](https://www.usebruno.com/)? The same 23 requests are also available as a Bruno
collection in [`bruno/`](bruno/). Bruno stores each request as a plain-text `.bru` file in the repo
(no cloud account), so request changes show up in normal `git diff`.

| Path | Purpose |
|---|---|
| `bruno/` | Collection root (`bruno.json` + per-folder `.bru` requests) |
| `bruno/environments/Inherent Local.bru` | "Inherent Local" environment with default local values |

### Open

1. Bruno → **Open Collection** → select `docs/examples/bruno/`.
2. Top-right environment dropdown → **Inherent Local**.
3. Run **Documents → Upload Document** first (it points at `../sample-documents/sample.txt`; change
   the file in the request's **Body** tab to test other types). Its post-response script saves
   `document_id` to the environment, so the dependent requests chain automatically.

Auth mirrors the Postman collection: `X-API-Key = {{api_key}}` at the collection level, overridden
to `{{ingest_key}}` on the **Ingestion** folder; public requests add `X-Workspace-Id`. Every request
asserts its status code in a `tests` block.

### Run from the CLI

```bash
npm install -g @usebruno/cli
cd docs/examples/bruno
bru run --env "Inherent Local"
```

Note: the **Upload Document** request needs a real file on disk; if you run headless before any
upload, set `document_id` first (`bru run ... --env-var document_id=<id>`) or skip the requests that
depend on it.

---

## Sample Files

| File | MIME Type | Located at |
|---|---|---|
| `sample.txt` | `text/plain` | `docs/examples/sample-documents/sample.txt` |
| `sample.md` | `text/markdown` | `docs/examples/sample-documents/sample.md` |
| `sample.json` | `application/json` | `docs/examples/sample-documents/sample.json` |
| `sample.csv` | `text/csv` | `docs/examples/sample-documents/sample.csv` |
| `sample.html` | `text/html` | `docs/examples/sample-documents/sample.html` |
| `sample.pdf` | `application/pdf` | Generate with any PDF tool; not committed (binary) |
| `sample.docx` | `application/vnd.openxmlformats-officedocument.wordprocessingml.document` | Generate with LibreOffice/Word; not committed (binary) |

### Generate sample PDF (requires `enscript` + `ps2pdf`)

```bash
enscript -p /tmp/sample.ps docs/examples/sample-documents/sample.txt
ps2pdf /tmp/sample.ps docs/examples/sample-documents/sample.pdf
```

### Generate sample DOCX (requires LibreOffice)

```bash
libreoffice --headless --convert-to docx \
  docs/examples/sample-documents/sample.txt \
  --outdir docs/examples/sample-documents/
```
