# Defect-Register Remediation — M1 Test Report (P1 fixes)

**Branch:** `fix/defect-register-remediation`
**Milestone:** M1 — all six P1 (critical) defects from the codescan defect register.
**Status:** ✅ complete. Every fix landed test-first, each with a failing test proven RED before the fix.

## Suite results (offline profile: `-m 'not compose and not slow and not benchmark'`)

| Package | Result |
|---------|--------|
| `inh-contracts` | **24 passed** |
| `inh-ingestion-svc` | **497 passed**, 1 deselected |
| `inh-public-api-svc` | **507 passed**, 8 deselected, 1 error* |
| **Total** | **1028 passed** |

\* The single error — `tests/integration/test_api_path.py::test_health_endpoint_exists` — is **pre-existing** (present in the baseline before any change) and requires a live service; it is unrelated to these fixes. Compose-marked integration/e2e suites are excluded from the offline profile and run in CI's `integration.yml`.

## Fixes, each with the test that guards it

| # | Fix | Severity | Commit | Guarding tests |
|---|-----|----------|--------|----------------|
| 1 | **Injective Weaviate naming** — base32 encode ids instead of stripping punctuation, closing a cross-tenant collision leak (`ws-123`/`ws_123`/`ws123` collapsed onto one tenant). | P0/P1 | `cb932f2` | contracts: injectivity + charset-validity + golden; service golden mirrors |
| 2 | **Store/tenant activities raise** — so Temporal `RetryPolicy` actually fires instead of instant dead-letter / NULL-tenant docs. | P1 | `122a4a6` | `TestStoreAndTenantRaiseOnFailure` (3) |
| 3 | **Release-compose hardening** — fail-fast secrets (no `postgres`/`dev-ingestion-key` defaults), loopback-only datastore ports. | P1 | `2544bd6` | `test_release_compose_hardening.py` (8) |
| 4 | **API-key workspace binding** — `X-Workspace-Id` can no longer escape a scoped key's workspace. | P1 | `9d25fdd` | `test_workspace_isolation.py` (+3) |
| 5 | **Rate limiter** — per-IP fallback for unauthenticated traffic + Redis backend for cross-instance correctness. | P1 | `a53d4a2` | RedisBackend + selection + IP-fallback middleware (7) |
| 6 | **Poison-message DLQ** — malformed MQ messages dead-lettered + ACKed (no redelivery loop); `db_service` wired. | P1 | `3447555` | `TestAsyncTriggerPoisonHandling` + backfill (3) |

## Breaking changes (documented in CHANGELOG under Unreleased)

- **Data:** existing Weaviate collections use the old names and must be re-indexed (drop + re-ingest) after fix #1. Postgres unaffected.
- **Deploy:** the release stack now refuses to boot without `POSTGRES_PASSWORD` and `INGESTION_API_KEY` set (fix #3).

## Verification method

Each fix followed strict TDD: a new test was written and observed FAILING against the unpatched code, the fix applied, the test observed PASSING, then the full offline suite re-run to confirm zero regressions. Name-dependent tests affected by fix #1 were made drift-proof (deriving expected names via the contract function rather than hardcoding).

## Not yet done (tracked)

- **M2:** the 16 P2 defects (durability/contract/perf) — next milestone.
- **M3:** the P3 hardening set.
- **Follow-up:** wire Weaviate API-key auth through both service clients (deferred from #3; loopback bind is the current isolation boundary).
