# MCP tools reference

The public API service ships an MCP server (`inherent-knowledge-base`)
exposing the same capabilities as the REST API with matching permission
enforcement and failure behavior.

## Running & transport

- **Transport: stdio.** Start the service with `SERVICE_MODE=mcp`; the MCP
  server runs as its own process (it is not mounted on the REST app).
- Every tool call carries the API key as a schema argument (`api_key`) —
  there are no transport headers on stdio (no `X-Workspace-Id`; use the
  `workspace_id` schema argument instead). The key is validated and the
  tool's permission checked **before** the handler runs, mirroring REST
  401/403 behavior. Handlers additionally enforce workspace scoping via
  the same rule REST uses (`get_authorized_workspace_ids` in
  `src/services/auth.py`, #138): a workspace-scoped key is bound to
  exactly its one workspace — a `workspace_id` naming any other workspace
  is rejected, even one the key's owner also owns. A user-scoped key
  (`workspace_id` unset on the key) may use any workspace its owner owns.
- Tools, schemas, permissions, and dispatch all derive from a single
  `_TOOLS` registry entry per tool, so the advertised surface cannot drift
  from the enforced one.

## Tools

All tools require `api_key` (string). Additional parameters below.

### Search (`search` permission)

| Tool | Parameters | Purpose | REST twin |
| --- | --- | --- | --- |
| `search_documents` | `query` (required); `workspace_id`, `limit` (10), `min_score` (0.0), `document_ids[]`, `search_mode` (`semantic`/`hybrid`/`keyword`), `alpha` (0.7) | Search chunks; with no `workspace_id`, fans out across every workspace the key is authorized for (its one bound workspace if scoped, otherwise every workspace its owner owns) | `POST /v1/search` |
| `search_memory` | same as `search_documents` | Memory-primitive alias — identical behavior | `POST /v1/search` |
| `get_citations` | same as `search_documents` | Search returning claim-level citation objects (spans, score, provenance, freshness) | `POST /v1/search` |
| `report_feedback` | `event_id`, `verdict` (`answered`/`partial`/`not_relevant`) required; `useful_chunk_ids[]`, `note` | Record a verdict on a captured search event; builds the workspace eval set | `POST /v1/evals/feedback` |
| `get_retrieval_health` | `workspace_id` (required) | Workspace retrieval scorecard | `GET /v1/evals/scorecard` |

### Read (`read` permission)

| Tool | Parameters | Purpose | REST twin |
| --- | --- | --- | --- |
| `list_documents` | `workspace_id`, `page` (1), `page_size` (20) | Paginated document listing | `GET /v1/documents` |
| `get_document` | `document_id` (required) | Single document's metadata | `GET /v1/documents/{id}` |
| `list_chunks` | `document_id` (required) | All chunks for a document | `GET /v1/chunks/{document_id}` |
| `get_document_context` | `document_id` (required) | Full concatenated chunk text + metadata header | `GET /v1/chunks/{document_id}/context` |
| `verify_claim` | `claim` (required); `evidence[]` | Offline lexical claim-vs-evidence support scoring | `POST /v1/verify-claim` |
| `explain_lineage` | `document_id` (required); `chunk_id` | Provenance + freshness for a document or chunk | `GET /v1/documents/{id}/lineage` |

### Write (`write` permission)

| Tool | Parameters | Purpose | REST twin |
| --- | --- | --- | --- |
| `upload_document` | `filename`, `content` (required); `content_type` (optional — omit it: derived from `filename`'s extension, see below), `workspace_id` | **Text-only** ingestion sharing REST's validate/dedup/store/enqueue pipeline. Binary formats (PDF/DOCX/PNG) and JSON are REST-only — use `POST /v1/documents`. If the key owns several workspaces, `workspace_id` is required | `POST /v1/documents` |
| `delete_document` | `document_id` (required) | Permanently delete document + vectors + chunks + stored bytes | `DELETE /v1/documents/{id}` |
| `refresh_stale_source` | `document_id` (required) | Re-enqueue an uploaded document to clear staleness; on MQ failure a retried best-effort compensation marks it `failed`, matching REST (see the REST reference for exhaustion behavior) | `POST /v1/documents/{id}/refresh` |

**`content_type`: omit it.** The `upload_document` accepted `content_type`
set is the `surfaces` field of the [file-type registry](file-types.md)
entries that include `mcp` — see that page for the full list, and for which
formats are REST-only (binary). When `content_type` is omitted, it is
**derived from `filename`'s extension** when recognized (`.py` ->
`text/x-python`, `.md` -> `text/markdown`, `.csv` -> `text/csv`, `.yaml` ->
`application/yaml`, `.sql` -> `application/sql`, and more — see the registry
link above for the full extension list), falling back to `text/markdown`
only for an unrecognized or absent extension (#117). For an extension whose
registry entry covers several distinct languages (source code — `.py`,
`.go`, `.java`, ...), the derived type resolves to that specific extension's
own MIME type, not a fixed first entry (#197: a `.go` upload with
`content_type` omitted resolves to `text/x-go`, not `text/x-python`).

If you DO declare `content_type` explicitly, it is always honored exactly as
given and never re-derived from the filename — so an explicit
`content_type` that disagrees with the extension (e.g. declaring
`text/markdown` for a `.go` file) is accepted as declared, not silently
corrected. This is why the tool schema carries no `default` for
`content_type`: several real MCP clients pre-fill an omitted argument from
its schema-advertised default before the server ever observes an omission,
which would turn every upload into an explicit `text/markdown` declaration
and defeat the extension-derivation above (#197's exact defect,
reintroduced through the schema). Omit the field; do not pass
`"text/markdown"` explicitly unless you actually mean it.

## Notes

- Search tools do not take `include_context` / `context_window` — use
  `get_document_context` for surrounding text.
- Permissions are exact membership, same as REST: `write` does not imply
  `read` or `search`.
- Document-scoped tools (`get_document`, `list_chunks`, `explain_lineage`,
  `delete_document`, `refresh_stale_source`, `get_document_context`) answer a
  document id that doesn't exist and one that exists in a workspace you
  aren't authorized for with the SAME `Error: Document '<id>' not found` —
  matching REST's undifferentiated `404` (#138). Do not rely on distinguishing
  these two cases; neither surface tells them apart.
- `search_documents` / `search_memory` / `get_citations` / `list_documents`
  carry a `workspaces_searched` field in their structured JSON payload (the
  fenced ```json block after the summary) listing the workspace ids the call
  actually covered — check it rather than assuming the prose summary means
  every workspace you own was searched, which is only true for a user-scoped
  key with no narrower request.
