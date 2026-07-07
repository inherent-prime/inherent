# Getting Started Locally

Use this guide to start the full Inherent stack, upload a sample document, wait
for ingestion, and run your first search.

## What You Will Run

Local development uses Docker Compose for the backing services and the two
Inherent services:

- `inh-public-api-svc` on `http://localhost:18000`
- `inh-ingestion-svc` on `http://localhost:18002`
- PostgreSQL, MongoDB, Weaviate, Valkey, s3rver, Temporal, and the embedding
  sidecar

The Makefile wraps the common commands so you do not need to memorize the
underlying `docker compose` and `uv` calls.

## Prerequisites

- Docker and Docker Compose
- Python 3.11+
- `uv`
- `curl`
- `jq` for the examples that parse JSON responses

## 1. Prepare Your Environment

From the repository root:

```bash
make setup
```

This creates `.env` from `.env.example` when needed and installs the dev
dependencies for both Python services.

Validate the environment:

```bash
make validate
```

If you are using Docker Compose, warnings about hostnames such as `postgres`,
`weaviate`, `valkey`, `s3rver`, or `text-embeddings-inference` are expected.
Those names resolve inside the Compose network. Use the published `localhost`
ports only when running an individual service directly on your host.

## 2. Start the Stack

Start everything in the background and seed a local public API key:

```bash
make dev
```

The first run can take several minutes because Docker images are built and the
embedding sidecar downloads its model.

The seeded local credentials are:

```bash
export API_BASE="http://localhost:18000"
export INGEST_BASE="http://localhost:18002"
export API_KEY="ink_dev_local_key_001"
export WORKSPACE_ID="ws_local_001"
```

Check service health:

```bash
make health
```

For a deeper readiness check:

```bash
curl -s "$API_BASE/health/ready" | jq .
```

## 3. Upload a Sample Document

Upload the committed sample text file through the public API:

```bash
curl -s -X POST "$API_BASE/v1/documents" \
  -H "X-API-Key: $API_KEY" \
  -H "X-Workspace-Id: $WORKSPACE_ID" \
  -F "file=@docs/examples/sample-documents/sample.txt;type=text/plain" \
  | tee /tmp/inherent-upload.json \
  | jq .
```

Save the document ID:

```bash
export DOC_ID="$(jq -r .document_id /tmp/inherent-upload.json)"
```

The upload response should show `status: "pending"`. Ingestion runs
asynchronously after the file is stored and an upload event is published.

## 4. Wait for Ingestion

Poll the document until its status becomes `processed`:

```bash
curl -s "$API_BASE/v1/documents/$DOC_ID" \
  -H "X-API-Key: $API_KEY" \
  -H "X-Workspace-Id: $WORKSPACE_ID" \
  | jq .
```

If the document stays pending, inspect the ingestion logs:

```bash
make logs SVC=inh-ingestion-svc
```

You can also inspect the public API logs:

```bash
make logs SVC=inh-public-api-svc
```

## 5. Search Your Indexed Document

After the document is processed, run a search:

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

Search with surrounding context:

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

Fetch the full reconstructed document context:

```bash
curl -s "$API_BASE/v1/chunks/$DOC_ID/context" \
  -H "X-API-Key: $API_KEY" \
  -H "X-Workspace-Id: $WORKSPACE_ID" \
  | jq .
```

## 6. Judge Retrieval Quality (Evals)

Every search response includes an `event_id`. Report whether the results
answered the question, and Inherent turns your real queries into a labeled
eval set — no golden-set authoring needed.

Label a few queries interactively (you judge, the script files real feedback):

```bash
python3 docs/examples/eval_trial.py
```

Check the scorecard (answer rate, corpus gaps, labeled-case count):

```bash
curl -s "$API_BASE/v1/evals/scorecard" \
  -H "X-API-Key: $API_KEY" \
  -H "X-Workspace-Id: $WORKSPACE_ID" | jq .summary
```

Run a mode comparison (recall@5 / MRR / nDCG@5 for keyword vs semantic vs
hybrid, on YOUR corpus):

```bash
RUN_ID=$(curl -s -X POST "$API_BASE/v1/evals/runs" \
  -H "X-API-Key: $API_KEY" \
  -H "X-Workspace-Id: $WORKSPACE_ID" | jq -r .run_id)
curl -s "$API_BASE/v1/evals/runs/$RUN_ID" \
  -H "X-API-Key: $API_KEY" \
  -H "X-Workspace-Id: $WORKSPACE_ID" | jq .run.aggregates
```

Notes: capture is on by default and raw query events are kept for 30 days
(`EVAL_RETENTION_DAYS`); disable per workspace with
`EVAL_CAPTURE_DISABLED_WORKSPACES` or purge with `DELETE /v1/evals/events`.
Promoted eval cases persist until you disable them via
`PATCH /v1/evals/cases/{case_id}`.

## Common Commands

