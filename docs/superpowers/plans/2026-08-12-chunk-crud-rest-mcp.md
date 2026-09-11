# Implementation plan: Chunk CRUD across REST + MCP (#133)

**Issue:** [#133 — Full CRUD on chunks across REST + MCP](https://github.com/inherent-prime/inherent/issues/133)  
**Status:** Sprint 1–3 complete — #133 closable; Sprint 4 is cleanup follow-up  
**Depends on:** Ordering decision Option A (decided on issue); public-api auth + compensation patterns  
**Architecture choice:** **Sync CRUD in public-api** — not Temporal, not MQ, not Option C  
**Design authority:** Locked in [#133 comments](https://github.com/inherent-prime/inherent/issues/133) by `inherent-prime` (Option A + implementation design). Issue body “open decision” is superseded.

---

## Problem (plain English)

Agents can **read** chunks on REST and MCP, but cannot create, edit, or
delete a single chunk there. Update exists only on internal
`inh-ingestion-svc` (`PATCH /chunks` → `ChunkEditWorkflow`), which real
API keys never hit. Create and Delete do not exist at all.

#133 gives full chunk CRUD on both public surfaces, with PG + Weaviate
kept consistent via compensation — without changing document ingestion
or inventing a new store.

---

## Architecture (locked — do not reopen)

| Decision | Choice | Rejected |
| --- | --- | --- |
| Ordering | **A — append / hard-delete, gaps allowed** | B full re-index; C fractional `position` (defer until mid-insert is a real need) |
| Where writes run | **Synchronous in `inh-public-api-svc`** | Delegate to ingestion Temporal / MQ (not read-your-writes; hard to compensate) |
| Identity | Existing `chunk_index` as **stable ID** (not dense 0..N) | Sibling re-index on insert/delete |
| Dual store | PG truth → vector (Create/Update); vector → PG (Delete, same as `delete_document_everywhere`) | Best-effort / swallowed vector failures |
| Compensation | Chunk-scoped helper mirroring `compensation.py` (retry + loud exhaustion) | Bare `except: pass` |
| Surfaces | REST + MCP `_TOOLS` (write permission) | REST-only or MCP-only |
| Workflow | No new Temporal workflow; retire `ChunkEditWorkflow` **after** public Update ships | New workflow per chunk op |

This is an **API surface + write-boundary** change inside the existing
dual-store / dual-surface architecture. It does **not** replace
`DocumentIngestionWorkflow`, add a datastore, or redesign chunk ACL (#41).

### Consistency formulas (reuse exactly)

- `content_hash = hashlib.sha256(content.encode()).hexdigest()` (ingestion / #41)
- `token_count = estimate_tokens(content)` — `ceil(max(words*1.3, chars/4))`
- Vector UUID = `uuid5(NAMESPACE_DNS, "{workspace_id}:{user_id}:{document_id}:{chunk_index}")`
- Vector props mirror ingestion schema (`document_id`, `workspace_id`, `user_id`, `content`, `chunk_index`, `content_hash`, …)

### Consumer contract note

`chunk_index` may have gaps after Delete. Treat it as a stable reference,
not a contiguous reading order. Document this on REST + MCP refs when
writes ship.

---

## Contract

| Op | REST | MCP (perm) | Flow |
| --- | --- | --- | --- |
| Create | `POST /v1/chunks/{document_id}` body `{content}` → `DocumentChunk` | `create_chunk` (write) | PG insert (`chunk_index=max+1`, hash, tokens) → `embed_passage` → Weaviate upsert. Vector fail → compensate (delete PG row). |
| Update | `PATCH /v1/chunks/{document_id}/index/{chunk_index}` body `{content}` → `DocumentChunk` | `edit_chunk` (write) | PG update (recompute hash + tokens) → re-embed → Weaviate update **with new vector**. Vector fail → restore prior content or mark divergent loudly. |
| Delete | `DELETE /v1/chunks/{document_id}/index/{chunk_index}` → 204 | `delete_chunk` (write) | Weaviate delete by UUID first → PG delete. Vector fail → abort before PG. |
| Read | Unchanged | Optional `get_chunk` for REST parity | — |

**Auth:** `require_write_permission` + workspace ownership (parity with
`resolve_workspace_read` / user-scoped `X-Workspace-Id`). Cross-tenant → 404.

---

## Acceptance criteria (from #133)

- [x] Ordering model A pinned in schema/docs (`chunk_index` gaps allowed)
- [x] Create / Update / Delete on public-API REST **and** MCP `_TOOLS`
- [x] All writes compensated (no silent PG/vector divergence)
- [x] Failure parity pinned in `tests/contract/test_failure_parity.py`
- [x] Docs updated (REST ref, MCP ref); CHANGELOG `Added`

Out of scope for #133 close: mid-document insert (Option C); retiring
ingestion `PATCH` (Sprint 4 follow-up).

---

## Sprint plan

Tests first every sprint. DoD = tests green; docs/CHANGELOG when behavior
ships to callers.

### Sprint 1 — Foundation (data + compensation) — **DONE** (2026-08-12)

**Goal:** Prove Option A append/delete and chunk-scoped compensation with
no public HTTP/MCP surface yet.

| Task | Notes |
| --- | --- |
| TDD: append-create | `chunk_index = max(chunk_index)+1`; hash + tokens correct |
| TDD: hard-delete | Row gone; gaps tolerated; unique `(doc, chunk_index)` still holds |
| Chunk compensation helper | Same retry + CRITICAL/metric exhaustion pattern as `mark_document_failed_with_retry` |
| TDD: vector-fail rollback | Create path: PG insert rolled back when vector write fails |

**Shipped:**
- `src/services/chunk_math.py` — `compute_chunk_content_hash`, `estimate_tokens` (ingestion parity)
- `DatabaseService.append_document_chunk` / `delete_document_chunk` / `get_document_chunk_by_index`
- `compensation.delete_chunk_with_retry` (operation e.g. `chunk_create_vector_rollback`)
- Tests: `test_chunk_math`, `test_append_chunk_db`, `test_delete_chunk_db`, `test_chunk_compensation` (26 passed with existing compensation suite)

**Done when:** Unit tests green for append, delete-with-gaps, compensation
rollback/exhaustion. No REST/MCP yet. ✅

---

### Sprint 2 — Public-API REST writes — **DONE** (2026-08-12)

**Goal:** Ship Create / Update / Delete on REST with real Weaviate
upsert/re-embed/delete.

| Task | Notes |
| --- | --- |
| Chunk vector write path | Mirror `delete_document_vectors` (raw HTTP + deterministic UUID + `embed_passage`) |
| `POST /v1/chunks/{document_id}` | Append-only Create |
| `PATCH /v1/chunks/{document_id}/index/{chunk_index}` | Update with **new vector** (do not leave content-only / stale embedding) |
| `DELETE /v1/chunks/{document_id}/index/{chunk_index}` | Vector-first then PG |
| Auth + not-found | Write perm; workspace ownership; 404 cross-tenant |
| Contract/unit tests | Happy path + DB down / vector down / not-found / permission |

**Shipped:**
- `SearchService.upsert_chunk_vector` / `delete_chunk_vector` + `chunk_vector_uuid`
- `DatabaseService.update_document_chunk`
- `chunk_writes.py` — `create_chunk_everywhere` / `update_chunk_everywhere` / `delete_chunk_everywhere`
- `compensation.restore_chunk_content_with_retry`
- REST POST / PATCH / DELETE on `api/v1/chunks.py`
- Tests: `test_update_chunk_db`, `test_chunk_vectors`, `test_chunk_writes`, `test_chunk_write_endpoints` (29 passed)

**Done when:** REST CRUD green under unit/contract tests. Agents can use
REST; MCP still read-only for chunks. ✅

---

### Sprint 3 — MCP parity + docs (closes #133) — **DONE** (2026-08-12)

**Goal:** Mirror REST on MCP; pin failure parity; publish the contract.

| Task | Notes |
| --- | --- |
| MCP `_TOOLS` | One registry entry each: `create_chunk`, `edit_chunk`, `delete_chunk` (write). Optional `get_chunk` if cheap |
| Failure parity | Pin create/update/delete pairs in `test_failure_parity.py` (vector/DB down, not-found, permission) |
| Docs | `docs/reference/rest-api.md`, `docs/reference/mcp-tools.md` — including gap-tolerant `chunk_index` note |
| CHANGELOG | `Added` under `[Unreleased]` with `(#133)` |

**Shipped:**
- MCP handlers + `_TOOLS` entries (`create_chunk`, `edit_chunk`, `delete_chunk`)
- Contract maps updated (`test_mcp_contract`, `test_mcp_http_transport` — HTTP 13 tools)
- Failure parity: `TestChunkCreate/Update/DeleteVectorDownParity`
- Docs + CHANGELOG `Added` (#133)

**Done when:** Acceptance checklist above is complete. **Close #133.** ✅

---

### Sprint 4 — Cleanup (follow-up; not required to close #133)

**Goal:** Remove the duplicate internal write path.

| Task | Notes |
| --- | --- |
| Deprecate ingestion `PATCH /chunks/...` | Point callers at public-API |
| Retire `ChunkEditWorkflow` + activities | After public Update is the only write path |
| Close/supersede tracking | Fold leftover #134/#137 concerns into "superseded by public-api CRUD" if still open |

**Done when:** Single chunk-write path remains (public-api). File as a
separate issue if #133 should merge first.

---

## Test matrix (minimum)

| Layer | Cases |
| --- | --- |
| Data | Append index; delete leaves gaps; hash/tokens; concurrent append uniqueness |
| Compensation | Vector fail after PG create → row gone; Update fail → prior content or loud divergence; Delete vector fail → PG untouched |
| REST | 201/200/204; 404 cross-tenant; 403 missing write; 5xx when stores diverge after compensation attempt |
| MCP | Tools registered; write perm; same outcomes as REST |
| Parity | `test_failure_parity.py` create/update/delete |

---

## Explicit non-goals

- Mid-insert / reordering (Option C)
- Full sibling re-index (Option B)
- New Temporal workflow for single-chunk ops
- Changing bulk ingestion chunking
- Chunk-level ACL redesign (separate from #41)

---

## Proof of complete (per Definition of Done)

1. Sprint tests green (unit → contract → parity as each sprint lands).
2. Docs + CHANGELOG updated when Sprint 3 ships.
3. Pattern sweep: no new swallowed dual-store failures; both REST and MCP
   leave the same document/chunk state on failure.
4. Issue #133 closed after Sprint 3; Sprint 4 tracked separately if needed.
