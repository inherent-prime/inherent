"""Capture write-behind (evals v1): never raises, honors opt-out, piggybacks
retention purge, and derives event fields from the search response."""

from unittest.mock import AsyncMock, patch

import pytest

from src.models.search import SearchRequest, SearchResponse, SearchResult
from src.services import eval_capture


def _response(n=1):
    results = [
        SearchResult(
            chunk_id=f"c{i}",
            document_id=f"d{i}",
            document_name=f"doc{i}",
            content="x",
            score=0.9 - i * 0.1,
        )
        for i in range(n)
    ]
    return SearchResponse(
        results=results, query="q", total_results=n, processing_time_ms=12.5, search_mode="hybrid"
    )


def test_new_event_id_format():
    eid = eval_capture.new_event_id()
    assert eid.startswith("ev_") and len(eid) == 35  # "ev_" + 32 hex chars


def test_capture_enabled_honors_optout():
    with patch.object(eval_capture, "settings") as s:
        s.eval_capture_enabled = True
        s.eval_capture_optout_set.return_value = {"ws-blocked"}
        assert eval_capture.capture_enabled("ws-1") is True
        assert eval_capture.capture_enabled("ws-blocked") is False
        s.eval_capture_enabled = False
        assert eval_capture.capture_enabled("ws-1") is False


@pytest.mark.asyncio
async def test_record_query_event_derives_fields_from_the_response():
    db = AsyncMock()
    with patch("src.services.eval_capture.get_database", new=AsyncMock(return_value=db)):
        await eval_capture.record_query_event(
            event_id="ev_x",
            workspace_id="ws-1",
            user_id="u-1",
            request=SearchRequest(query="q", search_mode="hybrid"),
            response=_response(2),
            transport="rest",
        )
    kwargs = db.insert_eval_event.call_args.kwargs
    assert kwargs["result_doc_ids"] == ["d0", "d1"]
    assert kwargs["result_chunk_ids"] == ["c0", "c1"]
    assert kwargs["top_score"] == pytest.approx(0.9)
    assert kwargs["transport"] == "rest"
    # The purge no longer rides along here (#240): record_query_event is awaited
    # on the request path, so it does the INSERT and nothing else. The purge is
    # queued separately by the search handler and stays write-behind — see
    # test_eval_capture_durability.py.
    db.purge_expired_eval_events.assert_not_awaited()


@pytest.mark.asyncio
async def test_purge_expired_events_uses_the_configured_retention():
    db = AsyncMock()
    with (
        patch("src.services.eval_capture.get_database", new=AsyncMock(return_value=db)),
        patch.object(eval_capture, "settings") as s,
    ):
        s.eval_retention_days = 30
        await eval_capture.purge_expired_events("ws-1")
    db.purge_expired_eval_events.assert_awaited_once_with(workspace_id="ws-1", retention_days=30)


@pytest.mark.asyncio
async def test_purge_expired_events_never_raises():
    """Best-effort like capture: a failed purge cannot surface anywhere."""
    with patch(
        "src.services.eval_capture.get_database",
        new=AsyncMock(side_effect=RuntimeError("db down")),
    ):
        await eval_capture.purge_expired_events("ws-1")


@pytest.mark.asyncio
async def test_record_query_event_never_raises():
    db = AsyncMock()
    db.insert_eval_event.side_effect = RuntimeError("db down")
    # Must swallow: capture failure can never surface into the search path.
    with patch("src.services.eval_capture.get_database", new=AsyncMock(return_value=db)):
        await eval_capture.record_query_event(
            event_id="ev_x",
            workspace_id="ws-1",
            user_id=None,
            request=SearchRequest(query="q"),
            response=_response(0),
            transport="rest",
        )


@pytest.mark.asyncio
async def test_record_query_event_swallows_db_resolution_failure():
    # Even resolving the database handle (cold/failed init) must not propagate:
    # the handle is acquired inside the task's try block, off the request path.
    with patch(
        "src.services.eval_capture.get_database",
        new=AsyncMock(side_effect=RuntimeError("db init failed")),
    ):
        await eval_capture.record_query_event(
            event_id="ev_x",
            workspace_id="ws-1",
            user_id=None,
            request=SearchRequest(query="q"),
            response=_response(1),
            transport="rest",
        )


# ---------------------------------------------------------------------------
# capture_search_event (#241): the ONE shared mint-record-stamp helper both
# REST (src/api/v1/search.py) and MCP (src/mcp_server/server.py) call for a
# single-workspace search, so an event_id and every field it carries can
# never drift between the two transports.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_capture_search_event_mints_and_stamps_the_response():
    db = AsyncMock()
    response = _response(1)
    with patch("src.services.eval_capture.get_database", new=AsyncMock(return_value=db)):
        event_id = await eval_capture.capture_search_event(
            transport="mcp",
            workspace_id="ws-1",
            user_id="u-1",
            request=SearchRequest(query="q"),
            response=response,
        )

    assert event_id is not None
    assert response.event_id == event_id
    kwargs = db.insert_eval_event.call_args.kwargs
    assert kwargs["event_id"] == event_id
    assert kwargs["transport"] == "mcp"


@pytest.mark.asyncio
async def test_capture_search_event_leaves_response_untouched_on_failure():
    """A failed write must not stamp a dangling id (#240's invariant, reused
    by the shared helper): response.event_id stays None, not a bad id."""
    db = AsyncMock()
    db.insert_eval_event.side_effect = RuntimeError("db down")
    response = _response(1)
    with patch("src.services.eval_capture.get_database", new=AsyncMock(return_value=db)):
        event_id = await eval_capture.capture_search_event(
            transport="mcp",
            workspace_id="ws-1",
            user_id="u-1",
            request=SearchRequest(query="q"),
            response=response,
        )

    assert event_id is None
    assert response.event_id is None


@pytest.mark.asyncio
async def test_capture_search_event_rest_and_mcp_write_equivalent_events():
    """Transport parity (#241): the SAME query through both transports must
    produce the same captured fields, differing only in transport/event_id."""
    db_rest, db_mcp = AsyncMock(), AsyncMock()
    request = SearchRequest(query="q", search_mode="hybrid")
    response_rest, response_mcp = _response(2), _response(2)

    with patch("src.services.eval_capture.get_database", new=AsyncMock(return_value=db_rest)):
        await eval_capture.capture_search_event(
            transport="rest",
            workspace_id="ws-1",
            user_id="u-1",
            request=request,
            response=response_rest,
        )
    with patch("src.services.eval_capture.get_database", new=AsyncMock(return_value=db_mcp)):
        await eval_capture.capture_search_event(
            transport="mcp",
            workspace_id="ws-1",
            user_id="u-1",
            request=request,
            response=response_mcp,
        )

    rest_kwargs = dict(db_rest.insert_eval_event.call_args.kwargs)
    mcp_kwargs = dict(db_mcp.insert_eval_event.call_args.kwargs)
    # event_id is minted per-call and transport is the one field the two
    # transports are SUPPOSED to disagree on; everything else must match.
    for key in ("event_id", "transport"):
        del rest_kwargs[key]
        del mcp_kwargs[key]
    assert rest_kwargs == mcp_kwargs
