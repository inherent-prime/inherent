# README Trim Plan

Goal: cut `README.md` from 406 lines to ~120, lead with the inherent.sh value
prop, and surface the trust signals (CI, coverage, release, license) a
maintained open-source repo is expected to show.

## 1. Diagnosis

| Problem | Evidence |
| --- | --- |
| Value prop is buried and off-message | README says "backend for RAG"; inherent.sh says "One brain, every agent. Store context once, stop paying for it everywhere." |
| Same content repeated 3-4 times | About / Why Use It / The Pitch / Key Features all list ingestion, chunking, embeddings, API. File-types link appears 3 times. |
| Trust signals missing | Only 2 badges. Coverage is enforced in CI (82-98% floors) but never shown. No release, license, Python, docs badges. |
| Stale facts | Docs link is malformed (`docs.inherent.sh/](https://...`). Says "two main services"; repo has four (`inh-cli`, `inh-contracts` missing). |
| Docs duplicated into README | Smoke test, bootstrap internals, endpoint table, per-service dev commands, Azure deploy notes all exist in `docs/`. |

## 2. Target structure (top-down, ~120 lines)

1. Hero image + link bar (Website, Docs, Sandbox, Blog)
2. Badge row: CI, Integration, Coverage, Release, License, Python, Docs
3. One-line pitch + 3-sentence "what it is"
4. Why Inherent (4 bullets, from inherent.sh)
5. Quickstart (two paths: `make quickstart` and published images)
6. First search (upload + search, 2 curl calls)
7. Architecture (ASCII diagram, kept)
8. Retrieval quality baseline (generated table, kept; it is a real differentiator)
9. Project health: CI gates, coverage floors, release cadence, docs
10. Repository layout (4 services, one line each)
11. Contributing / Security / Support / License (one line each)

## 3. What gets cut and where it lives

| Cut from README | Now lives in |
| --- | --- |
| About, Why Use It, The Pitch (merged into one "Why Inherent") | README §4 |
| Typical Flow, What's In The Repo, How It Works | Merged into Architecture |
| Ingestion `/ingest` curl example | `services/inh-ingestion-svc/Readme.md` |
| Bootstrap internals (both principals, `SEED_PRINCIPAL_B`) | `docs/getting-started/local.md` |
| Full smoke test script | `docs/getting-started/local.md` |
| Local Endpoints table | `docs/getting-started/local.md` |
| Per-service dev commands | `CONTRIBUTING.md` |
| Published-image notes (arm64, registry override, production hardening) | `docs/deploy/production.md`, `docs/deploy/azure.md` |
| Roadmap / ADR paragraph | One link under Architecture |

Before deleting, verify each target doc already contains the content; move it
if not. Nothing is lost, only relocated.

## 4. Badges to add and the work behind each

| Badge | Source | Work needed |
| --- | --- | --- |
| CI | existing `ci.yml` | none |
| Integration & Eval Gate | existing `integration.yml` | none |
| Coverage | **new** | Add a `coverage-badge` step in `ci.yml` that merges the four `.coverage` files, computes total %, and publishes via `schneegans/dynamic-badges-action` to a gist (needs `GIST_SECRET`). Alternative: Codecov upload + `codecov.io` badge. |
| Release | GitHub releases (`v0.6.0` exists) | none, shields `github/v/release` |
| License | `LICENSE` (MIT) | none |
| Python 3.11+ | static shields badge | none |
| Docs | `docs.yml` workflow | none |
| E2E smoke | existing `e2e-smoke.yml` | optional, badge row gets crowded; keep to 6 |

## 5. Fixes bundled with the trim

- Fix the broken Docs URL. Use `https://docs.inherent.sh/` (mkdocs `site_url` is the GitHub Pages fallback).
- List all four services.
- Add a "Project health" section that states the coverage floors (82 / 84 / 89 / 98) and the ratchet policy in two lines, linking `docs/testing.md`.
- Add `CHANGELOG.md` link and release cadence sentence.

## 6. Execution order

