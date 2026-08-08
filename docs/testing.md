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
against a laptop stack).

**Laptop Hetzner VM (manual):** [getting-started/local-vm-test.md](getting-started/local-vm-test.md)
— Terraform apply from your machine with Object Storage remote state, smoke
`/health`, optional bootstrap and `pytest -m compose`. Destroy when done.

**Hetzner production-path e2e:** `.github/workflows/hetzner-e2e.yml` — Terraform
apply on Hetzner (remote state key `inherent/ci/<run_id>/terraform.tfstate`),
bootstrap, then public-api `pytest -m compose` against the VM. Not a PR gate.

- **Triggers:** successful **Publish images** on a final `vX.Y.Z` tag
  (`workflow_run`; RCs skipped), or manual **Run workflow** form.
- **Form / inputs:** [infra/README.md § Manual run](https://github.com/inherent-prime/inherent/blob/main/infra/README.md#manual-run-github-form)
  — `ref` (required; checkout + compose; needs `infra/`), optional
  `inherent_version` (GHCR tag), `server_type` (default `cpx32`). “Use workflow
  from” only selects the workflow YAML branch.
- **Pin:** prefer aligned image tag + checkout when testing a release; use
  `ref=main` + explicit `inherent_version` when the release tag lacks `infra/`.
- **Secrets:** `HCLOUD_TOKEN`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`.
- **Variables:** `HETZNER_S3_BUCKET`, `HETZNER_S3_ENDPOINT`, optional
  `AWS_DEFAULT_REGION` (default `eu-central`).
- **Recover orphans:** `.github/workflows/hetzner-e2e-recover.yml` (`run_id`
  input) — same infra README section.
- **Local `act`:** optional laptop simulation of the workflow; see infra README
  § Local simulation and [audit/act-hetzner-e2e-weaviate-401.md](audit/act-hetzner-e2e-weaviate-401.md).
  Smoke image parity before long runs ([releasing](maintainers/releasing.md#hetzner-act-e2e-image-parity)).

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
that drops more than `EVAL_GATE_TOLERANCE` (default `0.02`) below the committed
`corpus/retrieval_baseline.json` fails the build, via
`tests/evals/eval_gate.py`. An absolute-floor backstop
(`RETRIEVAL_MIN_RECALL5`, default `0.15`) still applies underneath it.

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
