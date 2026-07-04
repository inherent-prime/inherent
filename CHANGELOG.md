# Changelog

All notable changes to Inherent are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); this project has not
yet cut a tagged release, so everything sits under **Unreleased**.

## [Unreleased] — Org-readiness program

A milestone-by-milestone push to make Inherent a self-hostable, permission-aware
agent **memory substrate** an organization can run on day one. Delivered as a
stack of reviewable PRs (merge order: #65 → #66 → #67 → #68 → #69 → #70, on top
of the already-merged M0–M2 #62/#63/#64). See
[docs/maintainers/org-readiness-requirements.md](docs/maintainers/org-readiness-requirements.md)
and [ADR 0001](docs/adr/0001-agent-memory-substrate.md).

### Defect-register remediation (in progress)

A codescan-driven pass fixing correctness, isolation, and durability defects.

- **⚠️ BREAKING (data) — collision-free Weaviate naming.** Workspace/user ids
  are now base32-encoded into collection/tenant names instead of stripping
  punctuation, which previously let ids differing only in punctuation
  (`ws-123` / `ws_123` / `ws123`) collapse onto one tenant — a cross-tenant
  leak (#1). Derivation is now injective. **Existing Weaviate collections use
  the old names and must be re-indexed** (drop + re-ingest) to migrate; Postgres
  is unaffected.
- **Auth** — a workspace-scoped API key can no longer be used against a
  different workspace via the `X-Workspace-Id` header, even one its owner also
  owns; the key's binding is authoritative.
- **Durable ingestion** — the `store_in_postgresql`, `store_in_weaviate`, and
  `ensure_tenant_ready` Temporal activities now re-raise on failure so the
  configured `RetryPolicy` actually fires (they previously swallowed errors
  into a success return → no retry, instant dead-letter, and NULL-tenant docs).
- **Poison messages** — a malformed upload event is now dead-lettered and
  ACKed instead of re-raising into an infinite MQ redelivery loop; the worker
  and api trigger are wired with `db_service` so dead-lettering is not a no-op.
- **Rate limiting** — unauthenticated / invalid-key requests (and all traffic
  during a transient auth-DB outage) are now bounded per client IP instead of
  bypassing the limiter; a Redis backend (selected when `REDIS_URL` is set)
  keeps limits correct across autoscaled instances, and the in-memory fallback
  now warns that limits are per-process. New `RATE_LIMIT_UNAUTHENTICATED`.
- **⚠️ BREAKING (deploy) — release Compose hardening.** `docker-compose.release.yml`
  now **refuses to start** unless `POSTGRES_PASSWORD` and `INGESTION_API_KEY`
  are set (no more shipped `postgres` / `dev-ingestion-key` defaults), and all
  backing datastores (Postgres, Mongo, Weaviate, Valkey, S3) publish their
  ports on `127.0.0.1` only. Set both variables (see `.env.example`) before
  `docker compose up`. **Weaviate now runs with API-key auth** (anonymous access
  off): set `WEAVIATE_API_KEY` too — both services authenticate to Weaviate with
  it (Bearer token), and the ports stay loopback-bound as defense-in-depth.

### M0–M2 (merged: #62, #63, #64)
- **Boundary** — agent-memory-substrate ADR + org-readiness plan (#46).
- **Foundation** — one-command `make quickstart`; OSS bootstrap creating the
  workspace in both Postgres + MongoDB; idempotent/non-destructive migrations;
  repo-level `make check`; Compose ingestion→search integration test
  (#3, #16, #5, #19, #15).
- **Durable ingestion** — document lifecycle status (no more 404-while-pending),
  durable upload→ingestion handoff, idempotent reindex + duplicate-chunk fix,
  failure-injection coverage (#7, #6, #60, #11, #31).

### M3 — Content fidelity (#65)
- README/upload/extraction format alignment + pdf/docx fixtures (#9).
- Configurable, model-aware chunking with a documented token budget (#10).
- Extraction & chunking quality evals (#34); coverage reporting added to CI.

### Deferred follow-ups (#66)
- Non-blocking ingestion with backpressure + dead-letter recording (#8, #18).
- PNG upload via OCR (Tesseract, optional extra, graceful fallback) (#61).
- Shared `inh-contracts` package for Weaviate naming + event schemas; both
  Docker build contexts moved to the repo root (#12, #17).

### M4 — Measurable retrieval (#67)
- Measurable hybrid baseline with documented scoring + score provenance (#45).
- Concurrent, bounded, ranking-safe multi-workspace search (#13).
- Golden corpus + ranking regression evals (recall@k/MRR/nDCG) (#33, #35).
- Latency/throughput benchmarks with loose SLO guards (#36).

### M5 — Trust (#68)
- Chunk-level authorization + provenance; **fixed a context-window
  cross-tenant leak** (neighbour chunks now scoped to the requesting user) (#41).
- Auth/tenancy/permission regression suite (#32).
- Freshness-aware memory (`is_stale`) + source refresh endpoint (#42).
- Claim-level citations + lexical `verify_claim` + `/v1/verify-claim` (#39).
- RAG poisoning / prompt-injection risk signals (non-blocking) (#44).
- Adaptive retrieval quality gate + bounded fallback (#43).
- Fix: reconcile Weaviate chunk-property schema on existing collections so
  search doesn't break on upgraded deployments (caught by the live E2E).

### M6 — Agent surface (#69)
- Renamed `src/mcp` → `src/mcp_server` (stopped shadowing the `mcp` SDK).
- MCP permission + feature parity with REST; shared `build_search_request` (#14).
- Memory primitive tools over MCP + REST: `search_memory`, `get_citations`,
  `verify_claim`, `explain_lineage`, `refresh_stale_source`; lineage endpoint (#40).
- REST/MCP contract regression suite (#30).
- Fix: `explain_lineage` reads provenance from the chunk columns (live-caught).

### M7 — Governance + DX (#70)
- Eval result reporting + baseline (#37); per-core-module coverage floors (#38);
  release acceptance matrix + `make release-check` (#29).
- Eval-gated advanced-index **scaffolding** (graph/hierarchy/rerank), off by
  default, no implementation (#47).
- Root pre-commit (#20); normalized dev-tool pins across services (#21);
  documented pytest markers + test profiles (#22); CI caching + step summaries
  (#27); developer-experience / local-setup issue templates (#28).

### Notes
- Every milestone was validated by a live `docker compose` end-to-end run
  (upload → ingest → embed → index → search, plus dedup/status/ranking/benchmark)
  in addition to offline suites.
- MVP-by-intent: heuristic poisoning risk (#44), lexical `verify_claim` (#39),
  and advanced indexes (#47, scaffolding only) are deliberate starting points to
  tighten behind the eval gates.

### Post-merge fixes
- search no longer 500s when a workspace isn't indexed yet — a query that races
  ahead of Weaviate class/tenant creation (or a brand-new/empty workspace) now
  returns empty results instead of an error; the retrieval regression guard was
  calibrated to the real fresh-stack baseline. Fixed the Integration (compose)
  CI workflow on `main`.
- document upload dedup no longer floods search with re-uploaded duplicates
  (#75). Dedup previously keyed only on `(workspace, filename)`, so re-uploading
  identical content under a different name created a new `document_id` with
  duplicate chunks/embeddings that monopolized top-k and pushed out distinct
  documents. Upload now computes `sha256(file_bytes)` and reuses the existing
  `document_id` on a content match (any filename) before falling back to
  filename dedup — verbatim copies collapse onto one document. Adds migration
  `010_document_content_hash.sql` (nullable `content_hash` column + lookup
  index) plus unit coverage and a compose E2E content-flood regression test.
