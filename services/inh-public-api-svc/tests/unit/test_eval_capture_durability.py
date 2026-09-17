"""An `event_id` in a search response must already resolve (#240).

The defect: `POST /v1/search` minted the id, put it on the response, and
scheduled the INSERT via `BackgroundTasks` — which Starlette runs *after* the
response is sent. The caller therefore received an identifier that did not yet
exist, and a caller posting feedback on the next round trip raced the write.
The insert usually won; under CI load it did not, and
`POST /v1/evals/feedback` answered 404 "Unknown event_id".

That is not a test-only race. `report_feedback` is an MCP tool, ADR 0003 makes
`event_id` the public join key for external eval stacks, and the documented
flywheel is search → feedback back to back.

The invariant these tests pin is the general one an API owes its caller:
**if a response carries an identifier, that identifier resolves.** Concretely —
capture is durable BEFORE the response is returned, and when it cannot be made
durable the response carries no id at all rather than a dangling one.

What deliberately did NOT change: capture still never raises into the search
path, and the retention purge stays write-behind (it is the slow part and has
no client-visible handle).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import BackgroundTasks

from src.api.v1 import search as search_api
from src.models.search import SearchRequest, SearchResponse, SearchResult
from src.services import eval_capture


def _response() -> SearchResponse:
    return SearchResponse(
        results=[
            SearchResult(
                chunk_id="c0",
                document_id="d0",
                document_name="doc0",
                content="x",
                score=0.9,
            )
        ],
        query="q",
        total_results=1,
        processing_time_ms=1.0,
        search_mode="hybrid",
    )


def _auth():
    auth = MagicMock()
    auth.workspace_id = "ws-1"
    auth.key_info.user_id = "u-1"
    return auth


async def _call_search(
    db_factory, *, run_background: bool = False
) -> tuple[SearchResponse, BackgroundTasks]:
    """Invoke the real route with the heavy collaborators stubbed out.

    Only the capture step is under test, so the quality gate, context
    expansion, metrics and audit are neutralised. `db_factory` stands in for
    `get_database` inside the capture service.

    `run_background` drains the queued tasks *inside* the patch, standing in
    for what Starlette does after the response is sent. Left False by default:
    the point of most of these tests is what is true BEFORE that happens.
    """
    search_service = MagicMock()
    search_service.search = AsyncMock(return_value=_response())
    background_tasks = BackgroundTasks()

    with (
        patch.object(search_api, "_apply_quality_gate_and_fallback", new=AsyncMock()),
        patch.object(search_api, "_expand_context_and_total_tokens", new=AsyncMock()),
        patch.object(search_api, "_record_search_metrics", new=MagicMock()),
        patch.object(search_api, "_schedule_audit", new=MagicMock()),
        patch("src.services.eval_capture.get_database", new=db_factory),
    ):
        response = await search_api.search_documents(
            request=SearchRequest(query="q", search_mode="hybrid"),
            auth=_auth(),
            search_service=search_service,
            background_tasks=background_tasks,
        )
        if run_background:
            await background_tasks()
    return response, background_tasks


@pytest.mark.asyncio
async def test_capture_row_is_written_before_the_response_returns() -> None:
    """The INSERT must have happened by the time the caller holds the id.

    Background tasks are deliberately NOT run here: that is the whole point.
    Anything still sitting in `background_tasks` has not happened yet from the
    caller's perspective, because Starlette runs them after the response.
    """
    db = AsyncMock()
    _, background_tasks = await _call_search(AsyncMock(return_value=db))

    db.insert_eval_event.assert_awaited_once()


@pytest.mark.asyncio
async def test_returned_event_id_matches_the_row_that_was_written() -> None:
    """A caller must be able to hand the id straight back to /v1/evals/feedback."""
    db = AsyncMock()
    response, _ = await _call_search(AsyncMock(return_value=db))

    assert response.event_id, "expected the response to carry an event_id"
    assert db.insert_eval_event.call_args.kwargs["event_id"] == response.event_id


@pytest.mark.asyncio
async def test_rest_capture_records_rest_transport() -> None:
    """REST search events are tagged transport='rest' (#241): the captured
    row must say which surface produced it, and REST must keep saying
    'rest' now that MCP shares the same capture helper."""
    db = AsyncMock()
    await _call_search(AsyncMock(return_value=db))

    assert db.insert_eval_event.call_args.kwargs["transport"] == "rest"


@pytest.mark.asyncio
async def test_no_event_id_when_the_capture_write_fails() -> None:
    """A failed capture yields no id, never a dangling one.

    Returning an id whose row does not exist is the defect itself: the caller
    cannot tell it apart from a durable id and gets a 404 later.
    """
    db = AsyncMock()
    db.insert_eval_event.side_effect = RuntimeError("db down")
    response, _ = await _call_search(AsyncMock(return_value=db))

    assert response.event_id is None


@pytest.mark.asyncio
async def test_search_still_succeeds_when_the_database_is_unreachable() -> None:
    """Capture failure must never surface as a search error (unchanged contract)."""
    response, _ = await _call_search(AsyncMock(side_effect=RuntimeError("db init failed")))

    assert response.results, "search must still return its results"
    assert response.event_id is None


@pytest.mark.asyncio
async def test_retention_purge_stays_write_behind() -> None:
    """The purge is the slow half and has no client-visible handle — defer it.

    It must NOT run inside the request, and it must be queued so it still runs
    after the response.
    """
    db = AsyncMock()
    _, background_tasks = await _call_search(AsyncMock(return_value=db))

    db.purge_expired_eval_events.assert_not_awaited()
    assert background_tasks.tasks, "expected the purge to be queued as a background task"

    # Now drain them, as Starlette does once the response is on the wire.
    db2 = AsyncMock()
    await _call_search(AsyncMock(return_value=db2), run_background=True)
    db2.purge_expired_eval_events.assert_awaited_once()


@pytest.mark.asyncio
async def test_record_query_event_reports_durability() -> None:
    """The service tells its caller whether the row landed, and never raises."""
    db = AsyncMock()
    with patch("src.services.eval_capture.get_database", new=AsyncMock(return_value=db)):
        assert (
            await eval_capture.record_query_event(
                event_id="ev_x",
                workspace_id="ws-1",
                user_id="u-1",
                request=SearchRequest(query="q"),
                response=_response(),
                transport="rest",
            )
            is True
        )

    db.insert_eval_event.side_effect = RuntimeError("db down")
    with patch("src.services.eval_capture.get_database", new=AsyncMock(return_value=db)):
        assert (
            await eval_capture.record_query_event(
                event_id="ev_x",
                workspace_id="ws-1",
                user_id="u-1",
                request=SearchRequest(query="q"),
                response=_response(),
                transport="rest",
            )
            is False
        )
