"""MCP search event capture (#241).

Before this fix, ``record_query_event`` was called only from the REST search
handler (``src/api/v1/search.py``) -- ``_run_search`` on the MCP side minted
no event and returned no ``event_id``, so ``report_feedback`` was unusable
over MCP and query analytics had no MCP data source at all.

The fix mints the event through ``eval_capture.capture_search_event`` -- THE
one shared helper both ``src/api/v1/search.py`` and
``src/mcp_server/server.py`` call -- rather than adding a second, independent
capture call on the MCP side (the fan-out shape the issue explicitly rejects,
since it lets the two transports drift field-by-field).

These tests cover the acceptance criteria that can be verified offline:
1. An MCP search mints an event and returns event_id in a shape an agent can
   read (``test_single_workspace_search_returns_event_id``).
2. report_feedback accepts an MCP-minted event_id
   (``test_event_id_round_trips_through_report_feedback``).
3. REST behaviour/fields are unchanged for the same query
   (``test_rest_and_mcp_capture_equivalent_events_for_the_same_query``).
4. The captured row records which transport produced it
   (``test_mcp_capture_records_mcp_transport``).
5. Multi-workspace / capture-disabled / failed-write / empty-results shapes.
"""

from __future__ import annotations

import json
from contextlib import ExitStack, contextmanager
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import BackgroundTasks

from src.api.v1 import search as search_api
from src.mcp_server import server as mcp_server
from src.models.api_key import APIKeyInfo
from src.models.search import SearchRequest, SearchResponse, SearchResult

pytestmark = pytest.mark.asyncio


@contextmanager
def _mcp_patches(db: AsyncMock, search: AsyncMock):
    """Patch every ``get_database`` binding a captured MCP search touches.

    ``src/mcp_server/server.py`` and ``src/services/eval_capture.py`` each
    import ``get_database`` into their OWN module namespace (``from ... import
    get_database``), so patching one does not patch the other -- both must
    point at the same mock or capture silently resolves a real (unmocked)
    database and swallows the resulting error, which reads exactly like
    "capture is disabled" from the outside. Mirrors the two-patch pattern
    ``tests/unit/test_eval_capture_durability.py`` already uses for REST.
    """
    with ExitStack() as stack:
        stack.enter_context(patch.object(mcp_server, "get_database", AsyncMock(return_value=db)))
        stack.enter_context(
            patch.object(mcp_server, "get_search_service", AsyncMock(return_value=search))
        )
        stack.enter_context(
            patch("src.services.eval_capture.get_database", new=AsyncMock(return_value=db))
        )
        yield


def _key(*, workspace_id: str | None = None, permissions=("search",)) -> APIKeyInfo:
    return APIKeyInfo(
        key_id="key-1",
        user_id="u-1",
        workspace_id=workspace_id,
        permissions=list(permissions),  # type: ignore[arg-type]
        rate_limit=100,
        expires_at=None,
        status="active",
    )


def _search_response(query: str = "q") -> SearchResponse:
    return SearchResponse(
        results=[
            SearchResult(
                chunk_id="chunk-1",
                document_id="doc-1",
                document_name="report.pdf",
                content="Paris is the capital of France.",
                score=0.91,
            )
        ],
        query=query,
        total_results=1,
        processing_time_ms=7.5,
        search_mode="hybrid",
    )


def _structured_payload(content) -> dict:
    text = content[0].text
    block = text.split("```json", 1)[1].rsplit("```", 1)[0].strip()
    return json.loads(block)["structured"]


# --------------------------------------------------------------------------
# 1. single-workspace search mints and returns event_id
# --------------------------------------------------------------------------


async def test_single_workspace_search_returns_event_id():
    db = AsyncMock()
    db.get_user_workspace_ids = AsyncMock(return_value=["ws-1"])
    search = AsyncMock()
    search.search = AsyncMock(return_value=_search_response())

    with _mcp_patches(db, search):
        content = await mcp_server._handle_search(_key(), {"query": "q"})

    payload = _structured_payload(content)
    assert payload["event_id"], f"expected an event_id, got payload {payload}"
    db.insert_eval_event.assert_awaited_once()
    assert db.insert_eval_event.call_args.kwargs["event_id"] == payload["event_id"]