1. Open a GitHub issue "docs: trim README and add trust badges" (AGENTS.md rule).
2. PR 1 (docs-only): replace README with the draft below, relocate cut content, fix links. `Docs` CI check must stay green.
3. PR 2 (CI): add coverage badge job in `ci.yml` and gist secret. Add badge to README once the gist publishes.
4. Verify: badges render on `main`, all README links resolve (`make docs` link check), `docs/README.md` reading order still points at README.

## 7. Draft README

```markdown
<p align="center">
  <a href="https://inherent.sh/">
    <img src="docs/imgs/Hero.png" alt="Inherent — One brain, every agent" width="100%" />
  </a>
</p>

<p align="center">
  <a href="https://inherent.sh/">Website</a> ·
  <a href="https://docs.inherent.sh/">Docs</a> ·
  <a href="https://app.inherent.sh/">Sandbox</a> ·
  <a href="https://inherent.sh/blog">Blog</a> ·
  <a href="https://inherent.sh/#pricing">Pricing</a>
</p>

<p align="center">
  <a href="https://github.com/inherent-prime/inherent/actions/workflows/ci.yml"><img src="https://github.com/inherent-prime/inherent/actions/workflows/ci.yml/badge.svg?branch=main" alt="CI" /></a>
  <a href="https://github.com/inherent-prime/inherent/actions/workflows/integration.yml"><img src="https://github.com/inherent-prime/inherent/actions/workflows/integration.yml/badge.svg?branch=main" alt="Integration & Eval Gate" /></a>
  <a href="https://github.com/inherent-prime/inherent/actions/workflows/ci.yml"><img src="https://img.shields.io/endpoint?url=https://gist.githubusercontent.com/<OWNER>/<GIST_ID>/raw/inherent-coverage.json" alt="Coverage" /></a>
  <a href="https://github.com/inherent-prime/inherent/releases"><img src="https://img.shields.io/github/v/release/inherent-prime/inherent" alt="Release" /></a>
  <a href="LICENSE"><img src="https://img.shields.io/github/license/inherent-prime/inherent" alt="License: MIT" /></a>
  <img src="https://img.shields.io/badge/python-3.11%2B-blue" alt="Python 3.11+" />
  <a href="https://docs.inherent.sh/"><img src="https://github.com/inherent-prime/inherent/actions/workflows/docs.yml/badge.svg?branch=main" alt="Docs" /></a>
</p>

# Inherent

**One brain, every agent.** Store company context once and let every AI agent
query it with citations, instead of paying to re-load the same context into
every prompt.

Inherent is the open-source context layer behind [inherent.sh](https://inherent.sh/).
You connect sources (text, Markdown, PDF, DOCX, source code, and
[more](docs/reference/file-types.md)). It extracts, chunks, embeds, and indexes
them, then serves cited retrieval over REST and MCP. Self-host it in your own
VPC or run it locally with one command.

## Why Inherent

- **Connected once, queried everywhere.** Ingest a source one time; every agent reads from the same governed index.
- **Every answer cites its source.** Retrieval returns passages with document, chunk, and position, not a black-box summary.
- **Measured, not claimed.** Retrieval quality is a CI gate with a ratcheting baseline (table below).
- **Yours to run.** FastAPI, PostgreSQL, Weaviate, Temporal, Valkey, S3. MIT licensed, deployable in your VPC.

## Quickstart

Prerequisites: Docker, Python 3.11+, `uv`.

```bash
git clone https://github.com/inherent-prime/inherent.git && cd inherent
make quickstart   # .env, install, Compose stack, dev workspace + API key, health check
```

No checkout? Run from published images:

```bash
curl -O https://raw.githubusercontent.com/inherent-prime/inherent/main/docker-compose.release.yml
INHERENT_VERSION=latest docker compose -f docker-compose.release.yml up -d
```

Then follow [Getting Started Locally](docs/getting-started/local.md) for the
bootstrap step, endpoints, smoke test, and troubleshooting. Going live? Read
[Taking Inherent to Production](docs/deploy/production.md) or
[Deploy to Azure](docs/deploy/azure.md).

## First search

```bash
export API_BASE=http://localhost:18000 API_KEY=ink_dev_local_key_001 WORKSPACE_ID=ws_local_001

# Upload
curl -s -X POST "$API_BASE/v1/documents" -H "X-API-Key: $API_KEY" -H "X-Workspace-Id: $WORKSPACE_ID" \
  -F "file=@docs/examples/sample-documents/sample.txt;type=text/plain" | jq .document_id

