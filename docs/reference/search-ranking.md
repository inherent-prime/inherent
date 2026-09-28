# Search ranking: usage-based reuse boost (#394)

Sections that keep reappearing in newer documents in a workspace rank somewhat higher for the same query. The boost is off by default and enabled per workspace.

## How it works

1. **Detect reuse at ingest** (`inh-ingestion-svc`, opt-in via `WORKSPACE_REUSE_DETECTION`)
   - Each new chunk ≥ `REUSE_MIN_CHUNK_CHARS` is compared with its `REUSE_TOP_K` nearest neighbours from **other** documents in the same workspace.
   - A candidate matches when cosine similarity ≥ `REUSE_SIMILARITY_THRESHOLD` (default `0.92`; sent to Weaviate as certainty `(1 + cos) / 2`) **and** text similarity ≥ `REUSE_TEXT_SIMILARITY_THRESHOLD` (default `0.7`; `0.0` disables the check).
   - A match increments the **older** chunk's `reuse_count` in Postgres (`document_chunks`) and Weaviate, and sets `last_reused_at`.
   - Idempotent: the dedupe key is (older chunk, reusing document, reusing chunk's content hash). Re-ingesting an unchanged document never double-counts.
   - A document never counts as reusing itself. Detection is best-effort and never fails ingestion.

2. **Boost at query time** (`inh-public-api-svc`, weight per workspace via `WORKSPACE_REUSE_BOOST`)
   - After fusion, for semantic, hybrid and keyword modes alike:
     `score × min(1.5, 1 + weight × log1p(reuse_count))`, then results are re-sorted.
   - `log1p` gives diminishing returns; the `1.5` cap stops reuse from burying a clearly more relevant result.
   - `weight = 0`, an unset workspace, or `reuse_count = 0` → the score is unchanged (identical ranking).
   - Each result exposes `reuse_count`.

## Enable for a workspace

```bash
# inh-ingestion-svc
WORKSPACE_REUSE_DETECTION=ws_abc
# inh-public-api-svc
WORKSPACE_REUSE_BOOST=ws_abc=0.1
```

Start with `0.1`. Measure with `inherent eval run` (see [Retrieval evals](retrieval-evals.md)) before and after changing the weight.

## Storage

- Migration `024_chunk_reuse.sql`: `document_chunks.reuse_count`, `document_chunks.last_reused_at`, and `chunk_reuse_events` (the dedupe ledger; rows cascade-delete with their chunk, so a workspace purge removes them).
- Weaviate: numeric `reuse_count` property, added to existing collections automatically.

See [Configuration](configuration.md) for every setting.