async def test_search_memory_also_returns_event_id():
    """search_memory shares _handle_search, so it must carry event_id too."""
    db = AsyncMock()
    db.get_user_workspace_ids = AsyncMock(return_value=["ws-1"])
    search = AsyncMock()
    search.search = AsyncMock(return_value=_search_response())

    with _mcp_patches(db, search):
        content = await mcp_server._handle_search(_key(), {"query": "q"})

    assert _structured_payload(content)["event_id"]


# --------------------------------------------------------------------------
# 2. report_feedback accepts an MCP-minted event_id (closes the loop)
# --------------------------------------------------------------------------


async def test_event_id_round_trips_through_report_feedback():
    db = AsyncMock()
    db.get_user_workspace_ids = AsyncMock(return_value=["ws-1"])
    search = AsyncMock()
    search.search = AsyncMock(return_value=_search_response())

    with _mcp_patches(db, search):
        content = await mcp_server._handle_search(_key(), {"query": "q"})
        event_id = _structured_payload(content)["event_id"]

        # report_feedback looks the event up by id + authorised workspaces;
        # stand in for the row capture just wrote.
        db.get_eval_event = AsyncMock(
            return_value={
                "event_id": event_id,
                "workspace_id": "ws-1",
                "query_text": "q",
                "search_mode": "hybrid",
                "result_doc_ids": ["doc-1"],
                "result_chunk_ids": ["chunk-1"],
            }
        )
        db.upsert_eval_feedback = AsyncMock(return_value=None)
        db.upsert_eval_case = AsyncMock(return_value="case_1")

        feedback_content = await mcp_server._handle_report_feedback(
            _key(), {"event_id": event_id, "verdict": "answered"}
        )

    assert "Error" not in feedback_content[0].text
    db.get_eval_event.assert_awaited_once()
    assert db.get_eval_event.call_args.kwargs["event_id"] == event_id
    db.upsert_eval_feedback.assert_awaited_once()


# --------------------------------------------------------------------------
# 3 + 4. transport field + REST/MCP parity for the identical query
# --------------------------------------------------------------------------


async def test_mcp_capture_records_mcp_transport():
    db = AsyncMock()
    db.get_user_workspace_ids = AsyncMock(return_value=["ws-1"])
    search = AsyncMock()
    search.search = AsyncMock(return_value=_search_response())

    with _mcp_patches(db, search):
        await mcp_server._handle_search(_key(), {"query": "q"})

    assert db.insert_eval_event.call_args.kwargs["transport"] == "mcp"


async def test_rest_and_mcp_capture_equivalent_events_for_the_same_query():
    """REST behaviour is unchanged (#241 acceptance criterion 3): the same
    query captured through each transport must produce the same event
    fields, differing only in event_id/transport (and latency, which is
    inherent to two separate SearchService.search calls in the harness)."""
    query = "refund policy"

    # --- REST -------------------------------------------------------------
    rest_db = AsyncMock()
    rest_search = AsyncMock()
    rest_search.search = AsyncMock(return_value=_search_response(query))
    rest_auth = AsyncMock()
    rest_auth.workspace_id = "ws-1"
    rest_auth.key_info.user_id = "u-1"

    with (
        patch.object(search_api, "_apply_quality_gate_and_fallback", new=AsyncMock()),
        patch.object(search_api, "_expand_context_and_total_tokens", new=AsyncMock()),
        patch.object(search_api, "_record_search_metrics", new=lambda *a, **k: None),
        patch.object(search_api, "_schedule_audit", new=lambda *a, **k: None),
        patch("src.services.eval_capture.get_database", new=AsyncMock(return_value=rest_db)),
    ):
        await search_api.search_documents(
            request=SearchRequest(query=query, search_mode="hybrid"),
            auth=rest_auth,
            search_service=rest_search,
            background_tasks=BackgroundTasks(),
        )

    # --- MCP ----------------------------------------------------------------
    mcp_db = AsyncMock()
    mcp_db.get_user_workspace_ids = AsyncMock(return_value=["ws-1"])
    mcp_search = AsyncMock()
    mcp_search.search = AsyncMock(return_value=_search_response(query))

    with _mcp_patches(mcp_db, mcp_search):
        await mcp_server._handle_search(_key(), {"query": query, "search_mode": "hybrid"})

    rest_kwargs = dict(rest_db.insert_eval_event.call_args.kwargs)
    mcp_kwargs = dict(mcp_db.insert_eval_event.call_args.kwargs)
    for key in ("event_id", "transport"):
        del rest_kwargs[key]
        del mcp_kwargs[key]
    assert rest_kwargs == mcp_kwargs
    assert rest_db.insert_eval_event.call_args.kwargs["transport"] == "rest"
    assert mcp_db.insert_eval_event.call_args.kwargs["transport"] == "mcp"


