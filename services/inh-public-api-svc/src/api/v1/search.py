"""Search endpoint."""

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Header

from src.config import settings
from src.models.search import (
    QualityVerdict,
    SearchRequest,
    SearchResponse,
    SearchResult,
)
from src.services.audit_publisher import (
    build_audit_event,
    count_results_by_risk,
    publish_audit_event,
)
from src.services.auth import ResolvedAuth, resolve_workspace_search
from src.services.database import get_database
from src.services.eval_capture import capture_enabled, new_event_id, record_query_event
from src.services.quality_gate import evaluate as evaluate_quality
from src.services.search import SearchService, get_search_service
from src.utils import get_logger

router = APIRouter()
logger = get_logger(__name__)


def _build_result_snippets(results: list[SearchResult]) -> list[dict]:
    """Extract top-5 result snippets for the audit event."""
    snippets = []
    for r in results[:5]:
        snippets.append(
            {
                "document_id": r.document_id,
                "filename": r.document_name,
                "snippet": r.content,
                "score": r.score,
                "chunk_index": r.metadata.get("chunk_index") if r.metadata else None,
            }
        )
    return snippets


def _compute_total_tokens(results: list[SearchResult]) -> int:
    """Sum token_count across all matched chunks and their context neighbours."""
    total = 0
    missing = 0
    for r in results:
        tc = (r.metadata or {}).get("token_count")
        if isinstance(tc, int):
            total += tc
        else:
            missing += 1
        for ctx in list(r.context_before or []) + list(r.context_after or []):
            if ctx.token_count:
                total += ctx.token_count
            else:
                missing += 1
    if missing:
        try:
            from src.services.metrics import record_search_chunks_missing_token_count

            record_search_chunks_missing_token_count(missing)
        except Exception as exc:
            logger.debug(
                "search_metric_record_failed", metric="missing_token_count", error=str(exc)
            )
    return total


def _record_search_metrics(request: SearchRequest, workspace_id: str | None) -> None:
    """Increment Prometheus counters for the search request. Best-effort."""
    try:
        from src.services.metrics import record_search_context_request, record_search_request

        record_search_request(mode=request.search_mode)
        if request.include_context:
            record_search_context_request(k=request.context_window)
    except Exception as exc:
        logger.debug("search_metric_record_failed", metric="search_request", error=str(exc))


def _schedule_audit(
    background_tasks: BackgroundTasks,
    *,
    auth: ResolvedAuth,
    request: SearchRequest,
    response: SearchResponse,
    source: str,
    workspace_id: str,
) -> None:
    """Build and schedule an audit event for fire-and-forget publishing."""
    event = build_audit_event(
        workspace_id=workspace_id,
        user_id=auth.key_info.user_id,
        api_key_id=auth.key_info.key_id,
        source=source,
        query_type="search",
        query_text=request.query,
        query_filters={"document_ids": request.document_ids} if request.document_ids else None,
        result_count=response.total_results,
        result_snippets=_build_result_snippets(response.results),
        # Provenance (#41): record the chunk_ids actually returned.
        returned_chunk_ids=[r.chunk_id for r in response.results if r.chunk_id],
        response_time_ms=response.processing_time_ms,
        # PM-S018 / PM-S019 — search mode & context metadata
        search_mode=request.search_mode,
        include_context=request.include_context,
        context_window=request.context_window,
        alpha=request.alpha,
        # RAG-poisoning visibility (#44): counts of returned chunks by risk level.
        risk_counts=count_results_by_risk(response.results),
    )
    # Adaptive retrieval quality gate (#43): record verdict + any fallback so the
    # audit trail shows when retrieval was weak / a fallback ran.
    if response.quality_verdict is not None:
        event["quality_verdict"] = response.quality_verdict.model_dump()
    event["performed_fallback"] = response.performed_fallback
    event["fallback_strategy"] = response.fallback_strategy
    background_tasks.add_task(publish_audit_event, event)


