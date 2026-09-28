"""Live MCP round-trip latency benchmark over Streamable HTTP (inherent#396).

``test_search_latency_throughput.py`` benchmarks REST's ``POST /v1/search``.
Nothing benchmarked the MCP surface end to end -- the transport a real AI
agent actually uses (``claude mcp add --transport http``) -- even though #396
sets its p95 target ("retrieval via MCP") specifically for that surface.
This file is that benchmark: it opens a genuine Streamable-HTTP session
(the same ``mcp`` SDK client ``tests/integration/test_compose_mcp.py`` uses)
against ``POST {PUBLIC_API_URL}/mcp``, sends real JSON-RPC ``tools/call``
requests, and measures wall-clock round-trip time for:

* ``search_documents`` -- the built-in tool every MCP agent has.
* a vertical-pack tool-profile tool (inherent#392), when this stack's
  benchmark workspace happens to have one bound (see
  ``docs/testing.md``'s "MCP round-trip benchmark" section for how to opt a
  local/CI stack into this) -- skipped, not failed, otherwise, since a pack
  binding is an opt-in per-workspace operator setting with no default.

Same conventions as ``test_search_latency_throughput.py``: loose SLO (this
issue's own 3000ms p95 target, overridable via ``MCP_P95_LATENCY_SLO_MS`` for
a slower CI runner), warm-up pass before measuring, and a merged JSON report
via ``write_benchmark_report`` for CI to pick up as an artifact.

Run against a live stack with::

    make dev
    uv run pytest tests/benchmark/test_mcp_round_trip.py -m 'benchmark and compose' -v --no-cov

OAuth: the compose stack has no real authorization server to mint a verifiable
bearer token against (see ``tests/integration/test_compose_mcp_oauth.py``'s
own docstring -- it stops at the discovery handshake for the identical
reason), so this file cannot exercise a genuine OAuth ``tools/call`` end to
end. If a caller sets ``INTEGRATION_OAUTH_TOKEN`` (a bearer token from a real
IdP pointed at this stack's ``OAUTH_AUTHORIZATION_SERVER``), the OAuth variant
below runs too; otherwise it is skipped with a clear reason rather than
silently omitted.
"""

from __future__ import annotations

import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.types import CallToolResult

from tests.benchmark.run_search_benchmark import summarize, write_benchmark_report

pytestmark = [pytest.mark.benchmark, pytest.mark.compose]

API_URL = os.environ.get("PUBLIC_API_URL", "http://localhost:18000").rstrip("/")
API_KEY = os.environ.get("INTEGRATION_API_KEY", "ink_dev_local_key_001")
WORKSPACE_ID = os.environ.get("INTEGRATION_WORKSPACE_ID", "ws_local_001")
OAUTH_TOKEN = os.environ.get("INTEGRATION_OAUTH_TOKEN")
MCP_URL = f"{API_URL}/mcp"

# #396's own target, loose enough (it's a hard-coded product SLO, not tuned
# per environment) to be overridden for a demonstrably slower CI runner --
# same escape hatch `P95_LATENCY_SLO_MS` in the REST benchmark file uses.
P95_LATENCY_SLO_MS = float(os.environ.get("MCP_P95_LATENCY_SLO_MS", "3000.0"))

# Kept modest (a real network + DB + vector-store round trip per call) --
# large enough for a meaningful p95, small enough not to make `make
# test-benchmark` slow. Matches LATENCY_REQUESTS' order of magnitude in the
# REST benchmark file.
WARMUP_REQUESTS = 3
LATENCY_REQUESTS = 30

QUERY = "what retrieval modes does Inherent support"

BENCHMARK_REPORT = os.environ.get("BENCHMARK_REPORT", "search-benchmark-report.json")


def _require_stack(client: httpx.Client) -> None:
    """Skip (don't fail) when no healthy public API is reachable -- the same
    posture every other compose-marked fixture in this package takes."""
    try:
        resp = client.get(f"{API_URL}/health", timeout=5)
    except httpx.HTTPError as exc:
        pytest.skip(f"public API not reachable at {API_URL}: {exc}")
    if resp.status_code != 200:
        pytest.skip(f"public API unhealthy at {API_URL}: HTTP {resp.status_code}")


@pytest.fixture(scope="module")
def client() -> httpx.Client:
    with httpx.Client(timeout=30) as c:
        _require_stack(c)
        yield c


