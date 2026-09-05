# MCP tools reference

The public API service ships an MCP server (`inherent-knowledge-base`) on
**two transports** — stdio and Streamable HTTP — exposing the same
capabilities as the REST API with matching permission enforcement and
failure behavior. Both transports derive from the exact same `_TOOLS`
registry (`src/mcp_server/server.py`), so the advertised surface cannot drift
from the enforced one on either transport.

## Transports

### Streamable HTTP (customer-facing, #220)

`POST /mcp` is mounted on the SAME app/port as the REST API
(`inh-public-api-svc`) — no separate process, no database credentials on
the client side. This is the transport SaaS customers use:

```bash
claude mcp add --transport http inherent https://api.inherent.sh/mcp \
  --header "X-API-Key: ink_..."
```

- **Auth: header, not schema.** The API key comes from the `X-API-Key` or
  `Authorization: Bearer` request header — the SAME dependency REST routes
  use (`get_api_key_info` in `src/services/auth.py`) — and **`api_key` is
  removed from every tool's schema** on this transport. An agent that sees
  `api_key` in a schema will hunt for the secret and may echo it into
  context, logs, or transcripts; routing auth through the header instead of
  a tool argument removes that surface entirely. Missing/invalid/expired
  keys get the same 401 REST returns, before any JSON-RPC request is even
  parsed.
