# Defect-Register Remediation — M2 Test Report (P2 fixes)

**Branch:** `fix/defect-register-remediation`
**Milestone:** M2 — all sixteen P2 (durability / API-contract / performance) defects.
**Status:** ✅ complete. Every fix landed test-first (RED proven before GREEN).

## Suite results (offline profile: `-m 'not compose and not slow and not benchmark'`)

| Package | Result |
|---------|--------|
| `inh-contracts` | **24 passed** |
| `inh-ingestion-svc` | **514 passed**, 1 deselected |
| `inh-public-api-svc` | **526 passed**, 8 deselected, 1 pre-existing error* |
| **Total** | **1064 passed** |

\* Same pre-existing live-service integration error as the M1 baseline; unrelated to these fixes.

## Fixes, each with its guarding test(s)

| # | Fix | Commit | Guard |
|---|-----|--------|-------|
| 14 | `/health/ready` returns 503 when unhealthy | `1efb3c8` | readiness status-code tests |
| 13 | MCP `list_documents` clamps page/page_size | `1efb3c8` | MCP page-bounds test |
| 12 | Raw `HTTPException` → RFC7807 problem+json | `9199dd9` | HTTPException problem-detail tests |
| 9 | Chunk edit recomputes `content_hash` + tokens | `9199dd9` | chunk-edit provenance test |
| 11 | Local storage path-boundary (`is_relative_to`) | `ce29807` | sibling/absolute/legit traversal tests |
| 15 | Shared `MQ_UPLOAD_TOPIC` env alias | `ce29807` | topic-alias contract test |
| 20 | Drop unbounded Prometheus labels | `cefea8c` | metric-cardinality guard |
| 22 | Stat/HEAD-based fetch size (no double download) | `cefea8c` | fetch no-download test |
| 8 | Weaviate batch inspects `failed_objects` | `c9de967` | batch-error + success tests |
| 21 | Context-window per-match ranges (merge only overlaps) | `c9de967` | range-computation tests |
| 16 | Trust-scoped audit IP + sanitized request id | `9eaad02` | trust/sanitize tests |
| 18 | Observable audit-drop metric | `9eaad02` | drop-metric test |
| 19 | Offload blocking embed off the event loop | `7f79599` | ingestion offload test + search regression |
| 7 | Idempotent workspace stats (run-id ledger, migration 011) | `94e20b7` | mocked-session idempotency tests |
| 10 | Observable pre-store document status (pending row) | `08a26f8` | activity + DB-method tests |
| 17 | Runtime MQ redelivery (XAUTOCLAIM + poison cap) | `ca5b63b` | reclaim/redispatch/poison/min-idle tests |

## Schema / config changes

- **Migration 011** adds `workspace_stats_ledger` (idempotency for #7); additive + idempotent.
- New settings: `RATE_LIMIT_UNAUTHENTICATED` (M1 #5), `trusted_proxies` (#16). Both default to safe values.

## Method

Strict TDD throughout: a failing test written and observed RED, the fix applied, GREEN confirmed, then the full offline suite re-run for zero regressions. Where a fix changed a shared contract (metric labels, health signature, batch-error behavior, stats signature, naming), the affected existing tests were updated to assert the new correct behavior rather than suppressed.

## Cumulative status

- **M1 (6 P1s)** ✅ + **M2 (16 P2s)** ✅ = **22 of the register's defects fixed**, 1064 tests green.
- **Next — M3:** the P3 hardening tier (#23–#42): delete legacy tests-only `processor.py`, dead-letter unique constraint, chunk-offset drift, `fk_workspace_tenant` migration, storage_backend enum drift, MCP determinism/context parity, `min_score` over-fetch, assert-based GraphQL guard, SSRF allow-list, CORS dev footgun, best-effort outbox, and the smaller smells.
- **Deferred follow-up:** wire Weaviate API-key auth through both service clients (from #3).
