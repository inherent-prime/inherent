# ADR 0002 — Weaviate Multi-Tenancy and Scale Strategy

- **Status:** Accepted. **ADR text corrected 2026-08-04** — the naming scheme
  it describes shipped in code a month earlier, **2026-07-04**. See
  [Amendment](#amendment-2026-08-04-injective-base32-naming-issue-1) for both
  the code change and the gap.
- **Date:** 2026-06-20
- **Deciders:** maintainers
- **Closes:** #12
- **Related:** [ADR 0001](0001-agent-memory-substrate.md)

## Context

Inherent stores document chunk vectors in Weaviate and must keep every
workspace's data isolated from every other workspace, and every user's data
isolated within their workspace. Two services touch this storage:

- `inh-ingestion-svc` writes chunks (extract → chunk → embed → index).
- `inh-public-api-svc` reads chunks (search / retrieve).

For isolation to work, both services must compute the **exact same** Weaviate
collection and tenant names from the same workspace and user identifiers.

> **Superseded 2026-07-04 in code (ADR corrected 2026-08-04).** The rest of
> this paragraph is 2026-06-20's design. A shared `inh-contracts` package now
> provides the one implementation both services import — see
> [Amendment](#amendment-2026-08-04-injective-base32-naming-issue-1).

There is no shared library between the services today, so the naming rules are
duplicated — once in `inh-ingestion-svc/src/services/weaviate.py` and once in
`inh-public-api-svc/src/services/search.py`. Any silent divergence between the
two copies would route reads and writes to different physical locations and
manifest as "ingested documents are not searchable," which is hard to diagnose.

## Decision

### Tenancy model

- **One Weaviate collection per workspace.** The collection name is
  `Workspace_<sanitized_workspace_id>`.
- **One Weaviate tenant per user**, living inside that workspace collection. The
  tenant name is `User_<sanitized_user_id>`.

> **Superseded 2026-07-04 in code (ADR corrected 2026-08-04).** The
> sanitization description below is the original, now-wrong scheme: it is
> lossy and non-injective, and shipped a cross-tenant leak (#1). See
> [Amendment](#amendment-2026-08-04-injective-base32-naming-issue-1) for the
> current implementation. Kept below as the historical record of what
> 2026-06-20 originally accepted.

Sanitization strips every non-alphanumeric character from the raw identifier
(`re.sub(r"[^a-zA-Z0-9]", "", id)`) before applying the `Workspace_` / `User_`
prefix. This keeps names valid as Weaviate class/tenant identifiers regardless
of the formatting of upstream IDs (UUIDs, slugs, emails, etc.).

This gives hard isolation at two levels: workspaces never share a collection,
and Weaviate's native multi-tenancy isolates users within a collection.

### Guarding the drift risk with a golden naming contract

> **Superseded 2026-07-04 in code (ADR corrected 2026-08-04).** The golden
> outputs in the table below (`Workspace_wslocal001`, `User_localdevuser`) and
> this subsection's "two duplicated implementations" premise are historical.
> See [Amendment](#amendment-2026-08-04-injective-base32-naming-issue-1) for
> current golden values and the single shared implementation.

Because the naming logic is duplicated across two services with no shared
package, drift is the primary risk. We guard it with **golden naming contract
tests in BOTH services** that pin the same fixed input → output vectors:

| Raw input        | Function                          | Expected output (golden) |
|------------------|-----------------------------------|--------------------------|
| `ws_local_001`   | workspace-collection naming       | `Workspace_wslocal001`   |
| `local-dev-user` | user-tenant naming                | `User_localdevuser`      |

These vectors correspond to the local dev workspace and user, so they are also
exercised end-to-end by the local smoke test. The ingestion-side contract lives
in `services/inh-ingestion-svc/tests/test_multi_tenancy.py` and the public-api
side in `services/inh-public-api-svc/tests/unit/test_search_service.py`. If
either service's sanitization changes (e.g. someone preserves underscores or
lowercases differently), its golden test fails in CI before the divergence can
ship and break cross-service retrieval.

## Known and assumed scale limits

The current model is deliberately simple and is correct for the present scale,
but it has assumed ceilings worth recording:

- **Collections grow linearly with workspaces.** One collection per workspace
  means thousands of workspaces become thousands of Weaviate collections. Each
  collection carries schema and index overhead; very large collection counts
  pressure Weaviate memory and startup/schema-load time.
- **Tenants grow linearly with users per workspace.** Native multi-tenancy
  scales to many tenants per collection, but tenant activation/load has a
  per-tenant cost; a workspace with a very large user count concentrates that
  cost in a single collection.
- **No sharding by tier or region.** All workspaces live in one Weaviate
  cluster; there is no placement strategy separating large/noisy tenants from
  small ones, nor any per-tier resource isolation.
- **Resolved 2026-07-04 in code (ADR corrected 2026-08-04; see
  [Amendment](#amendment-2026-08-04-injective-base32-naming-issue-1)).**
  Originally: naming was single-sourced only by tests, not by code — the
  contract was enforced by pinning the same golden value into two
  independently maintained implementations that had to be edited in lockstep.
  Naming now has one code implementation, imported by both services.

These limits are acceptable today (early scale, local-first / single-cluster
deployments) and are documented here so the trigger points for the next phase
are explicit rather than discovered in an incident.

## Future scaling path

When the limits above start to bind, the planned evolution is:

1. **Done 2026-07-04 in code (ADR corrected 2026-08-04; see
   [Amendment](#amendment-2026-08-04-injective-base32-naming-issue-1)).**
   Originally planned: extract a `shared-contracts` package moving the
   workspace/user naming functions into a single package imported by both
   services, replacing duplicated-code-plus-golden-tests with a single source
   of truth. This shipped as part of the amendment below — ahead of the scale
   trigger that motivated it, because the #1 security fix needed one
   implementation to fix, not two.
2. **Collection sharding by tier.** Introduce placement so workspaces map onto
   multiple Weaviate clusters/shards by tier (e.g. free vs. paid, or by region
   for data-residency), keeping per-cluster collection counts bounded and
   isolating large tenants from the long tail.
3. **Revisit per-workspace-collection granularity** for very large fleets
   (e.g. collection-per-tier with workspace as a property/tenant dimension) if
   collection-count overhead dominates, guided by retrieval and indexing evals
   rather than speculation.

## Amendment (2026-08-04): injective base32 naming (issue #1)

**This ADR text was corrected on 2026-08-04.** The naming scheme it describes
shipped in code over a month earlier — commit `484480d` (PR #86) on
**2026-07-04**, released in `v0.5.0` (2026-07-13), CHANGELOG `⚠️ BREAKING
(data)`. The gap between "shipped" and "documented" is the drift #164 exists
to close.

### What was wrong with the original decision

The strip-everything-non-alphanumeric scheme this ADR originally accepted is
**not injective**: distinct raw ids that differ only in punctuation collapse
onto the same sanitized string, so they collapse onto the same Weaviate
collection/tenant name. Real workspace and user ids are slugs with separators
(`ws_local_001`, `local-dev-user`), so this was reachable, not theoretical —
`ws-123`, `ws_123`, and `ws123` all sanitize to `ws123` and would have shared
one Weaviate collection. That is a cross-tenant data leak: one tenant's
vectors become readable (and, on write, overwritable) by another. Filed and
fixed as issue **#1**.

### Current implementation

`services/inh-contracts/src/inh_contracts/naming.py` is the single
implementation both services import:

- `_encode_id` (`naming.py:28-36`) — base32 (RFC4648) encodes the raw id:
  `base64.b32encode(raw_id.encode("utf-8")).decode("ascii").rstrip("=")`. The
  output alphabet is `A-Z2-7`, a reversible bijection, so distinct ids can
  never produce the same name.
- `get_workspace_collection_name` (`naming.py:39-46`) — `Workspace_` prefix +
  `_encode_id(workspace_id)`.
- `get_user_tenant_name` (`naming.py:49-51`) — `User_` prefix +
  `_encode_id(user_id)`.

`inh-ingestion-svc/src/services/weaviate.py` and
`inh-public-api-svc/src/services/search.py` both import these from
`inh_contracts.naming` and re-export them under their existing names for
backward compatibility with existing call sites — there is exactly one
implementation, not two kept in lockstep by tests.

Base32 (not base64 or hex) was chosen because its output alphabet (`A-Z2-7`)
is a subset of what both a Weaviate *collection* name (must start with an
uppercase letter, then alphanumeric/underscore) and a Weaviate *tenant* name
allow — no further escaping is needed after prefixing.

### Current golden values (supersede the table above)

| Raw input        | Function                        | Expected output (golden)         |
|-------------------|----------------------------------|-----------------------------------|
| `ws_local_001`    | `get_workspace_collection_name` | `Workspace_O5ZV63DPMNQWYXZQGAYQ` |
| `ws-123`          | `get_workspace_collection_name` | `Workspace_O5ZS2MJSGM`           |
| `local-dev-user`  | `get_user_tenant_name`          | `User_NRXWGYLMFVSGK5RNOVZWK4Q`   |
| `user_001`        | `get_user_tenant_name`          | `User_OVZWK4S7GAYDC`             |

Re-derived from the live `inh_contracts.naming` implementation in this repo
and asserted, independently, by every layer below.

### The golden contract has four layers

Not the two duplicated copies this ADR originally accepted, and not the three
this Amendment first claimed — there are four:

- `services/inh-contracts/tests/test_naming.py` — the package's own suite:
  the golden values above, plus injectivity tests that assert the ids that
  used to collide under the old strip scheme (`ws-123` / `ws_123` / `ws123` /
  `w-s123` / `ws.123`) now produce distinct names.
- `services/inh-ingestion-svc/tests/test_naming_contract.py` — pins the same
  golden values independently, ingestion side.
- `services/inh-public-api-svc/tests/unit/test_naming_contract.py` — pins the
  same golden values independently, public-api side.
- `services/inh-public-api-svc/tests/unit/test_search_service.py` — asserts
  behavioral properties (prefixing, no collisions, charset validity) by
  calling the same shared function; it does not hardcode its own copy of the
  golden values.

Three of the four hardcode the literal golden strings independently
(`inh-contracts`, ingestion, public-api's dedicated contract test); if any one
of them, or the shared implementation itself, drifts from the others, CI fails
on the mismatch.

### Migration

**Breaking for existing deployments.** Because the encoding changed, every
existing Weaviate collection/tenant name computed under the old scheme is
wrong under the new one. Existing collections must be dropped and re-ingested
— see the `v0.5.0` CHANGELOG entry. PostgreSQL data (which does not derive
Weaviate names) is unaffected.

## Consequences

- Strong, easy-to-reason-about isolation now, with no premature complexity.
- Naming correctness no longer depends on two implementations staying in sync
  by convention — there is one function, imported by both services (shipped
  in code 2026-07-04; see
  [Amendment](#amendment-2026-08-04-injective-base32-naming-issue-1)). The
  original "duplicated logic guarded by golden tests" design this ADR
  accepted only ever protected against the two copies *diverging from each
  other*; it could not catch — and did not catch — both copies sharing the
  same non-injective encoding. #1 was a flaw in the shared logic, not a drift
  between copies.
- Cross-service correctness is still protected by golden naming contract
  tests, now anchored to the shared package rather than to two independently
  maintained copies; CI catches drift before it reaches production.
- The scaling path is staged: shared contracts landed early (as a security
  fix, not scale motivated), sharding remains future work, gated on scale data.
