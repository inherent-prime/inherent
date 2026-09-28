# Vertical packs

A **vertical pack** is a small, separate repo that supplies domain config to
the generic core engine: chunking rules, a tag schema (with optional rules
for the rules tagger), tagger examples, and MCP tool profiles. Core has NO
domain terms in its code, docs, or tests — everything domain-specific lives
in the pack.

Contract and loader: `services/inh-contracts/src/inh_contracts/vertical_pack.py`
(`load_vertical`) and `vertical_discovery.py` (`discover_packs`,
`discover_entry_point_packs`, `discover_all_packs`). The first shipped pack
is the reference implementation of this exact format.

## Pack layout

```
<pack-root>/
  vertical.yaml                 # manifest — the single entry point core reads
  profiles/
    chunking/<name>.yaml        # ChunkingProfile
    tags/<name>.yaml            # TagSchema
    tagger/<name>.yaml          # TaggerExamples
    tools/<name>.yaml           # one or more ToolProfile
```

`vertical.yaml`:

```yaml
name: my-pack                   # ^[a-z][a-z0-9-]*$ — the pack's identity
version: 0.1.0
description: One line.
requires_core: ">=0.8.0"
profiles:
  chunking: profiles/chunking/my-chunking.yaml
  tags: profiles/tags/my-tags.yaml
  tagger_examples: profiles/tagger/examples.yaml
  tools:
    - profiles/tools/my-tool.yaml
```

`load_vertical(root)` parses the manifest and every profile it points to,
then cross-validates: a tool's `filters` must be declared tag fields, a
tool's `default_limit` must not exceed `max_limit`, every tagger example's
tags must match the tag schema, and (see below) every tag rule key must be
one of that field's own enum values. Any failure raises `VerticalError`
naming the file and the problem — never a partial/silently-wrong pack.

## Discovery

Discovery is OFF by default. An unset/nonexistent directory and no
registered entry points both mean "no packs" — every existing workspace
behaves exactly as it did before packs existed.

- **Directory**: set `VERTICAL_PACKS_DIR` (a service setting, read by that
  service — the discovery function itself just takes a path) to a directory
  containing one subdirectory per pack, each with its own `vertical.yaml` at
  its root. `discover_packs(dir)` returns `{name: Vertical}`, keyed by the
  manifest's own `name` (not the directory name).
- **Entry points**: a pack can ship as an installed Python package
  registering under the `inherent.verticals` entry-point group, value = the
  pack's root directory (a path string, or a zero-arg callable returning
  one). `discover_entry_point_packs()` loads all of them.
- `discover_all_packs(dir)` combines both; a directory pack wins a name
  collision.

A pack that fails to load is collected in `DiscoveryResult.errors` (keyed by
source name), never raised — one broken pack must never break discovery of
the others, or a no-packs deployment.

## Workspace binding

A workspace opts into a pack via a nullable `vertical_pack` field (the pack's
`name`) threaded through as plain data — e.g.
`ChunkTextInput.vertical_pack` / `DocumentIngestionInput.vertical_pack` in
`inh-ingestion-svc`. `None` (the default) is "no pack" and every feature
below is a no-op.

Workspace records are owned by a different service/repo (not this engine),
so there is no column here to read a workspace's bound pack from directly.
Until that binding exists, a hand-onboarded pilot is wired through one
operator setting, read **identically** by both services via
`inh_contracts.workspace_packs.parse_workspace_vertical_packs`:

```
WORKSPACE_VERTICAL_PACKS=ws_abc=support,ws_def=handbook
```

Comma-separated `workspace_id=pack_name` pairs; whitespace around entries and
around `=` is trimmed; a blank/unset value means no bindings at all (feature
off, every workspace unaffected — the default). Parsed **once at service
startup** (inside `Settings`, not lazily) — a malformed entry (missing `=`,
empty side, more than one `=`, or the same `workspace_id` bound twice) fails
the service to start with a clear error instead of silently degrading.

Resolution, in both services:

- `inh-public-api-svc`: `src/services/workspace_pack.py::resolve_workspace_pack(workspace_id)`
  looks `workspace_id` up in the parsed mapping, then loads that pack name
  from `VERTICAL_PACKS_DIR` (+ any `inherent.verticals` entry points),
  cached. Unmapped workspace, unset `VERTICAL_PACKS_DIR`, or a mapped name
  that isn't actually found under it — all degrade to `None`, never raise.
