# Release Acceptance Matrix

The suites that must pass before tagging a release, why each exists, and the
README/product claims they cover. Run the offline suites with a single
`make release-check`; the Compose e2e gate runs in CI via `integration.yml`.

See also: [docs/testing.md](../testing.md) (profiles + markers) and
[releasing.md](releasing.md) (the release checklist).

## Acceptance suites

### Offline suites (run via `make release-check`)

| # | Service        | Suite                       | Command                                                        | Why it gates a release |
| - | -------------- | --------------------------- | ------------------------------------------------------------- | ---------------------- |
| 1 | both           | Lint / format / type / sec  | `make check` *(validate + lint + format-check + type-check + security-check + test)* | Toolchain + offline test baseline must be green. |
| 2 | inh-public-api | Unit + integration          | `uv run pytest -m 'not compose'`                              | Core REST/MCP behavior, default offline run. |
| 3 | inh-public-api | Contract                    | `uv run pytest -m contract`                                   | REST/MCP response shapes, permissions, error codes don't regress. |
| 4 | inh-public-api | Security                    | `uv run pytest -m security`                                   | Auth/tenancy isolation regressions (offline). |
| 5 | inh-ingestion  | Unit + integration          | `uv run pytest -m 'not compose'`                              | Core ingestion/extraction/chunking, default offline run. |
| 6 | inh-ingestion  | Eval                        | `uv run pytest -m eval`                                       | Extraction/chunking quality stays within fixtures. |
| 7 | inh-ingestion  | Failure injection           | `uv run pytest -m failure_injection`                         | Graceful degradation under dependency failure. |
| 8 | inh-contracts  | Contract/schema             | `uv run pytest`                                               | Shared event + naming contracts remain stable. |

> `make check` (#1) already runs each service's default `pytest`
> (`-m 'not compose'`, with coverage), so #2 and #5 are covered by it; they are
> listed explicitly because they are also the things release notes speak to.

### Coverage floors

Coverage is enforced in CI (`.github/workflows/ci.yml`), not by
`make release-check`. The overall per-service floors a release must not regress
below:

| Service        | Overall floor (`--cov-fail-under`) |
| -------------- | ---------------------------------- |
| inh-ingestion  | 40                                 |
| inh-public-api | 45                                 |

Per-core-module risk-based floors are also enforced in the CI "Enforce
per-core-module coverage floors" step. Both sets are intentionally conservative
and should ratchet **up** over time — never down to make a release pass.

### Compose e2e (the gate — runs in CI, not in `make release-check`)

The full ingestion-to-search end-to-end path runs against a real Compose stack
(Postgres / Weaviate / Redis / S3) in `.github/workflows/integration.yml`. It
is **not** part of `make release-check` because it needs the stack up; treat a
green `integration.yml` run as the final release gate.

| Service        | Suite                                          | Command (CI)             |
| -------------- | ---------------------------------------------- | ------------------------ |
| inh-public-api | Compose: integration + retrieval evals + bench | `uv run pytest -m compose` |
| inh-ingestion  | Compose: benchmarks                            | `uv run pytest -m compose` |

Locally you can reproduce it with `make dev` (stack up) followed by
`make test-integration`.

## README claim → covering tests

| README / product claim                                              | Covering suite(s) |
| ------------------------------------------------------------------- | ----------------- |
| Multi-format ingestion (text, MD, CSV, HTML, JSON, PDF, DOCX, PNG)  | ingestion unit + `eval` (#5, #6) |
| Chunking + embedding generation for semantic retrieval             | ingestion unit + `eval` (#5, #6); compose retrieval evals |
| Background processing survives dependency hiccups                   | ingestion `failure_injection` (#7) |
| REST API: search, document listing, chunk access, context retrieval | public-api unit + `contract` (#2, #3) |
| MCP-friendly retrieval patterns                                     | public-api `contract` (#3) |
| Per-tenant isolation / auth on every call                          | public-api `security` (#4) |
| Stable shared event + naming contracts between services            | contracts suite (#8) |
| Vector-backed similarity search returns relevant passages          | compose e2e (ingestion-to-search) |
| End-to-end: upload → process → searchable                          | compose e2e (`integration.yml`) |
| Traffic-mined retrieval evals (feedback capture, scorecard, mode-comparison runs) | public-api `contract` (#3); compose e2e flywheel (`integration.yml`) |

## Pre-tag flow

1. `make release-check` — all offline acceptance suites green.
2. Confirm the latest `integration.yml` run (Compose e2e gate) is green.
3. Confirm coverage floors held in the latest CI run.
4. Follow the [release checklist](releasing.md).