- **Tool surface: 12 of 16.** `verify_claim`, `search_memory`,
  `get_citations`, and `report_feedback` are not advertised and cannot be
  called by name over HTTP
  (see [Surface difference](#surface-difference-http-vs-stdio) below) —
  unchanged on stdio.
- Rides the REST app's existing middleware stack — CORS, security headers,
  audit logging, and **rate limiting** all apply to `/mcp` the same way they
  apply to `/v1/*`, by construction (no second copy to keep in sync).
- Stateless: every call (`initialize`, `tools/list`, `tools/call`) is one
  independent HTTP request; the key is re-validated on every call.
- Tool errors set `isError: true` with a machine-branchable `error_class` in
  `structuredContent` (e.g. `authorization_failed`, `not_found`,
  `validation_error`, `unknown_tool`, `internal_error`) instead of the plain
  prose stdio still returns.

### OAuth 2.1 resource-server discovery (hosted only, #295)

`/mcp`'s auth is `X-API-Key` / `Bearer ink_...` only, by default -- nothing
below changes unless `OAUTH_ENABLED=true` is set (default **false**). A
self-hosted deployment should leave this off: turning it on advertises
`OAUTH_AUTHORIZATION_SERVER` as a trusted issuer for this resource, so it
must only ever be set to an authorization server the operator actually
runs (or, for the hosted SaaS deployment, Clerk).

With `OAUTH_ENABLED=true`:

- `GET /.well-known/oauth-protected-resource` serves an
  [RFC 9728](https://datatracker.ietf.org/doc/html/rfc9728)
  protected-resource metadata document naming `OAUTH_AUTHORIZATION_SERVER`
  and the minimal `OAUTH_SCOPES_SUPPORTED` catalogue (default `["kb:read",
  "kb:search"]` -- write access is never advertised upfront; it arrives via
  an `insufficient_scope` step-up on the specific tool that needs it,
  returned as a JSON-RPC `tools/call` result -- `isError: true`,
  `structuredContent.error: "insufficient_scope"`, `structuredContent.scope`
  naming what is missing -- at HTTP **200**, not a transport-level 403: the
  Streamable HTTP transport always answers a parsed `tools/call` with 200,
  so a per-tool scope check, which only runs once the request body has been
  parsed, has no way to change the surrounding HTTP status. A true HTTP 403
  challenge is reserved for connection-level rejection below, which runs
  before the body is parsed at all).
  This route does not exist at all when OAuth is disabled (a request 404s
  exactly as if the route were never registered, because it wasn't).
- An unauthenticated `/mcp` request's 401 carries a combined
  `WWW-Authenticate` header advertising BOTH schemes --
  `ApiKey, Bearer resource_metadata="...", scope="kb:read kb:search"` --
  never silently dropping the `ApiKey` challenge existing clients rely on.
- `Authorization: Bearer <token>` is treated as an OAuth access token only
  when the token does NOT start with the `ink_` API-key prefix; a `Bearer
  ink_...` value keeps resolving through the unchanged API-key path.
  Presented tokens are verified against `OAUTH_AUTHORIZATION_SERVER`'s
  published JWKS: signature, `iss`, `exp`, and -- non-negotiably, per
  [RFC 8707 Sec 2](https://datatracker.ietf.org/doc/html/rfc8707#section-2)
  -- that `aud` contains `OAUTH_RESOURCE_IDENTIFIER`. A token for any other
  resource is rejected outright, not merely logged about. An expired token
  always comes back as 401 (never 403), so a client's silent-refresh path
  can key off status code alone.
- Config: `OAUTH_ENABLED`, `OAUTH_AUTHORIZATION_SERVER`,
  `OAUTH_RESOURCE_IDENTIFIER`, `OAUTH_SCOPES_SUPPORTED`, `OAUTH_JWKS_URL`
  (optional override; defaults to
  `<OAUTH_AUTHORIZATION_SERVER>/.well-known/jwks.json`),
  `OAUTH_JWKS_CACHE_SECONDS` -- see `src/config/settings.py` for the full
  field docs.
- **Scope of #295**: this issue is the resource-server contract only
  (discovery + the 401 challenge shape + the insufficient-scope JSON-RPC
  shape + token verification). A
  verified OAuth caller with sufficient scope for a tool still gets a
  clearly-labeled "not yet available" rejection on `tools/call` --
  executing a tool needs mapping the token's `sub` to an Inherent
  user/workspace, which needs the identity link the commercial platform
  owns, not this repo (see issue #295's "Scope" section). `tools/list`
  works today for an OAuth caller since it needs no workspace resolution.

### stdio (self-hosters / internal development)

- Start the service with `SERVICE_MODE=mcp`; the MCP server runs as its own
  process (not mounted on the REST app), for self-hosters and internal dev
  running the full stack with production-shaped credentials.
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
- **All 16 tools** are advertised and callable, including the 4 excluded
  from HTTP (below) — unaffected by the HTTP transport's existence.

## Surface difference: HTTP vs stdio

| | stdio | Streamable HTTP |
| --- | --- | --- |
| Tool count | 16 | 12 |
| API key | `api_key` schema argument | `X-API-Key` / `Authorization` header |
| `verify_claim` | ✅ | ❌ excluded |
| `search_memory` | ✅ | ❌ excluded |
| `get_citations` | ✅ | ❌ excluded |
| `report_feedback` | ✅ | ❌ excluded |
| Tool error shape | prose `TextContent`, `isError: false` | `isError: true` + `error_class` |

Four tools are excluded from HTTP (`ToolDef.http_exposed = False` in the
registry — the exclusion is data on the tool's own entry, not a second
name list maintained separately):

- **`verify_claim`** — `src/services/verify.py` is an offline lexical
  token-overlap counter with no LLM and no negation handling. Against the
  source sentence *"Either party may cancel this Agreement at any time,"*
  it scores the claim *"**Neither** party may cancel this Agreement at any
  time"* as `strong, 0.833` support — the opposite of the truth. Sound as an
  internal pre-filter; unsafe under a tool name an HTTP agent reads as
  entailment. Still on stdio and REST; will return to HTTP once it is NLI-
  or LLM-backed.
- **`search_memory`** — identical behavior to `search_documents`; two tools
  doing one job costs every HTTP agent permanent context overhead for no
  added capability.
- **`get_citations`** — same parameters and endpoint as `search_documents`,
  whose results already carry a full `citation` object per result
  (`chunk_id`, `document_name`, `content`, `start_char`, `end_char`).
- **`report_feedback`** — stdio/REST-only, not part of issue #220's original
  original HTTP list; a pending decision, not a permanent exclusion.

## Tools

The full, stdio-side catalogue (all 16 tools). The **HTTP** column marks
whether a tool is also on the Streamable HTTP surface (see
[Surface difference](#surface-difference-http-vs-stdio) above). On
stdio every tool requires `api_key` (string) as a schema argument; on HTTP
the key is a header and `api_key` never appears in the schema. Additional
parameters below.

### Search (`search` permission)

| Tool | HTTP | Parameters | Purpose | REST twin |
| --- | --- | --- | --- | --- |
| `search_documents` | ✅ | `query` (required); `workspace_id`, `limit` (10), `min_score` (0.0), `document_ids[]`, `search_mode` (`semantic`/`hybrid`/`keyword`), `alpha` (0.7) | Search chunks; with no `workspace_id`, fans out across every workspace the key is authorized for (its one bound workspace if scoped, otherwise every workspace its owner owns). A single-workspace call also mints an eval capture event and returns its id as `event_id` (#241) — pass it to `report_feedback` | `POST /v1/search` |
| `search_memory` | ❌ | same as `search_documents` | Memory-primitive alias — identical behavior, including `event_id` capture | `POST /v1/search` |
| `get_citations` | ❌ | same as `search_documents` | Search returning claim-level citation objects (spans, score, provenance, freshness). Retrieves through the same shared path as `search_documents`, but does NOT mint an eval capture event — capture is opt-in at that call site (#241 review), and get_citations has never returned an `event_id` an agent could give feedback against | `POST /v1/search` |
| `report_feedback` | ❌ | `event_id`, `verdict` (`answered`/`partial`/`not_relevant`) required; `useful_chunk_ids[]`, `note` | Record a verdict on a captured search event (the `event_id` from `search_documents` / `search_memory`, or from `POST /v1/search`); builds the workspace eval set | `POST /v1/evals/feedback` |
| `get_retrieval_health` | ✅ | `workspace_id` (required) | Workspace retrieval scorecard | `GET /v1/evals/scorecard` |

### Read (`read` permission)

| Tool | HTTP | Parameters | Purpose | REST twin |
| --- | --- | --- | --- | --- |
| `whoami` | ✅ | none | Return the authenticated key's identity, binding, authoritative workspace set, engine version, and endpoint | `GET /v1/whoami` |
| `list_workspaces` | ✅ | (none — uses authorized workspaces only) | List caller's authorized workspaces with metadata. Response includes top-level `is_scoped_binding` flag (true for workspace-scoped keys, false for user-scoped) and array of workspaces, each with `workspace_id`, `name` (null if not set), and `document_count`. A workspace-scoped key sees exactly its bound workspace; a user-scoped key sees every workspace its owner owns | No direct REST twin; closest: `GET /v1/documents` |
| `list_documents` | ✅ | `workspace_id`, `page` (1), `page_size` (20) | Paginated document listing | `GET /v1/documents` |
| `get_document` | ✅ | `document_id` (required) | Single document's metadata | `GET /v1/documents/{id}` |
| `list_chunks` | ✅ | `document_id` (required) | All chunks for a document | `GET /v1/chunks/{document_id}` |
| `get_document_context` | ✅ | `document_id` (required); `max_chars` (default 20,000, capped at 100,000), `offset` (default 0) | Bounded window of chunk text + metadata header, capped at `max_chars` (#219: an uncapped call used to be able to exhaust an agent's own context window). Structured JSON block carries `truncated`, `total_chars`, `offset`, `next_offset` — if `truncated` is `true`, re-call with `offset=next_offset` for the rest | `GET /v1/chunks/{document_id}/context` |
| `verify_claim` | ❌ | `claim` (required); `evidence[]` | Offline lexical claim-vs-evidence support scoring | `POST /v1/verify-claim` |
| `explain_lineage` | ✅ | `document_id` (required); `chunk_id` | Provenance + freshness for a document or chunk | `GET /v1/documents/{id}/lineage` |

### Write (`write` permission)

| Tool | HTTP | Parameters | Purpose | REST twin |
| --- | --- | --- | --- | --- |
| `upload_document` | ✅ | `filename`, `content` (required); `content_type` (optional — omit it: derived from `filename`'s extension, see below), `workspace_id` | **Text-only** ingestion sharing REST's validate/dedup/store/enqueue pipeline. Binary formats (PDF/DOCX/PNG) and JSON are REST-only — use `POST /v1/documents`. If the key owns several workspaces, `workspace_id` is required | `POST /v1/documents` |
| `delete_document` | ✅ | `document_id` (required) | Permanently delete document + vectors + chunks + stored bytes | `DELETE /v1/documents/{id}` |
| `refresh_stale_source` | ✅ | `document_id` (required) | Re-enqueue an uploaded document to clear staleness; on MQ failure a retried best-effort compensation marks it `failed`, matching REST (see the REST reference for exhaustion behavior) | `POST /v1/documents/{id}/refresh` |

**`content_type`: omit it.** The `upload_document` accepted `content_type`
set is the `surfaces` field of the [file-type registry](file-types.md)
entries that include `mcp` — see that page for the full list, and for which
formats are REST-only (binary). When `content_type` is omitted, it is
**derived from `filename`'s extension** when recognized (`.py` ->
`text/x-python`, `.md` -> `text/markdown`, `.csv` -> `text/csv`, `.yaml` ->
`application/yaml`, `.sql` -> `application/sql`, and more — see the registry
link above for the full extension list), falling back to `text/plain`
only for an unrecognized or absent extension (#117, #208 — e.g. `Dockerfile`,
`Makefile`, `README`, `.gitignore`, `archive.tar.gz`; `text/plain` is the
honest generic for "a text file whose format we did not recognize," not a
`text/markdown` guess). For an extension whose
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

- `list_workspaces` (#297) returns only workspaces the caller's API key is
  authorized for — a workspace-scoped key sees exactly one, its bound workspace,
  while a user-scoped key sees every workspace its owner owns. Returns an
  empty list (not an error) if the caller is authorized for zero workspaces.
  Each workspace object includes `workspace_id`, `name` (null if not set in
  metadata), and `document_count`. The response also carries a top-level
  `is_scoped_binding` flag (always `true` for a workspace-scoped key; always
  `false` for a user-scoped key) describing the caller's key type. Use
  `list_workspaces` to discover valid `workspace_id` values before calling
  workspace-targeted tools like `upload_document`.
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
- `search_documents` / `search_memory`'s structured payload also carries
  `event_id` (#241) — the same handle `POST /v1/search`'s `SearchResponse`
  returns, minted and recorded through the same shared capture path as REST.
  It is `null` for a multi-workspace search (no single response to attribute
  one event to, matching REST's own single-workspace-only capture scope) and
  whenever eval capture is disabled for the workspace or the write failed.
  Pass a non-null `event_id` to `report_feedback` to close the loop; see
  [ADR 0003](../adr/0003-traffic-mined-retrieval-evals.md) for the flywheel
  this feeds.
- **Capture is opt-in at the shared retrieval call site, not implicit for
  every caller of it (#241 review).** `search_documents` / `search_memory`
  request capture explicitly; `get_citations` shares the identical retrieval
  code path (`_run_search`) but does not, so it never mints an
  `eval_query_events` row — an event get_citations could never surface an
  `event_id` for, and that no agent could therefore ever attach feedback to,
  would just double-count that query in MCP analytics.
- **`quality_verdict` is always `NULL` on `transport='mcp'` captured rows.**
  REST's `POST /v1/search` runs an adaptive retrieval quality gate and a
  single bounded fallback retry before capturing (`quality_verdict` populated,
  and the captured results/latency are the fallback's when one ran, see
  `docs/developer/search-sequence.md`); MCP's `search_documents` /
  `search_memory` call `SearchService.search` directly, with no quality gate
  on that path at all. The two transports therefore capture different
  *inputs* for the same query even though they write through the identical
  `capture_search_event` helper and record field list. Segmenting eval
  analytics by `quality_verdict` must account for this — every `mcp` row will
  read as ungated, never as a fallback.