- `inh-ingestion-svc`: the mapping is resolved **in plain application code**
  that starts the workflow (`src/api/app.py`'s `/ingest` route,
  `src/temporal/trigger.py`'s two MQ-triggered construction sites) —
  **never inside workflow code**, which must stay deterministic (#38).
  Each site sets `DocumentIngestionInput.vertical_pack =
  settings.workspace_vertical_packs.get(workspace_id)` before calling
  `start_workflow`; the workflow itself just threads the already-resolved
  value through to `ChunkTextInput`/`TagChunksInput`, unchanged. An explicit
  `vertical_pack` already present on the input (there is no way to supply
  one today — no upload message field carries it) would still win over the
  mapping, since resolution only fills it in when it's `None`.

`resolve_workspace_pack` (public-api) stays the single, documented extension
point for a real workspace-record-backed binding, once one exists — every
caller already goes through that one function. Same for ingestion's two
construction sites: once workspace records carry their own `vertical_pack`,
replace the `settings.workspace_vertical_packs.get(...)` lookup there with a
real lookup, and nothing else changes.

## `numbered_sections` chunking

A pack's `ChunkingProfile`:

```yaml
strategy: numbered_sections
split_at_level: 2          # headings at/above this level start a new chunk
keep_parent_heading: true  # prepend ancestor headings to each chunk
max_tokens: 800            # oversize sections fall back to sentence splitting
heading_patterns:
  - level: 1
    regex: '^(\d+)\.?\s+(?=[A-Z])'   # group 1 MUST capture the number
  - level: 2
    regex: '^(\d+\.\d+)\.?\s+\S'
```

Patterns are tried top to bottom (most specific first); a heading deeper than
`split_at_level` stays inside its parent's body instead of starting its own
chunk. A leading markdown heading prefix (`#`, `##`, …) — extraction may emit
these — is stripped before matching, so a pack's patterns never need to know
about markdown.

Every chunk's content is prefixed with its ancestor headings (when
`keep_parent_heading`), so the section number/heading is visible directly in
the retrieved text, not a side channel the caller must resolve separately.
`ChunkData.section_heading` additionally carries just that chunk's own
heading line for structured display/filtering. A section whose own text
exceeds `max_tokens` falls back to sentence splitting, with the same
ancestor context re-injected into every resulting slice — never one
unbounded chunk.

Precedence in `inh-ingestion-svc`'s chunk activity (top to bottom):

1. Per-document override (`ChunkTextInput.strategy`)
2. `numbered_sections`, when the input's `vertical_pack` resolves to a pack
   whose `chunking.strategy` is `numbered_sections`
3. Registry `chunking_hint` (file-type-derived; unchanged from before packs)
4. Global config (`settings.chunking_strategy`)

## Tag schema + rules

```yaml
fields:
  section_type:
    type: enum
    values: [pricing, security, other]
    rules:                       # OPTIONAL — omit for a pack with no tagger
      pricing: ["price", "fee", "\\bcost\\b"]
      security: ["security", "encrypt"]
      # "other" has no rules on purpose — see fallback, below.
  product:
    type: string
  effective_date:
    type: date
```

`rules` (per enum field, optional, backward-compatible) maps an allowed enum
value to a list of case-insensitive keyword/regex patterns. Rule keys must be
values already declared in that field's `values`; every pattern must compile.

`RulesTagger` (`inh-ingestion-svc/src/temporal/activities/tagging.py`) is the
only tagger shipped today. For each enum field with `rules`: match every
value's patterns against `heading + "\n" + text`, case-insensitively; the
highest-scoring value wins. Zero matches → the field is **omitted**, unless
the schema declares an enum value literally named `other`, in which case
`other` is the explicit fallback. `string`/`date` fields are filled only from
document-level metadata passed at upload (e.g. a title) matching the field
name exactly — never guessed from chunk text.

An LLM tagger is out of scope today (the ingestion engine has no chat LLM
client) — `Tagger` is the extension point; see the `TODO` in `tagging.py`.

Tags are written to the chunk's `metadata` JSONB **and** to Weaviate as a
`tags` `TEXT_ARRAY` property of `"field=value"` strings — this lets any pack
schema field be filtered without a per-pack Weaviate schema change. Existing
collections gain the property automatically (same additive
`_reconcile_collection_properties` path already used for prior fields).

## Search filters

`POST /v1/search`'s `SearchRequest.filters: dict[str, str | list[str]]`
filters on a pack's tags — `{field: value}` or `{field: [v1, v2]}` (any-of).
Combinable with `document_ids` (both apply, ANDed). Applies to all three
search modes (semantic, hybrid, keyword) — they share one query-building
path.

Validation (before any Weaviate call):

- No pack bound for the workspace → `400`.
- A filter names a field the pack's tag schema doesn't declare → `400`,
  listing the pack's actual fields.
- Multi-workspace search (no `X-Workspace-Id`) does not support `filters` —
  each workspace may have a different pack, or none.

Each `SearchResult.tags: dict[str, str] | None` carries the chunk's own tags
(parsed back from the `"field=value"` array), so a caller can show which tag
values matched.

## MCP tool profiles (inherent#392)

A pack's `vertical.yaml` may declare one or more `tools:` — `ToolProfile`
entries (`name`, `description`, `filters` ⊆ the pack's own tag schema fields,
`default_limit`, `max_limit`) served over MCP as real, named tools, e.g.:

```yaml
# profiles/tools/search-sections.yaml
name: search_sections
description: >-
  Search the handbook's numbered sections for policy text relevant to a
  question. Returns matching section text with its section number and heading.
filters: [section_type, product]
default_limit: 3
max_limit: 10
```

Each profile becomes an MCP tool named `search_sections` whose `inputSchema`
is generated, not hand-written: a required `query`, an optional `limit`
(bounded by `max_limit`, defaulting to `default_limit`), and one optional
parameter per `filters` field — an `enum` tag field gets a JSON Schema
`enum` of its declared `values`; any other field type gets a plain string.
Calling it runs the same search path `search_documents` uses, pre-scoped to
the profile's own `filters`, and returns the same structured-output shape
(text, section heading when the chunking put one in `metadata`, tags,
`source_url`, document id/title, and score).

**Scope: Streamable HTTP only.** stdio's `tools/list` has no per-connection
caller identity at all — every stdio call carries its own `api_key` as a
plain tool argument, and `tools/list` itself is answered before any tool
call, with nothing to authenticate against. HTTP, by contrast, authenticates
the connection via the `X-API-Key`/`Authorization` header before either RPC
runs, so `tools/list`/`tools/call` there can resolve a caller's own bound
pack. A caller with no bound pack (the default, everyone until an operator
maps a workspace) sees **exactly today's tool list on both transports**,
byte for byte — nothing here is visible until a workspace is actually bound.

**Workspace resolution — the simplest safe rule.** A caller may be
authorized for zero, one, or several workspaces (#138), and different
workspaces may carry different packs, the same pack, or none. Rather than
namespacing every profile tool's name by workspace, profile tools are
advertised **only when the caller's authorized workspace set has exactly one
member**, and that one workspace is bound to a pack with at least one tool
profile. Zero or several authorized workspaces (even several sharing the
identical pack) get no profile tools at all — there is no single workspace a
bare `search_sections(query=...)` call could unambiguously mean. A
workspace-scoped API key (the common case for a pack-bound workspace) always
resolves to exactly one, so this costs that caller nothing. See
`src/mcp_server/tool_profiles.py`'s module docstring for the full reasoning.

**Name collisions.** A profile tool name that matches one of core's own
built-in tool names (`search_documents`, `whoami`, ...) is **skipped, with a
logged warning** naming the pack and the tool — never registered, and never
raised as a load-time error (a pack's *other* tool profiles still load
normally). This is deliberately NOT enforced inside `inh_contracts`'
`load_vertical` — the pack-loading contract must not know this service's own
built-in tool names.

**Friendly errors.** An invalid filter value, or a filter field the bound
pack's tag schema doesn't declare (`TagFilterError`), comes back as a plain
`"Error: ..."` tool result — on stdio, `search_documents`/`search_memory`
(which now also accept the same `filters` shape as REST); on HTTP, both
those and every profile tool — never a raw 500 or an unhandled-exception
traceback.

**OAuth callers.** Profile tools ARE advertised to an OAuth-authenticated
caller (inherent#392 follow-up) once the token's subject resolves to an
Inherent user — via `OAUTH_USER_ID_CLAIM` or `OAUTH_SUBJECT_USERS` — using
the SAME single-authorized-workspace rule above, on that resolved user's own
workspaces. An OAuth caller with no resolved identity (the default, until an
operator configures one of those settings) sees no profile tools, same as
any caller with no bound pack; see [`mcp-tools.md`](./mcp-tools.md)'s "OAuth
callers can execute tools" section for the full identity-link contract.