# Search (once status is "processed")
curl -s -X POST "$API_BASE/v1/search" -H "X-API-Key: $API_KEY" -H "X-Workspace-Id: $WORKSPACE_ID" \
  -H "Content-Type: application/json" -d '{"query":"what retrieval modes does Inherent support","limit":3}' | jq .
```

Full endpoint reference: [REST API](docs/reference/rest-api.md) · [MCP tools](docs/reference/mcp-tools.md) · [Examples](docs/examples/README.md).

## Architecture

```text
                 documents
                     |
                     v
          +------------------------+
          |  inh-ingestion-svc     |
          |  extract / chunk /     |
          |  embed / index         |
          +-----------+------------+
                      |
        +-------------+-------------+
        v                           v
  +-------------+             +-------------+
  | PostgreSQL  |             |  Weaviate   |
  | metadata    |             | vectors     |
  +------+------+             +------+------+
         \                           /
          v                         v
             +-------------------+
             | inh-public-api-svc|
             | REST + MCP        |
             +---------+---------+
                       |
                       v
                    agents
```

Ingestion consumes upload events from Valkey, runs Temporal workflows, and
writes to PostgreSQL and Weaviate. The public API reads both and serves search
over REST and MCP. Product boundary and non-goals:
[ADR 0001](docs/adr/0001-agent-memory-substrate.md).

## Retrieval quality baseline

Committed floor in [`retrieval_baseline.json`](services/inh-public-api-svc/tests/evals/corpus/retrieval_baseline.json),
measured on the golden corpus over the full Compose stack. A regression over
`0.02` fails the build; a green run on `main` ratchets the floor up.

<!-- retrieval-baseline:start -->
<!-- Generated from services/inh-public-api-svc/tests/evals/corpus/retrieval_baseline.json by tests/evals/render_baseline_table.py — do not edit by hand. The eval-baseline-ratchet job regenerates it whenever the baseline moves. -->

| Mode | Recall@5 | MRR | nDCG@5 |
| --- | --- | --- | --- |
| Hybrid | 0.910 | 0.885 | 0.844 |
| Keyword | 0.860 | 0.841 | 0.796 |
| Semantic | 0.880 | 0.798 | 0.768 |
<!-- retrieval-baseline:end -->

Details and local runs: [docs/testing.md](docs/testing.md#retrieval-eval-gate-baseline-ratchet-and-trend-history-139).

## Project health

- **Every PR runs** lint, format, mypy, bandit, unit tests, Compose E2E, and the retrieval eval gate.
- **Coverage floors are enforced and never lowered**: ingestion 82%, public API 84%, CLI 89%, contracts 98%, plus per-module floors on auth, search, and verify.
- **Releases** follow [Keep a Changelog](CHANGELOG.md) and semver; images publish to `ghcr.io/inherent-prime/*` on every tag.
- **Docs** build in strict mode on every PR and publish to [docs.inherent.sh](https://docs.inherent.sh/).

## Repository layout

| Path | What it is |
| --- | --- |
| [`services/inh-ingestion-svc`](services/inh-ingestion-svc/Readme.md) | Temporal worker: extract, chunk, embed, index |
| [`services/inh-public-api-svc`](services/inh-public-api-svc/Readme.md) | REST API and MCP server over indexed content |
| [`services/inh-cli`](services/inh-cli/Readme.md) | `inherent` CLI for local stack and admin tasks |
| [`services/inh-contracts`](services/inh-contracts) | Shared constants and contracts between services |
| [`docs/`](docs/README.md) | Agent-first docs: getting started, reference, deploy, ADRs |
| [`infra/`](infra) | Terraform for Hetzner and Azure |

## Contributing and support

- Development setup, checks, and PR expectations: [CONTRIBUTING.md](CONTRIBUTING.md)
- Bugs and feature requests: [GitHub Issues](https://github.com/inherent-prime/inherent/issues) · Support routes: [SUPPORT.md](SUPPORT.md)
- Security reports (private): [SECURITY.md](SECURITY.md)
- Code of conduct: [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)

## License

[MIT](LICENSE)
```
