# Testing

This repository is a monorepo of three Python packages, each with its own test
suite and `pytest` configuration, plus a small root-level suite for artifacts
that don't belong to any one package:

- `services/inh-public-api-svc` — customer-facing API + MCP server
- `services/inh-ingestion-svc` — document ingestion service
- `services/inh-contracts` — shared event + naming contracts
- `tests/` (repo root) — pins repo-level artifacts (e.g. `docker-compose.yml`'s
  Postgres init behavior, the initial migration in
  `services/inh-ingestion-svc/scripts/migrations/`). No project dependencies
  (stdlib only), so it isn't part of any service's `uv sync` — run it via
  `uvx 'pytest==9.0.2' tests/` or `make test` (#183).

Within `inh-public-api-svc`, don't confuse `tests/app_flows/` with
`tests/integration/`: `tests/app_flows/` (renamed from `tests/e2e/` — the old
name overclaimed) drives the FastAPI app in-process with `get_database` /
`get_search_service` / auth mocked via `AsyncMock`/`MagicMock`, never
touching a real dependency. Live end-to-end coverage — a real client against
a real booted stack — lives in `tests/integration/test_compose_*.py` and is
gated by the `compose` marker (see [Markers](#markers) below); a `smoke`
subset of those runs on every PR.

Every test command below assumes you have synced dev dependencies first:

```bash
make install      # syncs dev deps for all three Python packages (public-api, ingestion, inh-contracts)
# or, per service:
cd services/<svc> && uv sync --extra dev --group dev
```

Tool versions (pytest, pytest-asyncio, pytest-cov, ruff, black, mypy, bandit)
are normalized across all three services — see
[docs/developer/dependencies.md](developer/dependencies.md).

## Postgres and what a local run actually covers

`ci.yml` runs `service-checks` with a live Postgres service container. Locally,
without Postgres up, both services' suites **skip** (not fail) every test
that needs a database connection, with reason `PostgreSQL not available` — a
local `make test`/`make check` green does **not** mean the full offline suite
ran. Measured on a laptop with no Postgres: `inh-ingestion-svc` alone shows
**382 passed, 365 skipped** (357 of those skips are `PostgreSQL not
available`). To run the full suite locally (matching CI), start Postgres
first, e.g. `docker compose up -d postgres` or `make dev`, then re-run
`uv run pytest` (or `make test`) against that database.

## Expected runtime

`make test` takes **~6m10s** wall time (measured with no Postgres up):
`inh-ingestion-svc` 24.8s (382 passed, 365 skipped), `inh-public-api-svc`
5m37s (864 passed), `inh-contracts` 0.4s (154 passed), root `tests/` <0.1s (2
passed) — public-api dominates the wall time. If your shell or agent harness
defaults to a ~2-minute command timeout, raise it or run `make test` in the
background before invoking it; it will not finish inside a default timeout.

## Default behavior

Both services default to **excluding Compose-backed tests** via `addopts`
(`-m 'not compose'`), so a bare `uv run pytest` is safe to run on a laptop with
no Docker stack up. Coverage (`--cov`) is on by default in both services.

## Test profiles

Run these from the relevant service directory (`cd services/<svc>`).

### Fast unit (innermost loop)

Skips Compose, slow, and benchmark tests. In `inh-public-api-svc` this marker
filter excludes almost nothing — measured, `make test-fast` runs the
**identical 864 tests** as the default profile there and finishes in
**~5m44s**, only ~26s faster than `make test`'s 6m10s. Despite the name, this
is not a quick local loop; treat the timeout guidance in
[Expected runtime](#expected-runtime) the same way you would for `make test`.
Coverage collection (on by default) is the actual cost, not the marker
filter: `uv run pytest --no-cov` measured at **2m56s** for public-api versus
**5m37s** with coverage on — pass `--no-cov` yourself for a fast inner loop;
no Make target does this for you today.

```bash
uv run pytest -m 'not compose and not slow and not benchmark'
```

Repo-wide shortcut:

```bash
make test-fast         # runs the fast profile across both services, inh-contracts, and root tests/
                        # -- NOT meaningfully faster than `make test`; see above
```

### Default (offline)

The full offline suite for a service (Compose tests already excluded by
`addopts`):

```bash
uv run pytest                 # uses each service's default -m 'not compose'
# explicit equivalent:
uv run pytest -m 'not compose'
```

Repo-wide shortcut:

```bash
make test             # pytest for both services, inh-contracts, and root tests/
```

### End-to-end / Compose

Requires a running local stack (`make dev` or `docker compose up`). These hit
real Postgres / Weaviate / Redis / S3 and are the release e2e gate:

```bash
uv run pytest -m compose
```

Repo-wide shortcut:

```bash
make test-integration   # public-api compose suite (stack must be up)
```

**Local compose CI:** `.github/workflows/integration.yml` (or `make test-integration`
against a laptop stack). Runs on push to `main`, nightly cron, and manual
dispatch — not on pull requests, since it runs the full Compose suite plus
the retrieval-eval hard gate and both services' benchmarks. See the
[retrieval-eval gate](#retrieval-eval-gate-baseline-ratchet-and-trend-history-139)
section below.

**PR-blocking smoke lane:** `.github/workflows/e2e-smoke.yml` — the `E2E
smoke` required check, and the only live end-to-end signal a PR gets before
merge. It boots the identical Compose stack `integration.yml` does, then runs
only tests tagged with the `smoke` marker via `-m "smoke and compose"`
(`--no-cov`; coverage is the fast lane's job). Today that is 6 tests, all in
`inh-public-api-svc` (ingestion has none yet — the step tolerates pytest's
"no tests collected" exit code so an empty selection doesn't fail the gate),
covering the ingest → search roundtrip, PDF extraction, cross-workspace
isolation, the MCP tool-surface pin plus an MCP search roundtrip, and
`event_id` usability, and they take ~25s against a booted stack; the job's
own timeout is 40 minutes to absorb stack boot/bootstrap time. MCP's
upload → poll → delete round trip is not smoke-tagged — it stays
`compose`-only, post-merge. The retrieval-eval hard gate is deliberately
excluded from `smoke` — its baseline only ratchets on `main` runs, so gating
it per-PR would block merges on drift unrelated to the PR — and the bulk of
the slower live E2E suites (tenancy, lifecycle, event-durability) remains
`compose`-only, one canary test from each promoted to `smoke` and the rest
running in the post-merge lane; dead-letter recovery is the one suite that
is entirely post-merge, none of it in `smoke`.

**Laptop Hetzner VM (manual):** [getting-started/local-vm-test.md](getting-started/local-vm-test.md)
— Terraform apply from your machine with Object Storage remote state, smoke
`/health`, optional bootstrap and `pytest -m compose`. Destroy when done.

**Hetzner production-path e2e: REMOVED.** `.github/workflows/hetzner-e2e.yml`
is gone; end-to-end coverage now lives entirely in GitHub Actions via
`integration.yml` (above), which runs the full Compose stack on the runner.

Why it was removed rather than fixed: the lane had not produced a genuine pass
since 2026-07-13, and its skip path reported **success** with every meaningful
step skipped — so a green check meant either "fully verified" or "did nothing",
indistinguishable without opening the run. v0.6.0 shipped believing it had e2e
coverage it never had. A signal that can be silently absent is worse than no
signal, because releasing.md pointed maintainers at it.

Two real gaps it surfaced are worth remembering if the lane ever returns:

- The **stdio** MCP test builds the server in-process on the runner and needs
  direct datastore access (`localhost:15432` / `:27018` / `:18080`).
  `docker-compose.release.yml` publishes no datastore ports and binds them to
  `127.0.0.1` on the VM, so that test cannot work against release compose
  without undoing that hardening. It is covered by `integration.yml`, which
  runs dev compose on the runner itself.
- `scripts/dev/bootstrap.sh` seeds the second tenancy principal only when
  `SEED_PRINCIPAL_B=1` or `API_KEY` is the local dev default. Any lane using a
  per-run key must set that flag or the cross-workspace isolation tests 401
  instead of verifying anything.

What GitHub Actions e2e does **not** cover, and is now untested in CI: the
published GHCR images, `docker-compose.release.yml` itself (localhost-bound
datastores, Weaviate API-key auth, required-secret guards), and any real-VM
behaviour. Verify those manually before a release —
[getting-started/local-vm-test.md](getting-started/local-vm-test.md) still
provides a laptop Terraform path, and the published-image smoke check in
[releasing](maintainers/releasing.md) still applies.

- **Recover orphans:** `.github/workflows/hetzner-e2e-recover.yml` is retained
  as a manual-dispatch cleanup tool for any leftover CI Terraform state.
- **Local `act`:** see infra README § Local simulation and
  [audit/act-hetzner-e2e-weaviate-401.md](audit/act-hetzner-e2e-weaviate-401.md).

See [infra/README.md](https://github.com/inherent-prime/inherent/blob/main/infra/README.md#ci-e2e) and
[releasing](maintainers/releasing.md#cutting-an-image-release).

## Markers

Markers are declared in each service's `[tool.pytest.ini_options].markers`.
Combine them with `-m` expressions (e.g. `-m 'security or contract'`).

| Marker             | Meaning                                                            | Services |
| ------------------ | ----------------------------------------------------------------- | -------- |
| `unit`             | Fast, isolated unit tests                                          | all      |
| `integration`      | Exercises real service dependencies                               | public-api, ingestion |
| `compose`          | Requires a running docker-compose stack (deselected by default)   | public-api, ingestion |
| `smoke`            | PR-blocking subset of `compose`; run via `-m "smoke and compose"` in `e2e-smoke.yml` | public-api (ingestion: none yet) |
| `slow`             | Slow-running tests                                                 | public-api, ingestion |
| `benchmark`        | Latency/throughput benchmarks (loose SLO regression guards)       | public-api, ingestion |
| `security`         | Auth/tenancy security regression tests (offline)                  | public-api, ingestion |
| `contract`         | REST/MCP/schema contract regression (shapes, permissions, codes)  | all      |
| `retrieval_eval`   | Retrieval quality and ranking regression benchmarks               | public-api |
| `eval`             | Fixture-backed extraction/chunking quality evaluations            | ingestion |
| `failure_injection`| Intentional dependency-failure tests                              | ingestion |

### Specialized examples

```bash
# Security regressions (offline)
cd services/inh-public-api-svc && uv run pytest -m security

# REST/MCP contract regressions
cd services/inh-public-api-svc && uv run pytest -m contract

# Retrieval quality benchmarks
cd services/inh-public-api-svc && uv run pytest -m retrieval_eval

# Ingestion extraction/chunking evaluations
cd services/inh-ingestion-svc && uv run pytest -m eval

# Ingestion dependency-failure injection
cd services/inh-ingestion-svc && uv run pytest -m failure_injection

# Benchmarks (either service)
cd services/<svc> && uv run pytest -m benchmark
```

## Retrieval-eval gate, baseline ratchet, and trend history (#139)

`test_compose_retrieval_regression.py` (`retrieval_eval` + `compose`) hard-gates
on regression, not just reporting: any per-mode metric (recall@5/MRR/nDCG@5)
that drops more than its **effective tolerance** below the committed
`corpus/retrieval_baseline.json` fails the build, via
`tests/evals/eval_gate.py`. An absolute-floor backstop
(`RETRIEVAL_MIN_RECALL5`, default `0.15`) still applies underneath it.

### Tolerance is derived from corpus resolution (#236)

`EVAL_GATE_TOLERANCE` (default `0.02`) is a **floor**, not the tolerance
itself. The actual per-metric tolerance the gate uses is:

```
effective_tolerance(metric, n) = max(EVAL_GATE_TOLERANCE, min_detectable_delta(metric, n))
```

where `n` is the number of gated golden queries (every query in
`corpus/qrels.jsonl` except `category == "abstention"`, matching the same
exclusion the pooled recall/MRR/nDCG averages already apply — abstention
queries have no relevant document by construction, so they score a
structural `0.0` regardless of ranking quality) and
`min_detectable_delta(metric, n)` is the smallest possible move a *single*
query's rank change can produce in that metric's pooled average:

| Metric family | Smallest single-query step | Formula |
|---|---|---|
| `mrr` | rank 1 → rank 2 | `0.5 / n` |
| `recall@k` | gaining/losing one relevant doc | `1 / n` |
| `ndcg@k` | top-2 positions swap | `(1 - 1/log2(3)) / n` |

**Why:** a fixed absolute tolerance can be finer than what a small corpus can
actually resolve. With `n = 13` gated queries (the corpus size as of
2026-08-12), the smallest possible MRR move is `0.5 / 13 ≈ 0.0385`, which
already exceeds the `0.02` floor — so *any* single query slipping one rank
position hard-failed the gate even when every other metric improved (#236,
first hit in #237, which blocked `main` for three days on a net-positive
change). `effective_tolerance` makes the gate's resolution match the
corpus's: below `min_detectable_delta`, the gate cannot distinguish "one
document moved one rank" from "retrieval regressed" — those two produce
overlapping numbers — so it must not fail on the difference. Raising
`EVAL_GATE_TOLERANCE` still works as a floor for a larger/noisier corpus; it
just can no longer be set *below* what the corpus can resolve.

**The honest trade-off:** widening the tolerance to match resolution also
widens what counts as "not a regression." At `n = 13`, `recall@5`'s derived
tolerance is `1 / 13 ≈ 0.0769` — a real recall regression up to ~7.7
percentage points on a single query can now pass the gate silently, over 3.5x
the old fixed `0.02` (2 percentage points). This is the same
one-rank-slip-shaped noise the fix is closing for `mrr`/`ndcg@5`, applied to
`recall@5`'s coarser (binary hit/miss, not rank-weighted) step size, so it
isn't a new risk the fix introduces so much as the existing risk's exact size
made visible. `min_detectable_delta` shrinks as `1/n`, so growing the golden
corpus's gated-query count is the direct lever to tighten it back down — e.g.
doubling `n` to 26 halves every metric's derived tolerance. This is a
standing incentive to keep expanding `corpus/qrels.jsonl`, not a one-time
trade to forget about.

`min_detectable_delta` and `effective_tolerance` live in `tests/evals/eval_gate.py`
and are unit-tested (hand-computed values) in `tests/evals/test_eval_gate.py`.
The compose test derives `n` from the same in-memory query/category mapping it
already uses to compute the pooled averages, so the tolerance is always
derived from the exact pool a given run measured over — not a hardcoded
constant that could drift from the corpus.

The `check` CLI subcommand also supports deriving `n`, for anyone invoking the
gate outside of pytest: pass `--num-queries N` directly, or `--qrels
path/to/qrels.jsonl` to have it counted (same abstention exclusion). Passing
neither preserves the pre-#236 behavior — `--tolerance` is used as a flat
value for every metric. **Precedence:** `.github/workflows/integration.yml`
does not invoke the `check` CLI at all today — the gate runs entirely inside
`test_compose_retrieval_regression.py`'s pytest assertion, which always
derives per-metric tolerance from the live corpus, using `EVAL_GATE_TOLERANCE`
only as the floor. If a caller ever does invoke `check` with an explicit
`--tolerance` alongside `--num-queries`/`--qrels`, derivation wins:
`--tolerance` is the floor under the derived value, never a way to force a
flatter (and possibly under-resolved) tolerance back on.

On a green gate on `main`, `.github/workflows/integration.yml`'s
`eval-baseline-ratchet` job ratchets the baseline up to
`max(current, baseline)` per mode/metric (never down), appends a line to
`corpus/retrieval_history.jsonl` — a durable, checked-in trend log of every
main-branch run's scores — regenerates the baseline table published in
`README.md` (see [below](#publishing-the-baseline-to-readmemd)), and opens (or
updates) a pull request carrying all three
changes, rather than pushing to `main` directly: branch protection rejects
direct `github-actions[bot]` pushes, so a push-based ratchet silently fails
every run (this is what left the baseline seeded at zeros for the entire time
#139 was live — see the history log's first entry for the real numbers it was
seeded with instead). The job reuses one branch
(`chore/ratchet-retrieval-baseline`) across runs; it checks for an **open** PR
specifically (`gh pr list --state open`, not `gh pr view`, which also matches
an already-merged PR on the same branch and would otherwise skip
`gh pr create` forever after the first merge) and, if one is open, pulls that
PR's own baseline/history forward before recomputing rather than resetting to
`main`'s older copy, so a not-yet-merged rise is never silently dropped. The
PR is opened with auto-merge requested so a clean ratchet still needs no human
action, but falls back to a normal maintainer-merged PR if auto-merge isn't
enabled on the repo — the same fallback applies if the optional
`RATCHET_PR_TOKEN` repo secret (a PAT or GitHub App token with
`contents:write`+`pull-requests:write`) isn't configured, since the default
`GITHUB_TOKEN` is excluded from triggering other workflow runs on the push/PR
it creates and the PR's required check (`ci.yml`) won't fire on its own
without it. On gate failure (push-to-main or nightly), the
`eval-regression-alert` job files or updates an issue labeled
`retrieval-eval-regression`. This does **not** gate PRs — the full Compose
stack stays too slow/expensive to run on every PR (see the note at the top of
`integration.yml`); regressions are caught post-merge, same as the rest of
this workflow.

The golden corpus (`corpus/qrels.jsonl`) tags each judgment with an optional
`category`: `general`, `exact_id`, `stale_version`, `paraphrase`, `abstention`
(a query with no relevant document — the correct signal is zero recall/MRR/
nDCG, not a fabricated match), or `multi_doc_crowding` (a query with 2+
genuinely relevant documents where one has many more chunks than the other —
`q14`, `rate-limiting-deep-dive.txt` (5 chunks) vs.
`rate-limit-quick-reference.txt` (1 chunk) — exercising the scenario
per-document diversification, #146, exists to fix: a naive score-sorted
top-k can crowd the shorter document out entirely). Per-category scores are
printed and written to the eval report (`_by_category`) for visibility; only
the per-mode pooled averages are gated, and `abstention` queries are excluded
from that pool since they can never contribute a positive score by
construction. Permission/tenancy boundaries are deliberately not a category
here — that's owned by the `security` marker suite, not this ranking-quality
corpus.

### Publishing the baseline to README.md

`tests/evals/render_baseline_table.py` renders `corpus/retrieval_baseline.json`
into the marker-delimited block in `README.md`:

```bash
# from services/inh-public-api-svc
uv run python -m tests.evals.render_baseline_table \
  --baseline tests/evals/corpus/retrieval_baseline.json \
  --readme ../../README.md
```

Invoke it as `python -m` from the service directory, not as a bare script path
from the repo root: it imports `load_metrics` from `eval_gate` rather than
keeping a second copy of the doc-key-dropping parse, and only `-m` puts the
`tests` package on `sys.path`.

It rewrites only the text between `<!-- retrieval-baseline:start -->` and
`<!-- retrieval-baseline:end -->`, and fails (exit 1) if that marker pair is
missing rather than silently leaving the README stale. Output is a pure
function of the baseline, so re-running against an unchanged baseline is a
no-op — the `eval-baseline-ratchet` job relies on that to keep `README.md` out
of its commit unless the numbers actually moved.

It renders the **baseline**, not `retrieval_history.jsonl`, deliberately: a
history line is appended on every main-branch run (each with a fresh
timestamp, so never a no-op), so rendering history would rewrite `README.md`
on every run. The baseline moves only on a real improvement. The baseline is
also a per-metric `max()` across runs, so the block reports it as a floor and
carries no single commit SHA — stamping one would misattribute values that
came from different commits.

Because the ratchet job now commits `README.md`, `README.md` is in the
workflow's `paths-ignore`; without that, merging a ratchet PR would re-trigger
`integration.yml` and recreate the unbounded ratchet loop that the
`retrieval_baseline.json`/`retrieval_history.jsonl` exclusions already prevent.

## Benchmark JSON report artifacts (REQ-EVL-3)

The live Compose benchmarks (`benchmark` + `compose`, both services) each
write a JSON summary alongside printing to stdout, so a run's numbers survive
past the CI log — same principle as the retrieval-eval report above, not just
for retrieval:

- **public-api search benchmarks** (`test_search_latency_throughput.py`) write
  `search-benchmark-report.json` with `search_latency` (p50/p95/p99/min/max
  ms) and `search_throughput` (QPS) keys, each carrying the commit SHA the run
  measured. `tests/benchmark/run_search_benchmark.py::write_benchmark_report`
  merges rather than overwrites, since both tests share one file within a run;
  the standalone CLI (`run_search_benchmark.py`) writes the same shape under a
  `cli_search` key via its own `--report` flag.
- **ingestion throughput benchmark** (`test_ingestion_throughput.py`) writes
  `ingestion-benchmark-report.json` with an `ingestion_throughput` key
  (`docs_per_sec`, `elapsed_s`, `batch_size`, commit SHA), via the sibling
  `tests/benchmark/benchmark_report.py` helper (duplicated rather than shared
  across services — separate Python packages, no common dependency between
  them).

Both are uploaded as CI artifacts (`search-benchmark-report`,
`ingestion-benchmark-report`) by `integration.yml`'s `compose-integration` job,
`if: always()` so a benchmark failure still leaves the partial numbers
retrievable. Override the output path locally with the `BENCHMARK_REPORT` env
var. These are visibility only — no CI gate reads them back; the loose
SLO assertions already in the tests are what fails the build on a gross
regression.

## Coverage

Coverage is enabled by default (`--cov=src --cov-report=term-missing`). To run
without it (faster, or to avoid coverage gates while iterating):

```bash
uv run pytest --no-cov
```

## Release gate

The suites that must pass before tagging — and how to run them in one shot via
`make release-check` — are documented in
[docs/maintainers/release_acceptance_matrix.md](maintainers/release_acceptance_matrix.md).