async def _expand_context_and_total_tokens(
    response: SearchResponse,
    request: SearchRequest,
    ctx_workspace_id: str,
    user_id: str,
) -> None:
    """Expand context windows (if requested) and set response.total_tokens.

    Mutates *response* in-place.  Best-effort: if the context fetch fails the
    error is swallowed inside ContextWindowBuilder.expand(); total_tokens is
    still computed from whatever data is available.

    Cross-tenant safety (#41): ``user_id`` is threaded into the context fetch so
    neighbour chunks are scoped to the requesting user, not just the workspace.
    """
    if request.include_context and response.results and ctx_workspace_id:
        from src.services.context_window import ContextWindowBuilder

        database = await get_database()
        builder = ContextWindowBuilder(database)
        await builder.expand(
            matches=response.results,
            workspace_id=ctx_workspace_id,
            user_id=user_id,
            k=request.context_window,
        )
    response.total_tokens = _compute_total_tokens(response.results)


async def _search_workspaces_concurrently(
    search_service: SearchService,
    *,
    user_id: str,
    workspace_ids: list[str],
    request: SearchRequest,
) -> tuple[list[SearchResult], float]:
    """Fan out a search across the user's authorised workspaces (#13).

    Behaviour:
    - The query embedding is computed ONCE and reused across every workspace,
      instead of re-embedding per workspace.
    - Workspaces are searched concurrently with ``asyncio.gather`` bounded by an
      ``asyncio.Semaphore`` sized from ``search_max_workspace_concurrency`` so a
      user with many workspaces cannot exhaust the Weaviate connection pool.
    - Per-workspace failure isolation (partial-result policy): if one workspace
      search raises, the error is logged and that workspace contributes zero
      results; the remaining workspaces still return. The request only fails if
      every workspace fails in a way that is surfaced here — single failures
      degrade to partial results rather than a 5xx.

    Returns the merged (unsorted, unlimited) results and the parallel wall-clock
    time in milliseconds (measured around the gather, NOT a sum of per-workspace
    times).
    """
    # Embed once, reuse across all workspaces (#13). Offload the blocking TEI
    # call to a thread so it doesn't stall the event loop (#19).
    query_vector = await asyncio.to_thread(search_service.embed_query_vector, request)

    semaphore = asyncio.Semaphore(settings.search_max_workspace_concurrency)

    async def _search_one(ws_id: str) -> list[SearchResult]:
        async with semaphore:
            try:
                resp = await search_service.search(
                    workspace_id=ws_id,
                    user_id=user_id,
                    request=request,
                    query_vector=query_vector,
                )
                return resp.results
            except Exception as exc:  # noqa: BLE001 — partial-result isolation
                logger.warning(
                    "multi_workspace_search_partial_failure",
                    workspace_id=ws_id,
                    error=str(exc),
                )
                return []

    start = time.time()
    per_workspace_results = await asyncio.gather(*(_search_one(ws_id) for ws_id in workspace_ids))
    wall_clock_ms = (time.time() - start) * 1000

    merged: list[SearchResult] = []
    for ws_results in per_workspace_results:
        merged.extend(ws_results)
    return merged, wall_clock_ms


# A retrieval callable: given a (possibly modified) request, return the matched
# results plus the processing time in ms. Both the single-workspace and the
# multi-workspace paths provide one of these so the quality gate + fallback
# logic is shared and path-agnostic (#43).
RetrieveFn = Callable[[SearchRequest], Awaitable[tuple[list[SearchResult], float]]]


def _build_fallback_request(
    request: SearchRequest, verdict: QualityVerdict
) -> SearchRequest | None:
    """Derive ONE bounded fallback request from a non-sufficient verdict (#43).

    Strategy:
    - low_confidence        → retry in keyword (BM25) mode. A weak top score in
                              semantic/hybrid mode often means a lexical match is
                              the better signal for this query.
    - insufficient_evidence → broaden the query: drop ``min_score`` to 0 and
                              widen ``limit`` so more candidates can surface.

    Returns the modified request, or ``None`` when no meaningful fallback applies
    (e.g. low_confidence already in keyword mode, or a broaden that changes
    nothing) so the caller skips the retry. The retry is always a SINGLE attempt;
    its result is never re-fed into the fallback (the caller enforces this).
    """
    if verdict.verdict == "low_confidence":
        if request.search_mode == "keyword":
            return None  # already keyword; a keyword retry would be identical
        return request.model_copy(update={"search_mode": "keyword"})

    if verdict.verdict == "insufficient_evidence":
        broadened_limit = min(max(request.limit * 2, request.limit), 100)
        if request.min_score <= 0.0 and broadened_limit == request.limit:
            return None  # nothing left to broaden
        return request.model_copy(update={"min_score": 0.0, "limit": broadened_limit})

    return None


