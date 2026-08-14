# Combined Coverage Gate — Design

Date: 2026-08-13
Status: Approved (maintainer sign-off in session)
Owner: sagarmoy

## Problem

Two separate problems, one of which is live today.

**1. The coverage gate is ~40 points looser than reality.** `ci.yml` enforces
`--cov-fail-under` of 40 (ingestion), 45 (public-api), 95 (contracts). Measured
actual on 2026-08-13: **83.1%**, **85.6%**, **99.5%**. The floors were written as
a deliberate ratchet and never raised. A PR could delete roughly half the test
suite and still pass the required merge gate. This is being fixed immediately in
a separate PR (`fix/ratchet-coverage-floors`) and is not part of this design
beyond setting its starting point.

**2. Unit coverage systematically undercounts what the suite proves.** The
worst-covered modules are precisely those that only real execution exercises:

| Module | Unit coverage | Exercised by |
|---|---|---|
| `inh-ingestion-svc/src/main.py` | **0%** (102 statements) | every E2E run — it is the service entrypoint |
| `audit_mongo_writer.py` | 0% (49) | E2E audit path |
| `shared_services.py` | 33.3% (66 missed of 99) | E2E bootstrap |
| `public-api/database.py` | 50.5% (184 missed of 372) | E2E, every request |
| `mongo_client.py` | 36.7% | E2E auth/workspace resolution |
| `embedder.py` | 51.4% | E2E search |

Driving these to 90% with unit tests would mean mocking the exact boundaries the
live E2E suite already proves — writing tests that assert against mocks to
satisfy a number, which is the failure mode the existing test suite was
explicitly built to avoid.

## Goal

A single per-service coverage number that reflects **everything the test suite
actually exercises** — unit, contract, and live E2E — gated on every PR, and
ratcheted upward to 90%.

## Decisions (maintainer-approved 2026-08-13)

1. **Metric: combined coverage.** Instrument the services inside their
   containers during E2E runs, extract the data, merge it with the unit and
   contract runs, gate the merged number.
2. **Path: ratchet, never decrease.** Floors start at the measured actual and
   only ever rise. A PR that lowers coverage fails.
3. **Approvals** (already applied to the GitHub ruleset, outside this design):
   1 required review, stale reviews dismissed on push, last-pusher cannot be the
   approver.

## Scope

**In scope:** coverage data from the runner-based lanes — `e2e-smoke.yml`
(PR-blocking) and `integration.yml` (post-merge). Both run compose on the
GitHub Actions runner, so a bind mount can retrieve the data.

**Out of scope:** the Hetzner VM lane. Its services run on a remote host behind
a firewall; retrieving coverage would require shipping data back over SSH and
reconciling paths from a different filesystem, for a lane that is post-merge and
on-demand only. Its value is deployment validation, not coverage.

**Out of scope:** changing what any test asserts. This design changes
measurement, not tests.

## Architecture

### Instrumenting the containers

Both service images gain an opt-in coverage mode, off by default and inert in
production images:

- `coverage` is added to the image (dev dependency group already present).
- A `sitecustomize.py` on the Python path calls `coverage.process_startup()`,
  which activates only when `COVERAGE_PROCESS_START` is set. This handles
  subprocesses and workers, which a wrapped `CMD` does not.
- `docker-compose.yml` gains a coverage overlay file,
  `docker-compose.coverage.yml`, setting `COVERAGE_PROCESS_START=/app/.coveragerc`
  and bind-mounting `./.coverage-data:/coverage` for both services.

The overlay is applied only by CI (`docker compose -f docker-compose.yml -f
docker-compose.coverage.yml`), so local `make dev` is unaffected.

### Flushing on shutdown

`coverage.process_startup()` registers an `atexit` hook, so data is written when
the process exits cleanly. `docker compose stop` sends `SIGTERM`; uvicorn
performs a graceful shutdown and exits normally, so the hook fires.

**This is the single most likely thing to go wrong.** The implementation must
verify empirically that `.coverage.*` files appear after a stop — not assume it.
If a service does not flush (for example a worker that traps `SIGTERM`), the
fallback is an explicit `docker compose kill -s SIGTERM` followed by a wait, or
raising `stop_grace_period`.

