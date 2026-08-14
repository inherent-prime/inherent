

[Website](https://inherent.sh/) · [Docs](https://inherent-prime.github.io/inherent/) · [Pricing](https://inherent.sh/#pricing) · [Blog](https://inherent.sh/blog) · [Try the Sandbox](https://app.inherent.sh/)

# Inherent



Build your private company brain.

Inherent is the backend for turning company knowledge into something AI systems can actually query.

You connect sources — plain text, Markdown, PDF, DOCX, source code, and more (see the full [supported file types](docs/reference/file-types.md) list). Inherent extracts the content, chunks it, generates embeddings, stores it, and exposes retrieval over REST and MCP-friendly patterns.

In practical terms, this repository is the ingestion, indexing, storage, and retrieval layer of a private RAG system.

## About

Inherent is for teams that want their agents to answer from company context instead of guessing from general model knowledge.

It gives you:

- a document ingestion pipeline
- chunking and embedding generation
- persistent storage for documents and chunks
- vector-backed search over indexed content
- an API layer for retrieving relevant results



## Why Use It

- Bring your own documents: ingest plain text, Markdown, PDF, DOCX, source code, and more — see the full [supported file types](docs/reference/file-types.md) list (PNG images are read via OCR, which requires the ingestion service's optional `ocr` extra and the `tesseract` system binary).
- Run locally: the repo ships with a Compose stack for the required databases and supporting services.
- Separate ingestion from retrieval: one service writes and indexes data, another serves search requests.
- Build on standard components: FastAPI, PostgreSQL, Weaviate, Temporal, Redis/Valkey, and S3-compatible storage.

## Key Features

- Multi-format ingestion — plain text, Markdown, PDF, DOCX, source code, and more; see the full [supported file types](docs/reference/file-types.md) list (PNG images via OCR, which requires the optional `ocr` extra plus the `tesseract` system binary)
- Chunking and embedding generation for semantic retrieval
- PostgreSQL as structured storage for documents and chunks
- Weaviate as vector index for similarity search
- REST API for search, document listing, chunk access, and context retrieval
- Traffic-mined retrieval evals — turn real search traffic and agent feedback into a labeled eval set, then score recall/MRR/nDCG across keyword, semantic, and hybrid modes on your own corpus, no golden-set authoring required
- Local-first developer setup with Docker Compose



### Retrieval Quality Baseline

Retrieval quality is a CI contract, not a claim. The numbers below are the  
committed baseline in  
`[retrieval_baseline.json](services/inh-public-api-svc/tests/evals/corpus/retrieval_baseline.json)`,


| Mode     | Recall@5 | MRR   | nDCG@5 |
| -------- | -------- | ----- | ------ |
| Hybrid   | 0.885    | 0.795 | 0.738  |
| Keyword  | 0.885    | 0.782 | 0.717  |
| Semantic | 0.885    | 0.705 | 0.706  |




Run-over-run scores are appended to
`[retrieval_history.jsonl](services/inh-public-api-svc/tests/evals/corpus/retrieval_history.jsonl)`.
See [docs/testing.md](docs/testing.md#retrieval-eval-gate-baseline-ratchet-and-trend-history-139)
for the gate, the absolute-floor backstop, and how to run the evals locally.

## What's In The Repo

- an ingestion service that processes and indexes documents
- a public API service that searches and returns document content
- a Docker Compose stack for running the databases and supporting services locally
- tests and service-level Python projects for development

## Security and Support

- Security reports: [SECURITY.md](SECURITY.md)
- Usage questions and support routes: [SUPPORT.md](SUPPORT.md)



## License

[MIT](LICENSE)