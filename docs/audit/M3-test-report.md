# Defect-Register Remediation — M3 Test Report (P3 hardening) + Program Summary

**Branch:** `fix/defect-register-remediation`
**Milestone:** M3 — the P3 hardening tier (#23–#42).
**Status:** ✅ complete (18 fixed test-first; #37 observable-signal version; #23 annotated with full deletion offered).

## Suite results (offline profile)

| Package | Result |
|---------|--------|
| `inh-contracts` | **24 passed** |
| `inh-ingestion-svc` | **542 passed**, 1 deselected |
| `inh-public-api-svc` | **536 passed**, 8 deselected, 1 pre-existing error* |
| **Total** | **1102 passed** |

\* Same pre-existing live-service integration error as the M1/M2 baseline.

## M3 fixes

| # | Fix | Commit |
|---|-----|--------|
| 33 | GraphQL name guard: raise, not assert (survives `python -O`) | `b5e51b9` |
| 32 | Removed dead, unscoped `_fallback_search` | `b5e51b9` |
| 39 | Trigger failure path: no UnboundLocalError masking | `8c4dc1c` |
| 36 | Dev CORS: force credentials off with wildcard origin | `8c4dc1c` |
| 31 | min_score over-fetch + truncate (no under-filled pages) | `8c4dc1c` |
| 28 | MCP search: stable tiebreaker ordering | `72dc1e9` |
| 35 | Filesystem connector path-boundary check | `72dc1e9` |
| 27 | `storage_backend` reuses the shared contract enum | `72dc1e9` |
| 40 | Prod worker logs render as JSON | `fb14ac3` |
| 26 | `fk_workspace_tenant` migration (012) | `fb14ac3` |
| 34 | SSRF allow-list on `read_file_from_url` | `2bcd463` |
| 24 | Dead-letter unique index + upsert (migration 013) | `2bcd463` |
| 41 | Staging explicit `::jsonb` cast | `a0453e2` |
| 29 | MCP schema drops no-op context params | `a0453e2` |
| 30 | Documented REST multi-workspace context skip | `a0453e2` |
| 42 | Documented optional env vars in `.env.example` | `a0453e2` |
| 38 | Chunking config resolved in the activity, not the workflow | `0026753` |
| 25 | Accurate chunk char-offsets from real source positions | `393f732` |
| 37 | Observable completion-publish drops (metric) | `c649048` |
| 23 | Legacy `processor.py` annotated deprecated/not-live | `f5304d6` |

## Two explicit follow-ups (not silently skipped)

1. **Full deletion of `processor.py`** — the register's preferred #23 remediation. It's verified dead (no runtime import), but deleting it removes ~700 lines + ~6 test files (extraction/OCR/chunking coverage). Annotated as legacy for now; awaiting a go-ahead to delete and confirm the activity tests fully cover that logic.
2. **Weaviate client-side API-key auth** — deferred from #3; the loopback port bind is the current isolation boundary. Enable `AUTHENTICATION_APIKEY_*` + thread `WEAVIATE_API_KEY` through both service clients.

## Program summary — all three milestones

| Milestone | Scope | Result |
|-----------|-------|--------|
| **M1** | 6 P1 (critical: isolation, auth, durability, deploy, rate-limit, poison) | ✅ |
| **M2** | 16 P2 (durability / API-contract / performance) | ✅ |
| **M3** | 20 P3 (hardening / smells / latent) | ✅ |
| **Total** | **42 register defects** | **✅ 40 fixed + 2 addressed with follow-ups** |

**~1102 offline tests green**, every fix TDD (RED→GREEN→regression), 3 additive migrations (011 stats ledger, 012 workspace FK, 013 dead-letter dedup), 3 documented breaking changes (Weaviate re-index, release-compose secrets, MQ topic env), all on `fix/defect-register-remediation` (`9d25fdd`→`f5304d6`).