@asynccontextmanager
async def _mcp_session(
    *, api_key: str | None, bearer: str | None = None
) -> AsyncIterator[ClientSession]:
    """Open an initialized MCP Streamable-HTTP session (mirrors
    ``test_compose_mcp.py``'s ``mcp_http_session`` -- see that helper's
    docstring for why this is a plain contextmanager, not a pytest fixture:
    ``streamablehttp_client``'s cancel scope must be entered and exited in the
    SAME task, which an async-generator fixture cannot guarantee under
    pytest-asyncio).
    """
    headers = {"X-API-Key": api_key} if api_key else {"Authorization": f"Bearer {bearer}"}
    async with streamablehttp_client(MCP_URL, headers=headers) as (read_stream, write_stream, _sid):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            yield session


async def _timed_call_tool(
    session: ClientSession, name: str, arguments: dict
) -> tuple[float, CallToolResult]:
    """One ``tools/call`` JSON-RPC round trip; return (elapsed_ms, result)."""
    started = time.monotonic()
    result = await session.call_tool(name, arguments)
    elapsed_ms = (time.monotonic() - started) * 1000.0
    return elapsed_ms, result


async def _profile_tool_name(session: ClientSession) -> str | None:
    """Return the name of a bound vertical-pack tool-profile tool (inherent#392)
    advertised to this caller, or ``None`` when the benchmark workspace has
    none bound -- see this module's docstring for why that is a skip, not a
    failure. Distinguished from a built-in tool by permission shape: every
    profile tool is search-shaped with no ``api_key``/``workspace_id``
    property (the built-ins all carry ``workspace_id``; profile tools are
    pre-scoped to one workspace, see ``tool_profiles.py``).
    """
    listed = await session.list_tools()
    known_builtins = {
        "whoami",
        "search_documents",
        "list_documents",
        "get_document",
        "list_chunks",
        "get_document_context",
        "explain_lineage",
        "upload_document",
        "delete_document",
        "refresh_stale_source",
        "get_retrieval_health",
        "list_workspaces",
        "create_chunk",
        "edit_chunk",
        "delete_chunk",
        "report_feedback",
    }
    for tool in listed.tools:
        if tool.name not in known_builtins:
            return tool.name
    return None


# --------------------------------------------------------------------------- #
# search_documents over MCP (API-key auth)
# --------------------------------------------------------------------------- #


async def test_mcp_search_documents_round_trip_p95(client: httpx.Client) -> None:
    """Serial round-trip latency over real ``tools/call`` requests; asserts
    #396's p95 target for retrieval via MCP."""

    async def _run() -> list[float]:
        async with _mcp_session(api_key=API_KEY) as session:
            for _ in range(WARMUP_REQUESTS):
                await _timed_call_tool(
                    session, "search_documents", {"query": QUERY, "workspace_id": WORKSPACE_ID}
                )
            latencies = []
            for _ in range(LATENCY_REQUESTS):
                elapsed_ms, result = await _timed_call_tool(
                    session, "search_documents", {"query": QUERY, "workspace_id": WORKSPACE_ID}
                )
                assert result.isError is False, f"search_documents failed: {result.content}"
                latencies.append(elapsed_ms)
            return latencies

    latencies = await _run()
    summary = summarize(latencies, wall_time_s=1.0)  # wall unused for this assert
    print(
        f"\nMCP search_documents round trip over {summary.count} calls: "
        f"p50={summary.p50_ms:.1f}ms p95={summary.p95_ms:.1f}ms "
        f"p99={summary.p99_ms:.1f}ms min={summary.min_ms:.1f}ms max={summary.max_ms:.1f}ms"
    )
    write_benchmark_report(
        BENCHMARK_REPORT,
        "mcp_search_documents_latency",
        {
            "count": summary.count,
            "p50_ms": summary.p50_ms,
            "p95_ms": summary.p95_ms,
            "p99_ms": summary.p99_ms,
            "min_ms": summary.min_ms,
            "max_ms": summary.max_ms,
        },
    )
    assert summary.p95_ms < P95_LATENCY_SLO_MS, (
        f"MCP search_documents p95 latency {summary.p95_ms:.1f}ms exceeded "
        f"#396's SLO {P95_LATENCY_SLO_MS}ms"
    )


# --------------------------------------------------------------------------- #
# A vertical-pack tool-profile tool over MCP (API-key auth) -- skip, not
# fail, when this stack's benchmark workspace has no pack bound.
# --------------------------------------------------------------------------- #


