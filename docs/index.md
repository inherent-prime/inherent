# Inherent

**Build your private company brain.**

Inherent is the backend for turning company knowledge into something AI
systems can actually query: a self-hostable ingestion, indexing, storage,
and retrieval layer for a private RAG system, exposed over REST and MCP.

You connect sources — plain text, Markdown, CSV, HTML, PDF, JSON, DOCX,
XLSX, PPTX, PNG (OCR), EML, EPUB, RTF, ODT, YAML/TOML/XML, source code
(Python, JS/TS, Go, Java, Rust, and more), SRT/WebVTT transcripts — see the
full [supported file types](reference/file-types.md) list — and Inherent
extracts, chunks, embeds, stores, and serves retrieval with citations,
permissions, and freshness signals.

[Get started locally](getting-started/local.md){ .md-button .md-button--primary }
[What's new](release-notes.md){ .md-button }

## Fast routes

| If you need to... | Open this |
| --- | --- |
| Start the system locally and run the first upload/search flow | [Local quickstart](getting-started/local.md) |
| Deploy to a Hetzner VM with Terraform | [Production](getting-started/production.md) |
| Harden the stack before real users/data | [Production hardening](deploy/production.md) |
| Separate content by clearance, tenant, or need-to-know | [Access-control model](access-control.md) |
| Replace a document that changed, or run a clean retrieval eval | [Keeping content current](keeping-content-current.md) |
| Call the API — every endpoint, with curl examples | [API examples](examples/README.md) · [REST API reference](reference/rest-api.md) |
| Wire an agent over MCP | [MCP tools reference](reference/mcp-tools.md) |
| Configure the services | [Configuration reference](reference/configuration.md) |
| See what changed in each release | [Release notes](release-notes.md) |
| Understand the design decisions | [ADR index](adr/README.md) |
| Contribute, report a vulnerability, get help | [Contributing](community/contributing.md) · [Security](community/security.md) · [Support](community/support.md) |

## How it works

Two services, standard components (FastAPI, PostgreSQL, Weaviate, Temporal,
Valkey, S3-compatible storage, Hugging Face TEI):

- **`inh-ingestion-svc`** owns document processing: consumes upload events,
  runs Temporal workflows, extracts, chunks, embeds, and writes to
  PostgreSQL + Weaviate.
- **`inh-public-api-svc`** serves retrieval: REST API and MCP server over
  the indexed content, with per-key permissions, workspace tenancy,
  citations, claim verification, lineage, and traffic-mined retrieval evals.

Run the whole stack with Docker Compose — from source (`make quickstart`)
or from published GHCR images (`docker-compose.release.yml`).