| Command | Purpose |
| --- | --- |
| `make help` | List available Makefile targets. |
| `make dev` | Start the stack in the background and seed the local API key. |
| `make up` | Start the stack in the foreground. |
| `make health` | Check public API and ingestion API health. |
| `make doctor` | Check every service and print triage hints for failures. |
| `make logs` | Follow all Compose logs. |
| `make logs SVC=inh-public-api-svc` | Follow one service's logs. |
| `make ps` | Show Compose service status. |
| `make down` | Stop the stack. |
| `make clean` | Stop the stack and remove local Compose volumes. |
| `make check` | Run validation, lint, formatting, typing, security checks, and tests. |

## Troubleshooting

### `make validate` prints Compose hostname warnings

This is expected when `.env` mirrors the Compose network. The warnings matter
only when you run services directly on your host instead of through Compose.

### The first startup is slow

The embedding sidecar downloads the configured model on first boot. Watch logs:

```bash
make logs SVC=text-embeddings-inference
```

### Upload succeeds, but search returns no results

Confirm the document reached `processed` status:

```bash
curl -s "$API_BASE/v1/documents/$DOC_ID" \
  -H "X-API-Key: $API_KEY" \
  -H "X-Workspace-Id: $WORKSPACE_ID" \
  | jq .
```

Then check the ingestion service logs:

```bash
make logs SVC=inh-ingestion-svc
```

### S3 — file upload or retrieval fails

Confirm the s3rver container is reachable:

```bash
curl -s http://localhost:19000
```

If the container is up but uploads fail, the bucket may not have been created
yet. The `s3rver` container creates the `inherent-documents` bucket on startup
via its `--configure-bucket` flag. Check its logs:

```bash
docker compose logs s3rver
```

If the bucket is missing, recreate the container:

```bash
docker compose up -d --force-recreate s3rver
```

### PostgreSQL — migrations not applied

The `postgres-init` container applies SQL migrations on startup. If services
report missing tables, check whether it completed successfully:

```bash
docker compose ps postgres-init
```

A clean run exits with code `0`. If it shows a non-zero exit or is still
running, check the logs:

```bash
docker compose logs postgres-init
```

Restart it after the `postgres` container is healthy:

```bash
docker compose restart postgres-init
```

To verify the schema directly:

```bash
docker compose exec postgres psql -U postgres -d knowledge_base -c '\dt'
```

### Deleting local volumes

Use `make clean` (equivalent to `docker compose down -v`) only when you want a
complete reset. All persistent data is destroyed.

| Volume | Data destroyed |
| --- | --- |
| `postgres_data` | PostgreSQL tables — workspaces, API keys, document metadata |
| `mongodb_data` | Raw document storage |
| `weaviate_data` | Vector embeddings — re-ingest all documents to rebuild |
| `valkey_data` | Redis stream offsets and cached state |
| `s3_data` | Uploaded file blobs |
| `tei_cache` | Downloaded embedding model — re-downloads on next start |

To delete a single volume without stopping the whole stack, stop the relevant
service first:

```bash
# Example: force the embedding model to re-download (fixes a corrupt cache)
docker compose stop text-embeddings-inference
docker volume ls | grep tei_cache          # find the exact volume name
docker volume rm <volume-name-from-above>
docker compose up -d text-embeddings-inference
```

After `make clean`, run `make dev` to rebuild and reseed:

```bash
make clean
make dev
```

### Temporal — workflow execution failures

Open the Temporal UI to inspect workflow history and identify failed activities:

```
http://localhost:18233
```

Or list namespaces via the Temporal UI REST API:

```bash
curl -s http://localhost:18233/api/v1/namespaces | jq .
```

### Weaviate — vector store not indexing

Check Weaviate readiness:

```bash
curl -s http://localhost:18080/v1/.well-known/ready
```

Confirm objects have been written:

```bash
curl -s "http://localhost:18080/v1/objects?limit=1" | jq .totalResults
```

### Valkey — event queue not delivering messages

Ping Valkey from inside the Compose network:

```bash
docker compose exec valkey valkey-cli PING
```

List keys to check whether upload events were published:

```bash
docker compose exec valkey valkey-cli KEYS '*'
```

### PostgreSQL — document metadata missing

Connect to the database and verify the schema exists:

```bash
docker compose exec postgres psql -U postgres -d knowledge_base -c '\dt'
```

The host port for external clients such as `psql` on the host is `localhost:15432`.

### Embedding service — embeddings not generated

Check service health:

```bash
curl -s http://localhost:18088/health
```

If the service is not ready, it may still be downloading the model on first
boot. Watch logs until `Ready` appears:

```bash
make logs SVC=text-embeddings-inference
```

## Next Steps

- Use [docs/examples/README.md](../examples/README.md) for endpoint-by-endpoint
  request examples.
- Open public API docs at `http://localhost:18000/docs`.
- Open Temporal UI at `http://localhost:18233`.
