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
  <a href="https://github.com/inherent-prime/inherent/actions/workflows/ci.yml"><img src="https://img.shields.io/badge/coverage-floors%2082%E2%80%9398%25%20enforced-brightgreen" alt="Coverage floors enforced in CI" /></a>
  <!-- Coverage badge: swap for the dynamic gist endpoint once COVERAGE_GIST_ID is set — see docs/maintainers/coverage-badge.md -->
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
