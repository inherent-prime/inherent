# Audit log

Every search-shaped query — REST or MCP, API-key or OAuth caller — publishes
one audit event to the `audit.log.write` MQ topic
(`src/services/audit_publisher.py` in `inh-public-api-svc`), which
`inh-ingestion-svc`'s `AuditLogConsumer` + `WriteAuditLogWorkflow` write into
MongoDB's `audit_logs` collection. This page lists every field on that event.

## Coverage (#393)

100% of retrieval-returning query calls publish an event, on every surface:

- **REST**: `POST /v1/search` (single- and multi-workspace).
- **MCP**: `search_documents`, `search_memory`, `get_citations`,
  `get_document_context`, `list_chunks`, and every vertical-pack profile
  tool — on stdio, HTTP (API-key caller), and HTTP (OAuth caller) alike.
  Wired at ONE choke point per transport (`src/mcp_server/audit.py`'s
  `dispatch_and_audit` / `audit_denied`), not one call per tool handler, so a
  new retrieval-returning tool is covered by marking its `ToolDef.
  returns_chunk_content=True` rather than adding a publish call to its
  handler.
- A call that is **denied** (missing permission/scope, quota exceeded, or an
  OAuth token with no linked Inherent identity) or **errors** still publishes
  an event — with `outcome` set accordingly and no returned chunk ids — so
  the audit trail is never silently missing a query someone actually made.
- Publishing is always fire-and-forget: a failure to publish (MQ down, no
  event loop) is logged and swallowed, and can never block or fail the
  underlying tool/search call.

A tool that never returns retrieved chunk content (`whoami`,
`upload_document`, `delete_document`, chunk CRUD, `list_workspaces`, …) is
unaffected — no audit event, same as before #393.

## Event fields

Fields present on every event (unchanged since before #393):

| Field | Type | Notes |
|---|---|---|
| `audit_id` | string (UUID) | Unique id for this event; also the Mongo `_id` (idempotent upsert). |
| `workspace_id` | string | Single-value, backward-compatible workspace label. `"multi"` for REST's multi-workspace fan-out; `"unknown"` for an MCP call with no resolved workspace yet (e.g. denied before dispatch). |
| `user_id` | string | The Inherent user this query is attributed to. |
| `api_key_id` | string | The API key id, or (MCP OAuth) the synthesized `oauth:<subject>` id — never the raw key or token. |
| `source` | `api_key` \| `dashboard` \| `chat` | Which product surface issued the request. Always `api_key` for MCP calls (see `surface` below for the transport distinction) — this field's enum is unchanged so prime's audit-log reader never has to change. |
| `query_type` | `search` \| `chat` | |
| `query_text` | string, ≤2000 chars | Truncated; falls back to `document_id=<id>` for a document-scoped tool call with no `query` argument (e.g. `get_document_context`). |
| `query_filters` | object | Vertical-pack tag filters or `document_ids`, when present. |
| `result_count` | int | |
| `result_snippets` | array, ≤5 | Each snippet's text capped at 200 chars. Populated by REST only today. |
| `returned_chunk_ids` | array of string | The chunk ids actually returned — empty for a denied/errored call, never populated as a stand-in for "zero results". |
| `response_time_ms` | float | |
| `request_id` | string (UUID) | |
| `query_timestamp` | ISO-8601 string | |

### Attribution fields (#393, additive)

Omitted entirely (not written as `null`) on any event that doesn't set
them — every consumer that reads these fields must treat their absence as
"unknown / pre-#393 event", not as a specific value:

| Field | Type | Notes |
|---|---|---|
| `principal_type` | `api_key` \| `oauth` | Which credential authenticated the call. Distinct from `source`, which is about product surface, not credential kind. |
| `principal_id` | string | The API key's id, or the OAuth token's subject — never the raw key/token. Same value as `api_key_id` for an API-key caller. |
| `surface` | `rest` \| `mcp` | Which transport the call came in on. |
| `tool_name` | string | The MCP tool name (including a vertical-pack profile tool's own name, e.g. `search_sections`) or the REST route name (`search_documents`). |
| `workspace_ids` | array of string | Every workspace actually queried by this call — more than one for a multi-workspace fan-out. `workspace_id` above stays the first of these (or the legacy `"multi"`/`"unknown"` label) for backward compatibility. |
| `outcome` | `ok` \| `error` \| `denied` | How the call ended. `denied`: rejected before the tool/handler ever ran (permission, scope, or quota). `error`: the handler ran and reported a failure (a raised exception, or its own `"Error: ..."` result). Omitted (⇒ implicitly `ok`) on every event published before #393 — REST's success-only publish path never had another outcome to record. |

## Where each field is set

- **Event shape**: `build_audit_event` (`src/services/audit_publisher.py`,
  `inh-public-api-svc`). The attribution fields are keyword-only and
  optional — a caller that omits them gets the exact pre-#393 event shape.
- **REST**: `_schedule_audit` (`src/api/v1/search.py`) — always
  `principal_type="api_key"`, `surface="rest"`,
  `tool_name="search_documents"`, `outcome="ok"` (REST's audit publish
  only ever runs after a successful response is built).
- **MCP**: `src/mcp_server/audit.py`'s `dispatch_and_audit` / `audit_denied`
  / `audit_denied_unresolved_oauth`, called once per tool call from each of
  the three dispatch loops (`src/mcp_server/server.py`'s stdio `call_tool`;
  `src/mcp_server/http_transport.py`'s HTTP `call_tool` and
  `_call_tool_oauth`). `principal_type`/`principal_id` are derived from the
  caller's `key_id` (an OAuth caller's is synthesized as
  `oauth:<principal_id>` by `_api_key_info_for_oauth`, inherent#392's
  follow-up), so no extra parameter has to be threaded through every
  dispatch site.
- **Returned chunk ids / workspaces**: a handler that returns chunk content
  calls `record_returned_chunk_ids` / `record_workspace_ids`
  (`src/mcp_server/audit.py`) — a small, contextvar-based mechanism, safe
  to call from a unit test that isn't inside an audited dispatch (a no-op
  there), so `ToolHandler`'s signature never had to widen to carry a second
  return value.

## Storage (ingestion) and reading (prime)

`inh-ingestion-svc`'s `audit_mongo_writer.upsert_audit_log` writes the
attribution fields additively (`event.get(...)`, defaulting `outcome` to
`"ok"` and `workspace_ids` to `[]` for a pre-#393 event) — no change to the
required-field validation in `audit_activities.validate_audit_event`, since
`source` never becomes anything outside its existing
`{api_key, dashboard, chat}` enum.

prime's Mongoose model (`inh-intg-svc`'s `auditLog.model.ts` /
`audit.types.ts`) declares `source` and `query_type` as fixed enums and reads
the rest of the document loosely; it is not touched by #393, and does not
need to be — the new fields simply aren't in its schema yet, and Mongoose
does not reject or drop unknown paths on a read.
