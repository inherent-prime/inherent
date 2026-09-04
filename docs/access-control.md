# Access-control model

**The workspace is the security boundary.** Inherent has no `user_tier`
parameter, no document-level ACL, and no metadata filter on search. If two
bodies of content must not reach the same caller, put them in two workspaces.

## Local admin exception

`GET /v1/admin/workspaces` and `GET /v1/admin/keys` intentionally list the
whole stack for a local single operator. `ADMIN_API_ENABLED` defaults to
`false`; disabled routes return `404`. Never enable this flag in SaaS or any
multi-operator deployment. The API is read-only and never returns key values
or hashes. MCP exposes no admin tools; agents use `whoami` for their own scope.

Read this before you design a clearance, tenancy, or need-to-know scheme on
top of Inherent.

## What is enforced

| Layer | Mechanism | Where |
| --- | --- | --- |
| Key → workspace | A workspace-scoped key is bound to one workspace; a mismatching `X-Workspace-Id` (REST) or `workspace_id` argument (MCP) is rejected. A user-scoped key may name any workspace the user owns; anything else is rejected | `services/inh-public-api-svc/src/services/auth.py` (`get_authorized_workspace_ids`, consumed by `_resolve_workspace` for REST and `src/mcp_server/server.py` for MCP) |
| Workspace → storage | One Weaviate collection per workspace (`Workspace_<encoded id>`), one tenant per user inside it (`User_<encoded id>`). Names are derived deterministically and injectively from the raw ids, so distinct ids can never collide onto one collection | `services/inh-contracts/src/inh_contracts/naming.py` |
| Query → tenant | Every Weaviate query carries the caller's tenant and targets a single workspace collection. A single-workspace search cannot read another workspace's collection | `services/inh-public-api-svc/src/services/search.py` (`_search_weaviate`) |
| Fan-out → authorized set | With no `X-Workspace-Id` / `workspace_id`, read/search fan out only over `get_authorized_workspace_ids(key_info, database)` — the caller's authorized set (the key's own workspace when scoped, otherwise every workspace the user owns) — so merged results cannot cross authorization | `services/inh-public-api-svc/src/api/v1/search.py` (REST), `services/inh-public-api-svc/src/mcp_server/server.py` (MCP) |

The REST and MCP surfaces share one implementation of the key-scoping rule
(`get_authorized_workspace_ids` in `src/services/auth.py`) — neither surface
derives workspace access from the user's full owned-workspace set on its own,
so a workspace-scoped key binds identically everywhere it is used
([#138](https://github.com/inherent-prime/inherent/issues/138)).

A document that exists in a workspace the caller isn't authorized for and a
document id that doesn't exist at all are answered IDENTICALLY on both
surfaces — REST's `404 "Document not found"`, MCP's
`Error: Document '<id>' not found` — so a caller cannot probe for the
existence of content outside their workspaces on either transport. (MCP's
document-scoped tools closed this identically in #138's follow-up; before
that they had a separate "you don't have access to document" message for the
unauthorized case, which was a working cross-workspace existence oracle a
scoped key could use to enumerate ids in a workspace it couldn't read.) See
the [REST API reference](reference/rest-api.md#workspace-scoping) for the
exact status codes and [ADR 0002](adr/0002-weaviate-multi-tenancy-scale.md)
for the tenancy model.

## What tenant scoping does not do

Tenant scoping isolates **workspaces from each other**. It does not partition
content **inside** a workspace:

- **Every document in a workspace is reachable by every key authorized for
  that workspace.** There is no per-document, per-folder, per-label, or
  per-clearance restriction.
- **The per-user Weaviate tenant is not a security boundary you should design
  against.** Vector search is scoped to the caller's user tenant, but the
  document and chunk read paths (`GET /v1/documents`,
  `GET /v1/chunks/{document_id}`) are workspace-scoped only. In the common
  deployment where one service identity uploads everything, every key for that
  workspace retrieves the whole workspace corpus.
- **`document_ids` on `POST /v1/search` is a caller-supplied narrowing, not an
  access control.** The caller chooses it and can omit it. Never treat it as
  enforcement.
- **Scores carry no clearance signal.** `min_score`, `search_mode`, and
  `alpha` change ranking, not authorization.

## Pattern: one workspace per clearance tier

Model each clearance tier as its own workspace and scope keys to the tiers
their holder may read.

| Tier | Workspace | Holds |
| --- | --- | --- |
| Tier 1 (public / contractor) | `ws_tier1` | Unrestricted handbooks, public policy |
| Tier 2 (employee) | `ws_tier2` | Internal process, org-only material |
| Tier 3 (restricted) | `ws_tier3` | Need-to-know material |

Provision keys against that split — no key-management API exists today, so
provisioning is script- or SQL-driven (see
[Production hardening §8](deploy/production.md#8-provision-workspaces-and-api-keys)):

- **Contractor key** — workspace-scoped to `ws_tier1`. Sending
  `X-Workspace-Id: ws_tier2` (REST) or `workspace_id: "ws_tier2"` on an MCP
  tool call returns an error on both surfaces. Tier 2 and Tier 3 content is
  not in the collection the key can query, so it cannot be retrieved at any
  `limit` or `min_score`.
- **Employee key** — user-scoped to a user owning `ws_tier1` and `ws_tier2`.
  Omitting `X-Workspace-Id` fans out over exactly those two.
- **Restricted key** — user-scoped to a user owning all three.

A caller reads the union of the tiers their key authorizes, and nothing else.
Content that must never reach a tier is never in a collection that tier's keys
can query — the guarantee holds without depending on ranking, filters, or
query construction.

Cost of the pattern: a document needed at several tiers is uploaded to each of
those workspaces and embedded once per workspace.

## Anti-pattern: one workspace, filter at query time

Loading every tier into a single workspace and expecting search to separate
them does not work, and is the most common misreading of this system:

- There is no retrieval parameter that expresses clearance. A query issued
  with a fully privileged key returns candidates from the whole workspace —
  correct behavior for that key, not a leak.
- Filtering the response in the calling application is not a boundary. The
  restricted chunks were already retrieved, scored, and returned over the wire.

If an evaluation reports "higher-tier documents appear in a lower tier's
candidate set", check the workspace layout first: a single mixed workspace
queried with a privileged key reproduces exactly that result by design.

## No document-level ACLs today

Inherent has no custom document metadata and no query-time metadata filters.
Document-level permissions cannot be expressed, and this page describes the
whole model. Custom metadata plus query-time filters is tracked as
[#160](https://github.com/inherent-prime/inherent/issues/160) — read it as an
open request, not as planned behavior.

## See also

- [REST API reference](reference/rest-api.md) — permissions, headers, status codes
- [Keeping content current](keeping-content-current.md) — the other half of
  "why did that document come back"
- [ADR 0002 — Weaviate multi-tenancy](adr/0002-weaviate-multi-tenancy-scale.md)
- [Production hardening](deploy/production.md) — provisioning workspaces and keys
