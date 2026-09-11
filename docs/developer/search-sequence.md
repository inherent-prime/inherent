---
search:
  exclude: true
---

# Search — sequence diagrams

Traces the full search path at micro level. All file references are relative to
`services/inh-public-api-svc/src/`.

Four diagrams:

1. [End-to-end REST flow](#1-end-to-end-rest-flow--post-v1search) — `POST /v1/search`
2. [Micro level inside `SearchService.search()`](#2-micro-level--inside-searchservicesearch)
3. [MCP surface](#3-mcp-surface--search_documents--search_memory-tools) — `search_documents` / `search_memory`
4. [Storage roles — upload to query](#4-storage-roles--weaviate-vs-postgres-upload-to-query) — how Weaviate and Postgres split the work

## 1. End-to-end REST flow — `POST /v1/search`

```mermaid
sequenceDiagram
    autonumber
    participant C as Agent / Client
    participant MW as Middleware stack<br/>(CORS → SecHeaders → RequestCtx → Auth → Audit → RateLimiting)
    participant EP as search_documents()<br/>api/v1/search.py
    participant AU as Auth deps<br/>services/auth.py
    participant PG as Postgres<br/>DatabaseService
    participant SS as SearchService<br/>services/search.py
    participant QG as quality_gate.evaluate()
    participant CW as ContextWindowBuilder
    participant BG as BackgroundTasks

    C->>MW: POST /v1/search {query, limit, min_score, document_ids,<br/>include_context, context_window, search_mode, alpha}<br/>Headers: X-API-Key, X-Workspace-Id?, X-Source?
    MW->>EP: request passes CORS → SecHeaders → RequestCtx → Auth → Audit → RateLimiting

    Note over EP,AU: FastAPI dependency resolution (before handler body)
    EP->>AU: get_api_key_info(X-API-Key or Authorization: Bearer)
    AU->>PG: require_api_key(key) — validate, load APIKeyInfo
    AU->>AU: get_search_permission — 403 if key lacks "search"
    AU->>AU: resolve_workspace_search → _resolve_workspace()
    alt workspace-scoped key
        AU-->>EP: workspace_id = key binding<br/>(mismatching X-Workspace-Id header → 403)
    else user-scoped key + header
        AU->>PG: get_user_workspace_ids(user_id)
        AU-->>EP: header id if owned, else 403
    else no header
        AU->>PG: get_user_workspace_ids(user_id)
        AU-->>EP: single workspace → use it, multiple → workspace_id = None
    end

    alt Single workspace (workspace_id resolved)
        EP->>SS: search(workspace_id, user_id, request)
        Note over SS: see Diagram 2 — embed, GraphQL, score, filter
        SS-->>EP: SearchResponse{results, processing_time_ms}
    else Multi-workspace (workspace_id = None)
        EP->>PG: get_user_workspace_ids(user_id)
        opt no workspaces
            EP-->>C: empty SearchResponse + quality_verdict<br/>verdict=insufficient_evidence, reason_code=no_results
        end
        EP->>SS: embed_query_vector(request) once<br/>(asyncio.to_thread, skipped for keyword mode)
        par asyncio.gather, bounded by Semaphore(search_max_workspace_concurrency)
            EP->>SS: search(ws_1, user_id, request, query_vector)
            EP->>SS: search(ws_N, user_id, request, query_vector)
        end
        Note over EP: per-workspace exception → log warning,<br/>contribute [] (partial-result policy #13)
        EP->>EP: merge, sort by (-score, chunk_id, document_id),<br/>truncate to limit, wall-clock ms around gather
    end

    Note over EP,QG: Adaptive quality gate + ONE bounded fallback (#43)
    EP->>QG: evaluate(results, request)
    QG-->>EP: verdict: sufficient | low_confidence | insufficient_evidence
    opt verdict ≠ sufficient and a fallback applies
        alt low_confidence (and mode ≠ keyword)
            EP->>SS: retry with search_mode="keyword" (keyword_retry)
        else insufficient_evidence
            EP->>SS: retry with min_score=0, limit×2 capped at 100 (broadened_query)
        end
        EP->>QG: re-evaluate retry results (final verdict — never loops)
        EP->>EP: replace results, performed_fallback=true,<br/>fallback_strategy set, processing_time_ms += retry ms
    end

    opt include_context=true (single-workspace-scoped request only)
        EP->>CW: expand(matches, workspace_id, user_id, k=context_window)
        CW->>CW: _compute_ranges: per match [idx−k, idx+k],<br/>merge overlapping/adjacent per document
        CW->>PG: get_context_chunks(workspace_id, user_id, ranges)<br/>— ONE batched query, user-scoped (#41)
        CW->>CW: _assign_windows → context_before / context_after
        Note over CW: fetch failure is logged and swallowed (best-effort)
    end
    EP->>EP: total_tokens = Σ token_count over matches + neighbours
    EP->>EP: Prometheus counters (best-effort, never fails request)

    EP->>BG: schedule publish_audit_event<br/>(snippets, chunk_ids, risk counts, verdict, fallback)
    opt single-workspace scope and eval capture enabled for workspace
        EP->>PG: await capture_search_event(transport="rest", …)<br/>— SAME shared helper MCP's search_documents / search_memory<br/>call for their own single-workspace search (#241)
        alt row is durable
            EP->>EP: response.event_id = event_id
        else capture failed
            Note over EP: no event_id — never a dangling one (#240)
        end
        EP->>BG: schedule purge_expired_events (retention, write-behind)
        Note over EP,BG: multi-workspace search never sets event_id
    end
    EP-->>C: 200 SearchResponse{query, results, total_results, processing_time_ms,<br/>search_mode, quality_verdict, performed_fallback, fallback_strategy,<br/>total_tokens, event_id}
    Note over BG: background tasks run AFTER the response is sent<br/>(fire-and-forget, can never slow or fail the search)
    Note over EP,PG: capture is the exception: it is awaited, because the<br/>response hands the caller an event_id and an id that<br/>does not resolve is worse than no id (#240)
```

## 2. Micro level — inside `SearchService.search()`

```mermaid
sequenceDiagram
    autonumber
    participant SS as SearchService
    participant EM as embedder.embed_query()<br/>(LRU cache 1024)
    participant TEI as TEI sidecar<br/>(BAAI/bge-small-en-v1.5, 384-dim)
    participant WV as Weaviate

    Note over SS: search() → _search_weaviate() → _apply_advanced_methods() (no-op)

    alt semantic / hybrid mode, no precomputed vector
        SS->>EM: asyncio.to_thread(embed_query_vector) — never blocks event loop (#19)
        alt cache hit
            EM-->>SS: cached 384-dim tuple
        else cache miss
            EM->>TEI: POST /embed {inputs: [query]}
            TEI-->>EM: [[f32 × 384]]
            EM-->>SS: tuple(float × 384), cached
        end
    else keyword mode
        Note over SS: no vector needed — embedding skipped entirely
    end

    SS->>SS: collection = Workspace_<base32(workspace_id)>,<br/>tenant = User_<base32(user_id)> (shared inh_contracts naming)
    SS->>SS: _require_safe_name() on both — explicit raise,<br/>GraphQL-injection guard that survives python -O (#33)

    SS->>SS: _build_graphql():<br/>• escape \ and " in query<br/>• fetch_limit = min(100, limit×3) if min_score>0 else limit (over-fetch #31)<br/>• where: document_id ContainsAny [ids] if document_ids set
    alt keyword
        SS->>SS: bm25: {query}
    else hybrid
        SS->>SS: hybrid: {query, vector[384], alpha}  (1.0=vector, 0.0=keyword)
    else semantic
        SS->>SS: nearVector: {vector[384]}
    end

    SS->>WV: POST /v1/graphql  Get { <Collection>(…, tenant: "<tenant>") {<br/>document_id, original_filename, content, chunk_index, start_char, end_char,<br/>content_hash, source_uri, ingested_at, content_risk, content_risk_reasons,<br/>_additional {id score certainty distance}}}
    alt HTTP 422
        WV-->>SS: class/tenant not created yet (ingest→search race)
        SS-->>SS: return [] — empty, not 500
    else 200 with GraphQL errors
        WV-->>SS: errors[]
        alt "Cannot query field <Collection>" or "tenant not found"
            SS-->>SS: return [] (nothing indexed yet)
        else any other error
            SS-->>SS: raise httpx.HTTPError (no silent fallback)
        end
    else 200 OK
        WV-->>SS: chunks[]
    end

    loop per chunk
        SS->>SS: score resolution:<br/>_additional.score (bm25/hybrid) → certainty →<br/>max(0, 1 − distance/2) → 0.0
        SS->>SS: score provenance: score_source = bm25 | hybrid | vector,<br/>echo bm25_score / vector_similarity / alpha
        SS->>SS: drop chunk if score < min_score (client-side filter)
        SS->>SS: metadata passthrough (non-core fields kept)
        SS->>SS: freshness (#42): parse ingested_at →<br/>is_stale if older than freshness_max_age_days (flagged, never dropped)
        SS->>SS: poisoning risk (#44): content_risk ("none"→None) + reasons<br/>(flagged, never dropped)
        SS->>SS: build Citation (#39) from the chunk's own fields<br/>(chunk_id, doc, content, start/end_char, score, source_uri, staleness)
    end
    SS->>SS: truncate to request.limit (undo the over-fetch)
    SS-->>SS: SearchResponse{results, total_results, processing_time_ms, search_mode}
```

## 3. MCP surface — `search_documents` / `search_memory` tools

```mermaid
sequenceDiagram
    autonumber
    participant A as MCP client (agent)
    participant T as _run_search() / _handle_search()<br/>mcp_server/server.py
    participant SS as SearchService
    participant PG as Postgres
    participant CAP as eval_capture.capture_search_event()

    A->>T: call tool {query, workspace_id?, limit, min_score,<br/>search_mode, alpha, …} (api_key via transport)
    T->>T: _run_search(): empty query → "Error: Query is required"
    alt workspace_id given
        T->>T: validate against user's authorised set
    else none
        T->>PG: get_user_workspace_ids(user_id) → all workspaces
    end
    T->>T: build_search_request(arguments) — SAME helper as REST (#14),<br/>transport keys (api_key, workspace_id) stripped, Pydantic validates
    loop each workspace (sequential)
        T->>SS: search(workspace_id, user_id, request)
        Note over SS: identical internals — Diagram 2
        SS-->>T: results, tagged with workspace_id
        opt caller requested capture, exactly one workspace resolved, and eval capture enabled
            Note over T: capture is opt-in at this call site (#241 review) --<br/>only _handle_search (search_documents/search_memory) passes<br/>capture=True; get_citations shares this retrieval but never sets it,<br/>so it mints no orphan event
            T->>CAP: capture_search_event(transport="mcp", workspace_id,<br/>user_id, request, response)
            Note over CAP,PG: SAME shared helper REST calls (Diagram 1, #241) —<br/>mints event_id, awaits the INSERT, stamps response.event_id<br/>only when durable; never a dangling id (#240)
            CAP->>PG: await record_query_event (INSERT eval_query_events,<br/>transport='mcp')
            CAP->>PG: await purge_expired_events (retention) — awaited here,<br/>MCP has no BackgroundTasks queue to defer it onto
        end
    end
    T->>T: sort by (-score, chunk_id, document_id), truncate to limit (#28)
    T-->>A: markdown summary + structured JSON<br/>{workspace_id, chunk_id, document_id, document_name, content, score,<br/>score_source, is_stale, source_uri, content_hash} plus<br/>{query, results, workspaces_searched, event_id} at the top level
    Note over T: event_id is null for a multi-workspace search,<br/>disabled capture, or a failed write — same contract as REST
```

## Behavioural invariants

- **Single-shot fallback**: the quality-gate retry (`api/v1/search.py`) is
  bounded to one attempt by construction — the retry's verdict is recorded but
  never triggers another fallback, and a fallback exception is swallowed so it
  can never fail the original request.
- **Context expansion only for single-workspace-scoped requests**: expanding a
  match against a workspace that isn't its own risks a cross-tenant neighbour
  read (#30), so `include_context` is honoured only when the request resolves
  to a single `workspace_id` (workspace-scoped key, `X-Workspace-Id`, or sole
  owned workspace). Multi-workspace fan-out (`workspace_id is None`) skips it.
- **Three-layer tenant isolation**: workspace collection + user tenant on every
  Weaviate query, `_require_safe_name` against GraphQL injection, and the
  fan-out only over `get_user_workspace_ids` — a fallback retry can never widen
  workspace scope.
- **MCP vs REST**: MCP searches workspaces sequentially (no semaphore/gather)
  and has no quality gate/fallback and no context-window expansion — those are
  REST-endpoint features layered above `SearchService`. Eval capture is the
  exception: both transports share it (below). Because MCP never runs the
  quality gate, **every `transport='mcp'` captured row has `quality_verdict =
  NULL`** and never reflects a fallback substitution — REST's `quality_verdict`
  is populated by the gate that runs before its own capture. Record *shape*
  cannot drift between the transports (both write through the identical
  `capture_search_event` call), but record *inputs* already differ at birth;
  this is a stated, deliberate limitation (#241 review), not a bug — see
  `test_mcp_capture_has_null_quality_verdict_that_rest_never_has` in
  `tests/unit/test_mcp_search_capture.py`. Anyone segmenting eval analytics by
  `quality_verdict` must account for it.
- **Eval capture is single-workspace only, opt-in at the call site, and
  shared by both transports (#241, #241 review finding 1)**: `event_id` /
  `eval_capture.capture_search_event()` run only when the request resolved a
  single `workspace_id` AND the caller asked to capture — REST's
  `search_documents` route always asks, and on MCP only `_handle_search`
  (`search_documents` / `search_memory`) passes `capture=True` to
  `_run_search`; `get_citations` shares the same retrieval but leaves capture
  at its default (`False`), since it is a citation *view*, not a user-facing
  search an agent can attach feedback to, and minting an event it never
  surfaces would just double-count the query. REST and MCP both call the ONE
  `capture_search_event` helper rather than each minting the event
  independently, so a field added to capture later cannot land on one
  transport and miss the other. A multi-workspace search never sets
  `event_id` on either transport. The captured row's `transport` column
  (`'rest'` | `'mcp'`) records which surface produced it.
- **Nothing after retrieval slows the response**: audit publishing, eval
  capture, and metrics are background/best-effort; a cold DB or down MQ never
  affects the serving path.

## 4. Storage roles — Weaviate vs Postgres (upload to query)

Dual-store architecture: **Weaviate is the search index, Postgres is the
system of record**. A single search touches both, at different moments, for
different jobs. Ingestion file references are relative to
`services/inh-ingestion-svc/src/`.

### Ingestion — one document, two stores

The live path is the Temporal workflow
(`temporal/workflows/document_ingestion.py`). After extraction and chunking,
staged chunks are written to both stores in parallel activities.

```mermaid
sequenceDiagram
    autonumber
    participant U as User / Agent
    participant WF as Temporal workflow<br/>document_ingestion.py
    participant ST as Staging
    participant PG as Postgres
    participant TEI as TEI sidecar<br/>(BAAI/bge-small-en-v1.5, 384-dim)
    participant WV as Weaviate

    U->>WF: upload document (via public API → MQ)
    WF->>WF: fetch → extract_text → chunk_text
    WF->>ST: stage chunk dicts (content, chunk_index,<br/>start/end_char, token_count, content_risk)

    par store_in_postgresql (temporal/activities/store.py)
        WF->>PG: processed_documents row (owner user_id, workspace_id,<br/>filename, status) + document_chunks rows<br/>(FK, chunk_index, content, token_count,<br/>start/end_char, content_hash, source_uri, ingested_at)
        Note over PG: unique (processed_document_id, chunk_index) —<br/>chunk ORDER is a relational fact
    and store_in_weaviate (temporal/activities/store.py)
        WF->>TEI: embed_texts(all chunk texts) — ONE batch,<br/>asyncio.to_thread (#19)
        TEI-->>WF: 384-dim vector per chunk
        WF->>WV: ensure collection Workspace_<base32(workspace_id)><br/>+ tenant User_<base32(user_id)>, then insert objects<br/>{vector + text + metadata} (Vectorizer.none —<br/>vectors always computed client-side)
        Note over WV: text is also BM25-indexed →<br/>serves keyword and hybrid modes too
        Note over WF,WV: Weaviate down → activity raises,<br/>Temporal retries, doc marked failed —<br/>never silently half-indexed
    end
```

After upload the same chunk exists twice: in Weaviate as
*(vector + text + metadata)* for retrieval, in Postgres as an ordered row for
everything else.

### Query — how the stores alternate

1. **Postgres — authorization first.** API-key validation and
   `get_user_workspace_ids(user_id)` decide which workspaces the fan-out may
   touch. Weaviate is never queried for a workspace Postgres didn't authorize.
2. **Embedding provider — query becomes a vector.** Same provider/model as
   ingestion (`EMBEDDING_PROVIDER`/`EMBEDDING_MODEL_ID`, default `tei` /
   `BAAI/bge-small-en-v1.5`, 384-dim — see
   [Embedding provider & model-identity guard](../reference/configuration.md#embedding-provider-model-identity-guard)),
   so query↔chunk cosine comparison is meaningful. Before the vector query
   runs, the target collection's persisted embedding identity is checked
   against the active provider's — a mismatch is a hard error, not a warning
   (#311), so a misconfigured model can't silently return noise instead of
   real matches. Keyword mode skips both the embed call and the identity
   check entirely.
3. **Weaviate — the actual search.** `nearVector` / `hybrid` / `bm25` per
   workspace, scoped to collection + tenant. Ranking and scoring is purely
   Weaviate — Postgres plays no part.
4. **Postgres — context expansion.** With `include_context=true` on a
   single-workspace-scoped request, `ContextWindowBuilder` fetches the chunks
   *around* each match (`[idx−k, idx+k]`) from `document_chunks` in one batched
   range query — trivial in SQL, awkward in a vector store; this is why the dual
   store exists. The join to `processed_documents.user_id` stops a shared
   workspace leaking another user's neighbour chunks (#41). The rows'
   `token_count` powers `total_tokens`, so an agent knows the context-budget
   cost up front.
5. **Postgres — after the response.** Eval-capture events are written in
   background tasks on single-workspace searches only, never on the serving
   path.

### Division of labour

| Concern | Store |
|---|---|
| Who are you, which workspaces you may search | Postgres |
| Which chunks match the query (rank + score) | Weaviate |
| What surrounds a match (context windows, chunk order) | Postgres |
| Token accounting (`total_tokens`) | Postgres (`token_count`) |
| Document delete | Both — vectors deleted first, then the DB row, so orphaned vectors never survive a "deleted" document (#87) |

Failure-mode consequence of the split: Postgres may hold a document's rows
before its vectors land in Weaviate (the ingest→search race) — the document
only becomes findable once Weaviate has it; until then search returns empty,
not an error.
