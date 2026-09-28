"""Coverage test for inherent#393: every retrieval-returning query is
attributed to its user, 100% of the time, for BOTH REST and MCP, API-key
and OAuth callers alike.

Before this fix, only REST's ``POST /v1/search`` published an audit event —
every MCP tool call (stdio, HTTP API-key, HTTP OAuth) published nothing at
all, and there was no `principal_type` / `principal_id` / `surface` /
`tool_name` on the event to tell an API-key call from an OAuth one or a REST
call from an MCP one.

This module drives N=8 real calls across the matrix the issue asks for --
REST and MCP (stdio + HTTP), API key and OAuth, search + a vertical-pack
profile tool + citations, success + denied + error -- and asserts exactly
N audit events were published, each carrying the caller/surface/tool_name
the call actually used and (for a successful retrieval) the exact chunk ids
returned. It reuses the same "patch get_mq_service, assert on the published
event dict" pattern ``tests/services/test_audit_publisher.py`` already uses
for REST, applied at each of the three MCP dispatch loops
(``src/mcp_server/server.py``'s stdio ``call_tool``,
``src/mcp_server/http_transport.py``'s HTTP ``call_tool`` and
``_call_tool_oauth``) plus REST's own ``search_documents`` route.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import mcp.types as mcp_types
import pytest
from fastapi import BackgroundTasks
from inh_contracts.vertical_discovery import discover_all_packs

from src.api.v1 import search as search_api
from src.mcp_server import audit as mcp_audit
from src.mcp_server import http_transport, tool_profiles
from src.mcp_server import server as mcp_server
from src.models.api_key import APIKeyInfo
from src.models.citation import Citation
from src.models.search import SearchRequest, SearchResponse, SearchResult
from src.services.auth import Principal

pytestmark = [pytest.mark.contract]

_FIXTURE_PACKS_DIR = Path(__file__).resolve().parents[1] / "fixtures"


def _handbook_vertical():
    return discover_all_packs(str(_FIXTURE_PACKS_DIR)).packs["handbook"]


def _key(*, key_id: str = "key-1", user_id: str = "user-1", permissions=("search",)) -> APIKeyInfo:
    return APIKeyInfo(
        key_id=key_id,
        user_id=user_id,
        workspace_id=None,
        permissions=list(permissions),  # type: ignore[arg-type]
        rate_limit=100,
        status="active",
    )


def _search_response(*, chunk_id: str = "chunk-1") -> SearchResponse:
    return SearchResponse(
        results=[
            SearchResult(
                chunk_id=chunk_id,
                document_id="doc-1",
                document_name="report.pdf",
                content="Paris is the capital of France.",
                score=0.91,
                # get_citations only surfaces a result that already carries
                # one (src/mcp_server/server.py::_handle_get_citations) --
                # needed so this fixture also exercises that tool's path.
                citation=Citation(
                    chunk_id=chunk_id,
                    document_id="doc-1",
                    document_name="report.pdf",
                    content="Paris is the capital of France.",
                    score=0.91,
                ),
            )
        ],
        query="q",
        total_results=1,
        processing_time_ms=7.5,
        search_mode="hybrid",
    )


class _RecordingPublisher:
    """Stand-in for ``publish_audit_event`` that records every call instead
    of touching MQ (the same "patch the publisher, assert on the dict"
    approach ``test_audit_publisher.py`` uses).

    MCP's choke point (``src/mcp_server/audit.py``) fires this through
    ``asyncio.create_task`` — genuinely fire-and-forget, exactly like REST's
    ``BackgroundTasks`` — so a caller here must yield the loop once
    (``await asyncio.sleep(0)``) after each MCP call for the recorded event
    to land before assertions run; ``drain()`` below is that one line, named
    for what it does at each call site.
    """

    def __init__(self) -> None:
        self.events: list[dict] = []

    async def _record(self, event: dict) -> None:
        self.events.append(event)

    @staticmethod
    async def drain() -> None:
        await asyncio.sleep(0)


@pytest.fixture
def recorder(monkeypatch):
    rec = _RecordingPublisher()
    # MCP's choke point imports `publish_audit_event` into ITS OWN namespace
    # (`from src.services.audit_publisher import ... publish_audit_event`),
    # so it must be patched there, not on `audit_publisher` directly (which
    # audit.py's own reference would not see).
    monkeypatch.setattr(mcp_audit, "publish_audit_event", rec._record)
    # REST's search route imports the SAME name into ITS OWN namespace too
    # (src/api/v1/search.py) and schedules it via `BackgroundTasks` instead
    # of `asyncio.create_task` -- run via `await background_tasks()` in the
    # REST call below, no draining needed.
    monkeypatch.setattr(search_api, "publish_audit_event", rec._record)
    return rec


async def _call_stdio_tool(
    name: str, arguments: dict, key_info: APIKeyInfo | None, *, db: AsyncMock | None = None
):
    """Drive the REAL stdio dispatcher (create_mcp_server()'s call_tool),
    with a fixed, already-validated key_info -- mirrors how
    tests/contract/test_mcp_contract.py exercises this handler.

    ``db`` (optional) is the SAME database double the calling test already
    configured (e.g. ``get_user_workspace_ids``) -- ``validate_api_key`` is
    layered onto it here rather than replaced, so a caller's other stubs
    stay intact. A caller with no auth-relevant db behaviour to configure
    can omit it and get a bare ``AsyncMock``.
    """
    server = mcp_server.create_mcp_server()
    handler = server.request_handlers[mcp_types.CallToolRequest]
    db = db if db is not None else AsyncMock()
    db.validate_api_key = AsyncMock(return_value=key_info)
    with patch.object(mcp_server, "get_database", AsyncMock(return_value=db)):
        req = mcp_types.CallToolRequest(
            method="tools/call",
            params=mcp_types.CallToolRequestParams(
                name=name, arguments={**arguments, "api_key": "ink_test"}
            ),
        )
        result = await handler(req)
    return result.root


async def _call_http_tool(name: str, arguments: dict, key_info: APIKeyInfo | None):
    """Drive the REAL HTTP API-key dispatcher, same helper shape as
    tests/contract/test_mcp_http_transport.py / test_mcp_quotas.py."""
    server = http_transport.create_http_mcp_server()
    handler = server.request_handlers[mcp_types.CallToolRequest]
    token = http_transport._current_key_info.set(key_info)
    try:
        req = mcp_types.CallToolRequest(
            method="tools/call",
            params=mcp_types.CallToolRequestParams(name=name, arguments=arguments),
        )
        result = await handler(req)
    finally:
        http_transport._current_key_info.reset(token)
    return result.root


class TestAttributionCoverage:
    """N calls in, exactly N audit events out — with correct attribution."""

    async def test_eight_calls_across_rest_and_mcp_produce_eight_correctly_attributed_events(
        self, recorder
    ):
        db = AsyncMock()
        db.get_user_workspace_ids = AsyncMock(return_value=["ws-1"])
        db.get_authorized_workspace_ids = AsyncMock(return_value=["ws-1"])
        search_service = AsyncMock()
        search_service.search = AsyncMock(return_value=_search_response())

        # ---------------------------------------------------------------
        # Call 1: REST search_documents, API-key caller -- success.
        # ---------------------------------------------------------------
        rest_auth = AsyncMock()
        rest_auth.workspace_id = "ws-1"
        rest_auth.key_info = _key(key_id="rest-key-1", user_id="rest-user-1")
        background_tasks = BackgroundTasks()
        with (
            patch.object(search_api, "_apply_quality_gate_and_fallback", new=AsyncMock()),
            patch.object(search_api, "_expand_context_and_total_tokens", new=AsyncMock()),
            patch.object(search_api, "_record_search_metrics", new=lambda *a, **k: None),
            patch("src.services.eval_capture.capture_enabled", return_value=False),
        ):
            await search_api.search_documents(
                request=SearchRequest(query="q"),
                auth=rest_auth,
                search_service=search_service,
                background_tasks=background_tasks,
            )
        await background_tasks()

        # ---------------------------------------------------------------
        # Call 2: MCP stdio search_documents, API-key caller -- success.
        # ---------------------------------------------------------------
        stdio_key = _key(key_id="stdio-key-1", user_id="stdio-user-1")
        with (
            patch.object(mcp_server, "get_search_service", AsyncMock(return_value=search_service)),
            patch("src.services.eval_capture.get_database", AsyncMock(return_value=db)),
        ):
            result = await _call_stdio_tool("search_documents", {"query": "q"}, stdio_key, db=db)
            await recorder.drain()
        assert not result.isError

        # ---------------------------------------------------------------
        # Call 3: MCP HTTP search_documents, API-key caller -- success.
        # ---------------------------------------------------------------
        http_key = _key(key_id="http-key-1", user_id="http-user-1")
        with (
            patch.object(mcp_server, "get_database", AsyncMock(return_value=db)),
            patch.object(mcp_server, "get_search_service", AsyncMock(return_value=search_service)),
            patch("src.services.eval_capture.get_database", AsyncMock(return_value=db)),
        ):
            result = await _call_http_tool("search_documents", {"query": "q"}, http_key)
            await recorder.drain()
        assert not result.isError

        # ---------------------------------------------------------------
        # Call 4: MCP HTTP search_documents, OAuth caller with a resolved
        # identity -- success.
        # ---------------------------------------------------------------
        oauth_principal = Principal(
            principal_id="oauth-subject-1",
            principal_type="oauth",
            scopes=frozenset({"kb:search"}),
            resolved_user_id="oauth-user-1",
        )
        with (
            patch.object(mcp_server, "get_database", AsyncMock(return_value=db)),
            patch.object(mcp_server, "get_search_service", AsyncMock(return_value=search_service)),
            patch("src.services.eval_capture.get_database", AsyncMock(return_value=db)),
        ):
            result = await http_transport._call_tool_oauth(
                "search_documents", {"query": "q"}, oauth_principal
            )
            await recorder.drain()
        assert not result.isError

        # ---------------------------------------------------------------
        # Call 5: MCP get_citations, API-key caller -- success.
        # ---------------------------------------------------------------
        with patch.object(mcp_server, "get_search_service", AsyncMock(return_value=search_service)):
            result = await _call_stdio_tool(
                "get_citations",
                {"query": "q"},
                _key(key_id="cite-key-1", user_id="cite-user-1"),
                db=db,
            )
            await recorder.drain()
        assert not result.isError

        # ---------------------------------------------------------------
        # Call 6: MCP vertical-pack profile tool, API-key caller -- success.
        # ---------------------------------------------------------------
        vertical = _handbook_vertical()
        profile_key = _key(key_id="profile-key-1", user_id="profile-user-1")
        with (
            patch.object(
                tool_profiles, "get_authorized_workspace_ids", AsyncMock(return_value=["ws-1"])
            ),
            patch.object(tool_profiles, "resolve_workspace_pack", return_value=vertical),
            patch.object(
                tool_profiles, "get_search_service", AsyncMock(return_value=search_service)
            ),
        ):
            profile_tools = await tool_profiles.resolve_profile_tools(profile_key, database=db)
            profile_tool = profile_tools["search_sections"]
            content = await mcp_audit.dispatch_and_audit(
                profile_tool, "search_sections", profile_key, {"query": "q"}, surface="mcp"
            )
            await recorder.drain()
        assert "Error" not in content[0].text

        # ---------------------------------------------------------------
        # Call 7: MCP tool call denied for lacking permission (no handler
        # ever runs) -- outcome="denied", no returned ids.
        # ---------------------------------------------------------------
        denied_key = _key(key_id="denied-key-1", user_id="denied-user-1", permissions=("read",))
        result = await _call_stdio_tool("search_documents", {"query": "q"}, denied_key, db=db)
        await recorder.drain()
        assert result.isError is False or "does not have" in result.content[0].text

        # ---------------------------------------------------------------
        # Call 8: MCP tool call whose handler reports an error -- a
        # workspace_id the key is not authorized for -- outcome="error",
        # no returned ids. (A missing ``query`` is rejected by the MCP
        # SDK's own jsonschema validation, BEFORE our dispatcher ever runs
        # -- not a case this module's choke point can see at all, so this
        # uses a handler-level error instead, exactly like get_citations'
        # equivalent TagFilterError path would.)
        # ---------------------------------------------------------------
        error_key = _key(key_id="error-key-1", user_id="error-user-1")
        await _call_stdio_tool(
            "search_documents",
            {"query": "q", "workspace_id": "ws-not-authorized"},
            error_key,
            db=db,
        )
        await recorder.drain()

        # =================================================================
        # Assertions: exactly 8 events, correctly attributed.
        # =================================================================
        events = recorder.events
        assert len(events) == 8, f"expected 8 audit events, got {len(events)}: {events}"

        by_key = {e["api_key_id"]: e for e in events}

        rest_event = by_key["rest-key-1"]
        assert rest_event["surface"] == "rest"
        assert rest_event["principal_type"] == "api_key"
        assert rest_event["principal_id"] == "rest-key-1"
        assert rest_event["user_id"] == "rest-user-1"
        assert rest_event["tool_name"] == "search_documents"
        assert rest_event["outcome"] == "ok"
        assert rest_event["returned_chunk_ids"] == ["chunk-1"]

        stdio_event = by_key["stdio-key-1"]
        assert stdio_event["surface"] == "mcp"
        assert stdio_event["principal_type"] == "api_key"
        assert stdio_event["tool_name"] == "search_documents"
        assert stdio_event["outcome"] == "ok"
        assert stdio_event["returned_chunk_ids"] == ["chunk-1"]
        assert stdio_event["workspace_ids"] == ["ws-1"]

        http_event = by_key["http-key-1"]
        assert http_event["surface"] == "mcp"
        assert http_event["principal_type"] == "api_key"
        assert http_event["outcome"] == "ok"

        oauth_event = by_key["oauth:oauth-subject-1"]
        assert oauth_event["surface"] == "mcp"
        assert oauth_event["principal_type"] == "oauth"
        assert oauth_event["principal_id"] == "oauth-subject-1"
        assert oauth_event["user_id"] == "oauth-user-1"
        assert oauth_event["outcome"] == "ok"
        assert oauth_event["returned_chunk_ids"] == ["chunk-1"]

        citations_event = by_key["cite-key-1"]
        assert citations_event["tool_name"] == "get_citations"
        assert citations_event["outcome"] == "ok"
        assert citations_event["returned_chunk_ids"] == ["chunk-1"]

        profile_event = by_key["profile-key-1"]
        assert profile_event["tool_name"] == "search_sections"
        assert profile_event["outcome"] == "ok"
        assert profile_event["workspace_ids"] == ["ws-1"]

        denied_event = by_key["denied-key-1"]
        assert denied_event["outcome"] == "denied"
        assert denied_event["returned_chunk_ids"] == []

        error_event = by_key["error-key-1"]
        assert error_event["outcome"] == "error"
        assert error_event["returned_chunk_ids"] == []