### Combining

The container writes coverage with paths rooted at `/app/src`. The runner needs
them mapped back to `services/<svc>/src`. This is what `[paths]` in
`.coveragerc` does, and getting it wrong is the classic failure — it silently
produces a report where nothing matches and coverage looks near-zero:

```ini
[paths]
source =
    src/
    /app/src/
```

CI then runs, per service: `coverage combine` over the unit run's data file and
the extracted container files, followed by `coverage report --fail-under=<floor>`.

### Where the gate runs

| Lane | Data combined | Gate |
|---|---|---|
| `Required tests before merge` (PR) | unit + contract | existing per-service floor (ratcheted) |
| `E2E smoke` (PR) | + smoke E2E container data | **combined floor — the new gate** |
| `integration.yml` (post-merge) | + full compose E2E | reported, ratchet PR opened when it rises |

The PR-blocking combined number comes from unit + contract + smoke E2E. The
post-merge lane produces the fuller number; when it exceeds the current floor,
the existing baseline-ratchet pattern (already used for retrieval metrics in
`integration.yml`) opens a PR raising the floor.

## Expected effect

`main.py` alone is 102 statements at 0% — roughly 2.6% of ingestion's total,
recovered purely by measuring what E2E already runs. Adding the client modules
(`mongo_client`, `embedder`, `weaviate`, `redis_mq`) plausibly closes most of
the 318-statement gap to 90% without writing a single new test.

This is an estimate, not a promise. The implementation measures the real
combined number before any floor is set, and the ratchet starts there.

## Failure modes

| Failure | Handling |
|---|---|
| Container never flushes coverage | Verified empirically in the implementation; fallback is explicit SIGTERM + grace period |
| `[paths]` remapping wrong | Symptom is a near-zero combined report. Guard: the implementation asserts combined ≥ unit-only, since combining can only add |
| Coverage data missing entirely | `coverage combine` on no files yields an empty report and a misleading 0% — CI must fail loudly if no container data files were found, not silently report unit-only |
| Instrumentation slows E2E | Measured; coverage adds roughly 10-30% runtime. The smoke lane has enormous headroom (23s of tests inside a 40-minute timeout) |
| Coverage mode leaks into production images | The overlay is CI-only and `sitecustomize` is inert without `COVERAGE_PROCESS_START`. A guard test asserts `docker-compose.release.yml` never sets that variable |

## Testing this feature

- **Guard tests** (repo-root, matching existing `test_*_workflow_guards.py`
  style): the release compose file never enables coverage; the combined step
  fails when no container data is found; floors are never lowered.
- **Empirical verification** before any gate is enabled: combined number
  measured and compared against unit-only for both services, proving the merge
  worked rather than silently reporting the same number.
- **The assertion that combined ≥ unit-only** is the core correctness check —
  it catches both a broken `[paths]` mapping and missing container data.

## File structure

```
docker-compose.coverage.yml                  # C: CI-only overlay enabling instrumentation
services/inh-ingestion-svc/sitecustomize.py  # C: coverage.process_startup() hook
services/inh-public-api-svc/sitecustomize.py # C: same
services/*/Dockerfile                        # M: ensure coverage installed + sitecustomize on path
.coveragerc  (or per-service [tool.coverage]) # M: [paths] remapping for combine
scripts/ci/combine-coverage.sh               # C: extract, combine, report, fail-under
.github/workflows/e2e-smoke.yml              # M: mount data dir, run combine, gate
.github/workflows/integration.yml            # M: same, report-only + ratchet
tests/test_coverage_gate_guards.py           # C: guard tests
docs/testing.md                              # M: document the combined metric
```

## Sequencing

1. **Ratchet floors to actual** — separate PR, already in flight. Closes the
   live hole today.
2. **Instrument + combine, measure only.** No gate. Prove the mechanism works
   and record the real combined number.
3. **Enable the combined gate** at the measured floor.
4. **Climb to 90%** by raising the floor as coverage improves, writing tests for
   whatever genuinely remains uncovered.

Step 2 is deliberately separate from step 3: enabling a gate on a number we have
not yet observed is how CI ends up red for reasons nobody can reproduce.
