"""Eval capture (evals v1).

Records every single-workspace search into eval_query_events so agent feedback
can label it later.

Split into two halves, deliberately (#240):

- ``record_query_event`` is **awaited by the search handler**, before the
  response is returned. It has to be: the response carries the ``event_id``,
  and an identifier a caller cannot resolve is worse than no identifier. This
  was originally a fire-and-forget BackgroundTasks job copied from audit
  publishing — but audit has no client-visible handle and capture does, so the
  pattern did not transfer. A caller that posted feedback on the next round
  trip raced the INSERT and got a 404.
- ``purge_expired_events`` stays **write-behind**. It is the slow half, nobody
  holds a handle to it, and piggybacking it on capture avoids needing a
  scheduler at trial scale.

Unchanged contract: capture NEVER raises into the serving path. All database
work — including resolving the database handle — stays inside a try block, so
a cold or failed DB init cannot surface as a search error. What changed is how
failure is reported: ``record_query_event`` returns False instead of swallowing
silently, so the handler knows not to advertise an ``event_id`` that does not
exist.
"""

from __future__ import annotations

import uuid

from src.config.settings import settings
from src.models.search import SearchRequest, SearchResponse
from src.services.database import get_database
from src.utils.logger import get_logger

logger = get_logger(__name__)


def new_event_id() -> str:
    """Mint a capture event id ("ev_" + uuid4 hex)."""
    return "ev_" + uuid.uuid4().hex


def capture_enabled(workspace_id: str) -> bool:
    """Capture is on by default (opt-out): global flag AND per-workspace list."""
    if not settings.eval_capture_enabled:
        return False
    return workspace_id not in settings.eval_capture_optout_set()


async def record_query_event(
    *,
    event_id: str,
    workspace_id: str,
    user_id: str | None,
    request: SearchRequest,
    response: SearchResponse,
    transport: str,
) -> bool:
    """Persist one search event. Returns True when the row is durable.

    Awaited on the request path (#240) so the ``event_id`` the response carries
    is resolvable the moment the caller holds it. Resolves the database handle
    here rather than in the search handler so even a failed DB init stays
    inside this try block and cannot surface as a search error.

    Returns False instead of raising: the caller must be able to distinguish
    "captured" from "not captured" — that is what decides whether an
    ``event_id`` is advertised at all — without capture ever being able to fail
    a search.

    ``transport`` (#241) is ``"rest"`` or ``"mcp"`` — which surface produced
    this event — required (not defaulted) so a future third transport cannot
    silently mislabel itself by omission; see ``capture_search_event`` below,
    the single call site both surfaces go through.
    """
    try:
        db = await get_database()
        await db.insert_eval_event(
            event_id=event_id,
            workspace_id=workspace_id,
            user_id=user_id,
            query_text=request.query,
            search_mode=response.search_mode,
            result_doc_ids=[r.document_id for r in response.results],
            result_chunk_ids=[r.chunk_id for r in response.results],
            top_score=response.results[0].score if response.results else None,
            quality_verdict=response.quality_verdict.verdict if response.quality_verdict else None,
            latency_ms=response.processing_time_ms,
            transport=transport,
        )
        return True
    except Exception as exc:  # noqa: BLE001 — capture is best-effort by contract
        logger.warning("eval_capture_failed", event_id=event_id, error=str(exc))
        return False


async def capture_search_event(
    *,
    transport: str,
    workspace_id: str,
    user_id: str | None,
    request: SearchRequest,
    response: SearchResponse,
) -> str | None:
    """Mint + persist the capture event for one single-workspace search, and
    stamp ``response.event_id`` when it lands (#241).

    THE shared search-path helper: REST's ``POST /v1/search`` and MCP's
    ``search_documents`` / ``search_memory`` both call this — and only
    this — to capture a search. Before #241, REST inlined "mint id, call
    ``record_query_event``, stamp the response" directly at its call site and
    MCP had no equivalent at all; any field ``record_query_event`` needed
    later would have had to be added at REST's call site and would then
    silently miss a second, independently-written MCP call site (the fan-out
    shape the issue calls out). Factoring the whole mint-record-stamp
    sequence into one function makes that class of drift unrepresentable:
    there is only one call site per transport, and both go through the exact
    same three lines.

    Caller contract (unchanged from REST's original inline version, now
    shared instead of duplicated):
    - Call this only for a SINGLE-workspace search, and only after checking
      ``capture_enabled(workspace_id)`` — this function does not re-check it,
      because the caller already needs that same boolean to decide whether to
      schedule ``purge_expired_events`` afterwards, and computing it twice
      would be the only thing left to drift. A multi-workspace fan-out has no
      one response to attribute an event to, and REST has never captured for
      that path either — callers must not call this per sub-workspace of a
      fan-out.
    - ``response`` is mutated in place (``event_id`` set) only on success;
      on any failure it is left untouched, so ``response.event_id`` staying
      ``None`` is itself the "not captured" signal — never a dangling id.

    Returns the minted ``event_id``, or ``None`` when the write did not land.
    """
    event_id = new_event_id()
    if await record_query_event(
        event_id=event_id,
        workspace_id=workspace_id,
        user_id=user_id,
        request=request,
        response=response,
        transport=transport,
    ):
        response.event_id = event_id
        return event_id
    return None


async def purge_expired_events(workspace_id: str) -> None:
    """Drop raw events past the retention window. Write-behind, best-effort.

    Scheduled after the response (no client-visible handle, and it is the slow
    half of capture). Piggybacks on search traffic so trial deployments need no
    scheduler; a workspace that stops being searched simply stops purging,
    which is acceptable because retention is a storage bound, not a
    correctness one.
    """
    try:
        db = await get_database()
        await db.purge_expired_eval_events(
            workspace_id=workspace_id, retention_days=settings.eval_retention_days
        )
    except Exception as exc:  # noqa: BLE001 — purge is best-effort by contract
        logger.warning("eval_purge_failed", workspace_id=workspace_id, error=str(exc))