def _fallback_strategy_name(request: SearchRequest, verdict: QualityVerdict) -> str:
    """Human-readable label for the fallback that ran, for response + audit."""
    if verdict.verdict == "low_confidence":
        return "keyword_retry"
    return "broadened_query"


async def _apply_quality_gate_and_fallback(
    response: SearchResponse,
    request: SearchRequest,
    retrieve: RetrieveFn,
) -> None:
    """Evaluate retrieval quality and, if needed, do ONE bounded fallback (#43).

    Mutates *response* in place: sets ``quality_verdict`` and, when a fallback
    runs, ``performed_fallback`` / ``fallback_strategy`` and replaces
    ``results`` / ``total_results`` / ``processing_time_ms`` with the retry's.

    Loop-safety: the fallback is attempted at most ONCE. The retry's verdict is
    recomputed and attached, but it never triggers another fallback — there is
    no recursion and no loop here, by construction.
    """
    verdict = evaluate_quality(response.results, request)
    response.quality_verdict = verdict

    if verdict.verdict == "sufficient":
        return

    fallback_request = _build_fallback_request(request, verdict)
    if fallback_request is None:
        return

    strategy = _fallback_strategy_name(request, verdict)
    try:
        fb_results, fb_ms = await retrieve(fallback_request)
    except Exception as exc:  # noqa: BLE001 — fallback must never fail the request
        logger.warning("quality_gate_fallback_failed", strategy=strategy, error=str(exc))
        return

    response.results = fb_results
    response.total_results = len(fb_results)
    response.processing_time_ms = round(response.processing_time_ms + fb_ms, 2)
    response.performed_fallback = True
    response.fallback_strategy = strategy
    # Re-evaluate on the post-fallback results. This is the FINAL verdict; it is
    # NOT used to trigger another fallback (single bounded retry, see docstring).
    response.quality_verdict = evaluate_quality(fb_results, fallback_request)