# --------------------------------------------------------------------------
# 5. multi-workspace / disabled / failed-write / empty-results shapes
# --------------------------------------------------------------------------


async def test_multi_workspace_search_returns_no_event_id():
    """Matches REST, which never captures its multi-workspace fan-out either:
    there is no single response to attribute one event to."""
    db = AsyncMock()
    db.get_user_workspace_ids = AsyncMock(return_value=["ws-1", "ws-2"])
    search = AsyncMock()
    search.search = AsyncMock(return_value=_search_response())

    with _mcp_patches(db, search):
        content = await mcp_server._handle_search(_key(), {"query": "q"})

    assert _structured_payload(content)["event_id"] is None
    db.insert_eval_event.assert_not_awaited()


async def test_capture_disabled_returns_no_event_id():
    db = AsyncMock()
    db.get_user_workspace_ids = AsyncMock(return_value=["ws-1"])
    search = AsyncMock()
    search.search = AsyncMock(return_value=_search_response())

    with _mcp_patches(db, search), patch.object(mcp_server, "capture_enabled", return_value=False):
        content = await mcp_server._handle_search(_key(), {"query": "q"})

    assert _structured_payload(content)["event_id"] is None
    db.insert_eval_event.assert_not_awaited()


async def test_failed_capture_write_returns_no_event_id_but_search_still_succeeds():
    db = AsyncMock()
    db.get_user_workspace_ids = AsyncMock(return_value=["ws-1"])
    db.insert_eval_event.side_effect = RuntimeError("db down")
    search = AsyncMock()
    search.search = AsyncMock(return_value=_search_response())

    with _mcp_patches(db, search):
        content = await mcp_server._handle_search(_key(), {"query": "q"})

    payload = _structured_payload(content)
    assert payload["event_id"] is None
    assert payload["results"], "search must still return its results"


async def test_empty_results_search_still_mints_an_event():
    """An empty result set is still a captured search (REST parity: capture
    does not gate on result count)."""
    db = AsyncMock()
    db.get_user_workspace_ids = AsyncMock(return_value=["ws-1"])
    search = AsyncMock()
    search.search = AsyncMock(
        return_value=SearchResponse(
            results=[],
            query="q",
            total_results=0,
            processing_time_ms=1.0,
            search_mode="hybrid",
        )
    )

    with _mcp_patches(db, search):
        content = await mcp_server._handle_search(_key(), {"query": "q"})

    payload = _structured_payload(content)
    assert payload["results"] == []
    assert payload["event_id"], "an empty-result search is still a captured event"
    db.insert_eval_event.assert_awaited_once()


async def test_failed_search_query_required_error_never_reaches_capture():
    """The pre-search validation error path (missing query) must not attempt
    capture at all."""
    db = AsyncMock()
    search = AsyncMock()

    with _mcp_patches(db, search):
        content = await mcp_server._handle_search(_key(), {"query": ""})

    assert content[0].text == "Error: Query is required"
    search.search.assert_not_called()
    db.insert_eval_event.assert_not_awaited()


async def test_scoped_key_single_bound_workspace_still_captures():
    """A workspace-scoped key (#138) narrows to exactly one workspace, which
    is still the single-workspace capture case."""
    db = AsyncMock()
    search = AsyncMock()
    search.search = AsyncMock(return_value=_search_response())

    with _mcp_patches(db, search):
        content = await mcp_server._handle_search(_key(workspace_id="ws-scoped"), {"query": "q"})

    payload = _structured_payload(content)
    assert payload["event_id"]
    assert db.insert_eval_event.call_args.kwargs["workspace_id"] == "ws-scoped"
