"""Offline MCP dispatch-overhead micro-benchmark (inherent#396).

The live-stack benchmark (``test_mcp_round_trip.py``, ``compose`` + ``benchmark``
marked) measures END-TO-END latency: network, auth DB round trips, the real
search backend, everything. It answers "is search over MCP fast enough for a
user", but it cannot run in an environment with no Compose stack (this sandbox
has none), and even where it can run, a slow result is hard to attribute: is
the 3s coming from OUR dispatch code, or from the vector store / embedding
call the search backend makes?

This test answers the narrower, always-runnable question: with the search
BACKEND mocked (a `SearchService.search` call that returns instantly), how
much time does OUR OWN per-call code add -- API-key/permission checks, tool-
profile resolution (vertical pack lookup + caching, inherent#392), tag-filter
validation, audit-event construction (inherent#393), and JSON-serializing the
structured response? That overhead is what #396's hotspot fixes target, and it
is exactly the part a mocked backend isolates: no network, no DB, no vector
store, so every millisecond measured here is dispatch-path Python.

Two call shapes, both exercised through the REAL dispatcher (the actual
registered `mcp.server.lowlevel.Server` request handlers, not a hand-rolled
stand-in):

* ``search_documents`` over stdio (``server.py``'s ``create_mcp_server``) --
  the plain built-in-tool path (auth, permission, search, audit).
* a vertical-pack tool-profile tool (``search_sections``, inherent#392) over
  the Streamable HTTP transport (``http_transport.py``'s
  ``create_http_mcp_server``) -- the path that additionally resolves a
  workspace's bound pack and rebuilds that pack's ToolDef on every call
  (see ``tool_profiles.resolve_profile_tools``).

Both build ONE shared ``Server`` instance outside the timing loop (matching
how ``src/main.py`` builds it once for the process lifetime) and run a
warm-up pass before measuring, so the numbers reflect steady-state overhead,
not first-call JIT/import/lru_cache-fill costs -- the SAME warm-up discipline
`test_search_latency_throughput.py`'s live benchmark uses.

The p95 ceiling (``MCP_DISPATCH_P95_MS``, default 50ms) is deliberately loose:
50ms of PURE in-memory Python dispatch overhead is ~1/60th of #396's 3000ms
end-to-end budget, so this catches a gross regression (e.g. re-reading a YAML
pack from disk on every call, or a JWKS fetch with no cache) without flaking
on a busy CI runner. Not `compose`/`benchmark` marked -- it needs no stack, so
it runs in the default offline suite where a regression is caught immediately
rather than only on the next Compose run.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from mcp.types import CallToolRequest, CallToolRequestParams

from src.config.settings import Settings
from src.mcp_server import audit as mcp_audit
from src.mcp_server import http_transport, tool_profiles
from src.mcp_server import server as mcp_server
from src.models.api_key import APIKeyInfo
from src.models.search import SearchResponse, SearchResult
from src.services import workspace_pack as workspace_pack_module
from tests.benchmark.run_search_benchmark import summarize

pytestmark = pytest.mark.unit

FIXTURE_PACKS_DIR = Path(__file__).resolve().parents[1] / "fixtures"

# Generous on purpose (see module docstring) -- overridable for a slower CI
# runner, same knob shape as the live benchmark's P95_LATENCY_SLO_MS.
DISPATCH_P95_MS = float(os.environ.get("MCP_DISPATCH_P95_MS", "50.0"))

WARMUP_ITERATIONS = 20
MEASURED_ITERATIONS = 200


def _mock_search_response() -> SearchResponse:
    return SearchResponse(
        results=[
            SearchResult(
                chunk_id="chunk-1",
                document_id="doc-1",
                document_name="report.pdf",
                content="Paris is the capital of France.",
                score=0.91,
                score_source="vector",
                source_uri="s3://bucket/report.pdf",
                source_url="https://drive.example/report.pdf",
                content_hash="abc123",
            )
        ],
        query="q",
        total_results=1,
        processing_time_ms=0.1,
        search_mode="semantic",
    )


async def _dispatch(server, name: str, arguments: dict) -> float:
    """Invoke the real call_tool handler once; return elapsed ms."""
    req = CallToolRequest(
        method="tools/call",
        params=CallToolRequestParams(name=name, arguments=arguments),
    )
    handler = server.request_handlers[CallToolRequest]
    started = time.perf_counter()
    await handler(req)
    return (time.perf_counter() - started) * 1000.0


def _report(label: str, latencies: list[float]) -> None:
    summary = summarize(latencies, wall_time_s=1.0)  # wall unused for this assert
    print(
        f"\nMCP dispatch overhead ({label}) over {summary.count} calls: "
        f"p50={summary.p50_ms:.3f}ms p95={summary.p95_ms:.3f}ms "
        f"max={summary.max_ms:.3f}ms min={summary.min_ms:.3f}ms"
    )
    assert summary.p95_ms < DISPATCH_P95_MS, (
        f"MCP dispatch overhead ({label}) p95 {summary.p95_ms:.3f}ms exceeded "
        f"generous ceiling {DISPATCH_P95_MS}ms -- see this module's docstring"
    )


# --------------------------------------------------------------------------- #
# Built-in tool over stdio: search_documents
# --------------------------------------------------------------------------- #


class TestSearchDocumentsDispatchOverhead:
    async def test_p95_dispatch_overhead_is_within_ceiling(self) -> None:
        mock_db = AsyncMock()
        key_info = APIKeyInfo(
            key_id="key-bench",
            user_id="user-bench",
            workspace_id=None,
            permissions=["search"],
            rate_limit=100,
            expires_at=None,
            status="active",
        )
        mock_db.validate_api_key = AsyncMock(return_value=key_info)
        mock_db.get_user_workspace_ids = AsyncMock(return_value=["ws-bench"])

        mock_search = AsyncMock()
        mock_search.search = AsyncMock(return_value=_mock_search_response())

        server = mcp_server.create_mcp_server()
        arguments = {"api_key": "k", "query": "what is the capital of France"}

        with (
            patch.object(mcp_server, "get_database", AsyncMock(return_value=mock_db)),
            patch.object(mcp_server, "get_search_service", AsyncMock(return_value=mock_search)),
            # Capture (#241) shares the real path -- point its own `get_database`
            # import at the same mock (two independent module bindings, see
            # tests/unit/test_mcp_search_capture.py's `_mcp_patches`).
            patch("src.services.eval_capture.get_database", new=AsyncMock(return_value=mock_db)),
            # Audit publishing is fire-and-forget (never blocks dispatch either
            # way) -- stubbed here purely to avoid a real Redis connection
            # attempt racing in the background during the timing loop; the
            # "build the event" cost this benchmark cares about still runs
            # (see audit.py's `_publish`, called before `_fire_and_forget_publish`).
            patch.object(mcp_audit, "publish_audit_event", AsyncMock(return_value=None)),
        ):
            for _ in range(WARMUP_ITERATIONS):
                await _dispatch(server, "search_documents", dict(arguments))

            latencies = [
                await _dispatch(server, "search_documents", dict(arguments))
                for _ in range(MEASURED_ITERATIONS)
            ]

        _report("search_documents/stdio", latencies)


# --------------------------------------------------------------------------- #
# Vertical-pack tool profile over Streamable HTTP: search_sections
# --------------------------------------------------------------------------- #


class TestToolProfileDispatchOverhead:
    async def test_p95_dispatch_overhead_is_within_ceiling(self) -> None:
        workspace_pack_module._discover_packs_cached.cache_clear()

        fake_settings = Settings(
            _env_file=None,
            vertical_packs_dir=str(FIXTURE_PACKS_DIR),
            workspace_vertical_packs_raw="ws-bench=handbook",
        )

        key_info = APIKeyInfo(
            key_id="key-bench",
            user_id="user-bench",
            workspace_id="ws-bench",
            permissions=["search"],
            rate_limit=100,
            expires_at=None,
            status="active",
        )

        mock_db = AsyncMock()
        # Workspace-scoped key (#138): resolved via the Mongo membership
        # check, not `get_user_workspace_ids` -- see auth.py's
        # `get_authorized_workspace_ids` docstring.
        mock_db.user_owns_workspace_in_mongo = AsyncMock(return_value=True)

        mock_search = AsyncMock()
        mock_search.search = AsyncMock(return_value=_mock_search_response())

        http_server = http_transport.create_http_mcp_server()
        arguments = {"query": "pricing"}

        with (
            patch("src.services.workspace_pack.settings", fake_settings),
            patch.object(http_transport, "get_database", AsyncMock(return_value=mock_db)),
            patch.object(tool_profiles, "get_search_service", AsyncMock(return_value=mock_search)),
            patch.object(mcp_audit, "publish_audit_event", AsyncMock(return_value=None)),
        ):
            token = http_transport._current_key_info.set(key_info)
            try:
                for _ in range(WARMUP_ITERATIONS):
                    await _dispatch(http_server, "search_sections", dict(arguments))

                latencies = [
                    await _dispatch(http_server, "search_sections", dict(arguments))
                    for _ in range(MEASURED_ITERATIONS)
                ]
            finally:
                http_transport._current_key_info.reset(token)

        _report("search_sections (tool profile)/http", latencies)
