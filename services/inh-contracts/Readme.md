# inh-contracts

Shared contracts package consumed by **both** the ingestion service
(`inh-ingestion-svc`) and the public API service (`inh-public-api-svc`).

This is the single source of truth for:

- **Weaviate naming** (`inh_contracts.naming`) — `get_workspace_collection_name`
  and `get_user_tenant_name`. Both services derive collection/tenant names from
  raw workspace/user ids and MUST agree byte-for-byte (#12).
- **Versioned event schemas** (`inh_contracts.events`) —
  `DocumentUploadMessage` and `DocumentCompletionMessage`, the canonical
  cross-service event contracts carrying `contract_version` (#17).
- **Embedding provider abstraction** (`inh_contracts.embedding`, #311) — the
  `EmbeddingProvider` interface, the `TEIProvider` (default) and
  `OpenAICompatibleProvider` wire adapters, `create_embedding_provider()`,
  the shared retry/batching helpers, and the Weaviate collection
  model-identity guard (`inh_contracts.embedding.identity`). Both services'
  `embedder.py` are thin wrappers over this — see
  `docs/reference/configuration.md`'s "Embedding provider &
  model-identity guard" section for the operator-facing picture.

`CONTRACT_VERSION` pins the semantic version of these contracts.

## Usage

Each service declares a uv path dependency in its `pyproject.toml`:

```toml
[project]
dependencies = ["inh-contracts", ...]

[tool.uv.sources]
inh-contracts = { path = "../inh-contracts" }
```

and imports from it:

```python
from inh_contracts.naming import get_workspace_collection_name, get_user_tenant_name
from inh_contracts.events import DocumentUploadMessage, DocumentCompletionMessage
```