async def test_mcp_tool_profile_round_trip_p95(client: httpx.Client) -> None:
    """Same measurement as above, for a tag-filtered tool-profile tool
    (inherent#392) instead of the built-in ``search_documents`` -- exercises
    pack resolution + the profile's own filter validation on the hot path.

    Skips when ``WORKSPACE_ID`` has no vertical pack bound (see
    ``docs/testing.md``'s "MCP round-trip benchmark" section for how an
    operator opts a stack into this via ``WORKSPACE_VERTICAL_PACKS`` /
    ``VERTICAL_PACKS_DIR``) -- neither this stack's default `make dev`
    compose config nor CI's integration job sets either, so this is expected
    to skip there today; it is here so a stack that DOES bind one gets this
    coverage for free.
    """

    async def _run() -> list[float] | None:
        async with _mcp_session(api_key=API_KEY) as session:
            profile_tool = await _profile_tool_name(session)
            if profile_tool is None:
                return None
            for _ in range(WARMUP_REQUESTS):
                await _timed_call_tool(session, profile_tool, {"query": QUERY})
            latencies = []
            for _ in range(LATENCY_REQUESTS):
                elapsed_ms, result = await _timed_call_tool(session, profile_tool, {"query": QUERY})
                assert result.isError is False, f"{profile_tool} failed: {result.content}"
                latencies.append(elapsed_ms)
            return latencies

    latencies = await _run()
    if latencies is None:
        pytest.skip(
            f"workspace '{WORKSPACE_ID}' has no vertical pack bound (WORKSPACE_VERTICAL_PACKS) "
            "-- no tool-profile tool advertised to benchmark; see this test's docstring"
        )
    summary = summarize(latencies, wall_time_s=1.0)
    print(
        f"\nMCP tool-profile round trip over {summary.count} calls: "
        f"p50={summary.p50_ms:.1f}ms p95={summary.p95_ms:.1f}ms "
        f"p99={summary.p99_ms:.1f}ms min={summary.min_ms:.1f}ms max={summary.max_ms:.1f}ms"
    )
    write_benchmark_report(
        BENCHMARK_REPORT,
        "mcp_tool_profile_latency",
        {
            "count": summary.count,
            "p50_ms": summary.p50_ms,
            "p95_ms": summary.p95_ms,
            "p99_ms": summary.p99_ms,
            "min_ms": summary.min_ms,
            "max_ms": summary.max_ms,
        },
    )
    assert (
        summary.p95_ms < P95_LATENCY_SLO_MS
    ), f"MCP tool-profile p95 latency {summary.p95_ms:.1f}ms exceeded #396's SLO {P95_LATENCY_SLO_MS}ms"


# --------------------------------------------------------------------------- #
# OAuth variant -- only runs when a real bearer token is supplied (see this
# module's docstring for why the compose stack cannot mint one itself).
# --------------------------------------------------------------------------- #


async def test_mcp_search_documents_round_trip_p95_oauth(client: httpx.Client) -> None:
    if not OAUTH_TOKEN:
        pytest.skip(
            "INTEGRATION_OAUTH_TOKEN not set -- no real authorization server available in this "
            "stack to mint a verifiable bearer token (see this module's docstring)"
        )

    async def _run() -> list[float]:
        async with _mcp_session(api_key=None, bearer=OAUTH_TOKEN) as session:
            for _ in range(WARMUP_REQUESTS):
                await _timed_call_tool(
                    session, "search_documents", {"query": QUERY, "workspace_id": WORKSPACE_ID}
                )
            latencies = []
            for _ in range(LATENCY_REQUESTS):
                elapsed_ms, result = await _timed_call_tool(
                    session, "search_documents", {"query": QUERY, "workspace_id": WORKSPACE_ID}
                )
                assert result.isError is False, f"search_documents (oauth) failed: {result.content}"
                latencies.append(elapsed_ms)
            return latencies

    latencies = await _run()
    summary = summarize(latencies, wall_time_s=1.0)
    print(
        f"\nMCP search_documents round trip (OAuth) over {summary.count} calls: "
        f"p50={summary.p50_ms:.1f}ms p95={summary.p95_ms:.1f}ms max={summary.max_ms:.1f}ms"
    )
    write_benchmark_report(
        BENCHMARK_REPORT,
        "mcp_search_documents_latency_oauth",
        {
            "count": summary.count,
            "p50_ms": summary.p50_ms,
            "p95_ms": summary.p95_ms,
            "p99_ms": summary.p99_ms,
            "min_ms": summary.min_ms,
            "max_ms": summary.max_ms,
        },
    )
    assert summary.p95_ms < P95_LATENCY_SLO_MS, (
        f"MCP search_documents (OAuth) p95 latency {summary.p95_ms:.1f}ms exceeded "
        f"#396's SLO {P95_LATENCY_SLO_MS}ms"
    )