@router.post("/search", response_model=SearchResponse)
async def search_documents(
    request: SearchRequest,
    auth: Annotated[ResolvedAuth, Depends(resolve_workspace_search)],
    search_service: Annotated[SearchService, Depends(get_search_service)],
    background_tasks: BackgroundTasks,
    x_source: Annotated[str | None, Header(alias="X-Source")] = None,
) -> SearchResponse:
    """
    Perform semantic search across documents in the workspace.

    Requires an API key with 'search' permission.
    Workspace can be specified via ``X-Workspace-Id`` header.
    If the user has multiple workspaces and no header is provided,
    searches across all accessible workspaces.
    """
    # Determine audit source from header
    valid_sources = {"dashboard", "chat"}
    source = x_source if x_source in valid_sources else "api_key"

    workspace_id = auth.workspace_id

    if workspace_id:
        response = await search_service.search(
            workspace_id=workspace_id,
            user_id=auth.key_info.user_id,
            request=request,
        )

        # Adaptive retrieval quality gate + single bounded fallback (#43).
        async def _retrieve_single(req: SearchRequest) -> tuple[list[SearchResult], float]:
            resp = await search_service.search(
                workspace_id=workspace_id,
                user_id=auth.key_info.user_id,
                request=req,
            )
            return resp.results, resp.processing_time_ms

        await _apply_quality_gate_and_fallback(response, request, _retrieve_single)

        # PM-S019: expand context windows and compute total_tokens
        await _expand_context_and_total_tokens(
            response, request, workspace_id, auth.key_info.user_id
        )
        _record_search_metrics(request, workspace_id)
        _schedule_audit(
            background_tasks,
            auth=auth,
            request=request,
            response=response,
            source=source,
            workspace_id=workspace_id,
        )

        # Evals v1: mint the capture event id and schedule the write-behind.
        # Fire-and-forget like audit; the response carries event_id so agents
        # can report feedback (POST /v1/evals/feedback) on exactly this search.
        # No awaits here: the task resolves the database itself, so capture can
        # never fail or slow the serving path (not even on a cold DB init).
        if capture_enabled(workspace_id):
            event_id = new_event_id()
            response.event_id = event_id
            background_tasks.add_task(
                record_query_event,
                event_id=event_id,
                workspace_id=workspace_id,
                user_id=auth.key_info.user_id,
                request=request,
                response=response,
            )
        return response

    # Multi-workspace: search across all user workspaces, merge results
    database = await get_database()
    user_workspaces = await database.get_user_workspace_ids(auth.key_info.user_id)
    if not user_workspaces:
        response = SearchResponse(
            results=[],
            query=request.query,
            total_results=0,
            processing_time_ms=0.0,
            search_mode=request.search_mode,
        )
        # Quality gate (#43): no workspaces means no evidence; record the verdict
        # but there is nothing to fall back to.
        response.quality_verdict = evaluate_quality(response.results, request)
        # PM-S019: no results, total_tokens stays 0; context expansion skipped
        await _expand_context_and_total_tokens(response, request, "", auth.key_info.user_id)
        _record_search_metrics(request, None)
        _schedule_audit(
            background_tasks,
            auth=auth,
            request=request,
            response=response,
            source=source,
            workspace_id="multi",
        )
        return response

    # #13: concurrent, bounded fan-out with per-workspace failure isolation.
    # Permission scoping (#45): user_workspaces == get_user_workspace_ids, the
    # caller's authorised set, so merged results cannot cross authorization.
    # Wrapped as a retrieve callable so the quality gate can re-run it once for a
    # bounded fallback (#43) over the SAME authorised workspace set.
    async def _retrieve_multi(req: SearchRequest) -> tuple[list[SearchResult], float]:
        merged, wall_ms = await _search_workspaces_concurrently(
            search_service,
            user_id=auth.key_info.user_id,
            workspace_ids=user_workspaces,
            request=req,
        )
        # Deterministic global top-k: sort by descending score with a stable
        # tiebreaker on (chunk_id, document_id) so equal scores rank consistently
        # across requests, then truncate to the requested limit (#13).
        merged.sort(key=lambda r: (-r.score, r.chunk_id, r.document_id))
        return merged[: req.limit], wall_ms

    limited, wall_clock_ms = await _retrieve_multi(request)

    response = SearchResponse(
        results=limited,
        query=request.query,
        total_results=len(limited),
        # Parallel wall-clock, not the sum of per-workspace times (#13).
        processing_time_ms=round(wall_clock_ms, 2),
        search_mode=request.search_mode,
    )
    # Adaptive retrieval quality gate + single bounded fallback (#43), reusing the
    # same authorised fan-out so a fallback can never widen the workspace scope.
    await _apply_quality_gate_and_fallback(response, request, _retrieve_multi)

    # PM-S019: context expansion needs a single workspace scope. For a
    # multi-workspace search (len != 1) we deliberately skip it — expanding a
    # match with a workspace that isn't its own risks a cross-tenant neighbour
    # read (#30). So include_context is honored only for single-workspace users;
    # for multi-workspace users it is a documented no-op (empty ctx_ws below).
    ctx_ws = user_workspaces[0] if len(user_workspaces) == 1 else ""
    await _expand_context_and_total_tokens(response, request, ctx_ws, auth.key_info.user_id)
    _record_search_metrics(request, None)
    _schedule_audit(
        background_tasks,
        auth=auth,
        request=request,
        response=response,
        source=source,
        workspace_id=user_workspaces[0] if len(user_workspaces) == 1 else "multi",
    )
    return response
