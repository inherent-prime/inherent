"""Eval capture write-behind (evals v1).

Records every single-workspace search into eval_query_events so agent feedback
can label it later. Runs as a FastAPI BackgroundTasks job scheduled by the
search handler (same fire-and-forget pattern as audit publishing): it must
NEVER raise into, or add latency to, the serving path. To keep that guarantee,
ALL database work — including resolving the database handle itself — happens
inside record_query_event's try block, so a cold or failed DB init can never
surface as a search error. The retention purge piggybacks on capture so no
scheduler is needed at trial scale.
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
) -> None:
    """Persist one search event, then opportunistically purge expired rows.

    Resolves the database handle here (not in the search handler) so even a
    failed DB init stays inside this try block, off the request path.
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
        )
        await db.purge_expired_eval_events(
            workspace_id=workspace_id, retention_days=settings.eval_retention_days
        )
    except Exception as exc:  # noqa: BLE001 — capture is best-effort by contract
        logger.warning("eval_capture_failed", event_id=event_id, error=str(exc))
