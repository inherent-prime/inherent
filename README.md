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

**One brain, every agent.** Store your company context once. Every AI agent
(Claude, Cursor, and anything that speaks MCP or REST) queries it with
citations.

Inherent is the open-source context layer behind [inherent.sh](https://inherent.sh/).
Self-host it in your own VPC or run it locally with one command.

https://github.com/user-attachments/assets/c01c0e25-c995-4e70-b07c-ce6da2ff393d

## The problems it solves

| Problem | What Inherent does |
| --- | --- |
| Paying to re-send the same context | Ingest a source once. Agents search the index and pull back only the passages they need. |
| Every agent has its own version of the truth | One index serves every agent over [REST](docs/reference/rest-api.md) and [MCP](docs/reference/mcp-tools.md). |
| Answers you cannot verify | Search results carry citations, with document, chunk, and position. A lineage lookup shows where a document came from and whether it is stale. |
| Outdated content outranking current content | Re-upload a file under the same name and it is reindexed in place; the old text stops being retrievable. See [Keeping content current](docs/keeping-content-current.md). |
| Sensitive data and who can see it | Self-host it. Each workspace is a security boundary, and API keys can be scoped to a single workspace. See the [access-control model](docs/access-control.md). |
| Content in many formats | Text, Markdown, PDF, DOCX, source code, and [more](docs/reference/file-types.md) are extracted, chunked, embedded, and indexed for you. |
| Not knowing if search is any good | Run your own test queries with `inherent eval run` to measure hit rate, and check a per-workspace retrieval scorecard. See [retrieval evals](docs/reference/retrieval-evals.md). |

## Quickstart

Prerequisites: Docker, Python 3.11+.

```bash
pip install inherent
inherent up
inherent connect claude --print
```

`inherent up` pulls the engine images, waits for health, and stores local
credentials in `~/.inherent/`. Next, upload a document and run your first
search: [Getting Started Locally](docs/getting-started/local.md).

## Connect your agent

```bash
inherent connect claude --print    # Claude Code
inherent connect cursor --print    # Cursor
```

Hosted endpoint, claude.ai connector and other options: [MCP tools](docs/reference/mcp-tools.md) · [CLI reference](docs/reference/cli.md#connect). Anything else can call the [REST API](docs/reference/rest-api.md).

## Use cases

- **A shared company brain** for every coding agent and assistant your team runs.
- **Cited answers** from policies, specs, contracts, and transcripts, with a source for each claim.
- **Agents that read and write context.** Over MCP they can search, read, upload, and edit documents and chunks.
- **A private RAG backend** you can run in your own VPC, behind your own keys.
- **Custom agents** that query the same index. See the [examples](docs/examples/README.md).

## Learn more

- [Documentation](https://docs.inherent.sh/) · [REST API](docs/reference/rest-api.md) · [MCP tools](docs/reference/mcp-tools.md) · [CLI](docs/reference/cli.md)
- Going live: [Production hardening](docs/deploy/production.md) · [Deploy to Azure](docs/deploy/azure.md)
- Help: [Support](SUPPORT.md) · [GitHub Issues](https://github.com/inherent-prime/inherent/issues)
- Contributing: [CONTRIBUTING.md](CONTRIBUTING.md) · [Code of Conduct](CODE_OF_CONDUCT.md)
- Security reports (private): [SECURITY.md](SECURITY.md)

## License

[MIT](LICENSE)
