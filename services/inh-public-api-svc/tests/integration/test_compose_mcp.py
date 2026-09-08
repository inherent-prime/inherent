"""Live MCP end-to-end suite: Streamable HTTP + stdio against the compose stack.

The MCP surface (``src/mcp_server/``) had contract coverage only -- every
existing test drives ``create_mcp_server()`` / ``create_http_mcp_server()``'s
handlers directly with mocked ``get_database`` / ``get_search_service``, or
through ``TestClient`` against an in-process app. Nothing had ever pointed a
REAL MCP client at a RUNNING stack. This file is that client:

- **HTTP**: the ``mcp`` SDK's ``streamablehttp_client`` opens a genuine
  Streamable-HTTP session against ``POST {API_URL}/mcp`` -- the same one line
  ``claude mcp add --transport http`` produces -- and drives
  initialize -> tools/list -> tools/call over the wire, through the whole
  ASGI middleware stack (auth, rate limiting, audit logging).
- **stdio**: the SDK's in-memory client/server session
  (``mcp.shared.memory.create_connected_server_and_client_session``) is
  connected to the real ``create_mcp_server()`` object with settings pointed
  at the compose-published backend ports, so its handlers execute against the
  same live Postgres / Mongo / Weaviate / TEI the HTTP tests use. In-memory
  streams replace the pipe, NOT the backends: nothing here is mocked.

This test is marked ``compose`` and is deselected by the default pytest run
(see ``addopts`` in pyproject). Run it against a live stack with::

    make dev            # or: make quickstart
    uv run pytest tests/integration/test_compose_mcp.py -v --no-cov

Configuration (all have local defaults; override via env):
    PUBLIC_API_URL            default http://localhost:18000
    INTEGRATION_API_KEY       default ink_dev_local_key_001
    INTEGRATION_WORKSPACE_ID  default ws_local_001
    INTEGRATION_TIMEOUT       seconds to wait for ingestion (default 180)
    DATABASE_URL / MONGODB_URI / WEAVIATE_URL / EMBEDDING_SERVICE_URL
                              backends for the in-process stdio server; the
                              defaults are docker-compose.yml's PUBLISHED host
                              ports (see ``_LIVE_BACKENDS``).
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager

import httpx
import pytest
from mcp import ClientSession

# ``streamablehttp_client``, not the newer ``streamable_http_client`` alias it
# emits a DeprecationWarning in favour of: this service pins ``mcp>=1.1.2,<2``,
# and the new name does not exist across that whole range. The old name is
# still exported and functional in the installed 1.25.0. Switch when the floor
# of the pin moves past the rename.
from mcp.client.streamable_http import streamablehttp_client
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import CallToolResult, TextContent

from src.config.settings import settings as app_settings
from src.mcp_server.server import create_mcp_server

pytestmark = [pytest.mark.compose, pytest.mark.integration, pytest.mark.slow]

API_URL = os.environ.get("PUBLIC_API_URL", "http://localhost:18000").rstrip("/")
API_KEY = os.environ.get("INTEGRATION_API_KEY", "ink_dev_local_key_001")
WORKSPACE_ID = os.environ.get("INTEGRATION_WORKSPACE_ID", "ws_local_001")
TIMEOUT = int(os.environ.get("INTEGRATION_TIMEOUT", "180"))

# The mount path is a BARE ``/mcp`` with no trailing slash -- `add_route`, not
# `app.mount` (see the comment at the bottom of http_transport.py's
# ``mount_mcp_http``: a Mount would 307-redirect ``/mcp`` to ``/mcp/``).
MCP_URL = f"{API_URL}/mcp"

# REST headers for the seeding/feedback calls that ride the normal API.
REST_HEADERS = {"X-API-Key": API_KEY, "X-Workspace-Id": WORKSPACE_ID}

# ---------------------------------------------------------------------------
# Tool-surface pins.
#
# THESE TWO LISTS ARE DELIBERATE DUPLICATION OF ``_TOOLS`` / ``_http_tools()``
# AND MUST STAY HARDCODED. Deriving them from the registry (e.g.
# ``sorted(_TOOLS)`` or ``[n for n, t in _TOOLS.items() if t.http_exposed]``)
# would make this test tautological: it would assert that the registry equals
# itself and would keep passing while a tool is silently added, renamed, or
# flipped on/off for HTTP. The whole value here is that registry drift BREAKS
# a live test and forces a human to re-confirm the published surface -- 15
# tools on stdio, 11 of them exposed on HTTP (#220's "10, not 13", plus
# report_feedback which arrived after that issue was filed). If a diff to
# these lists is intentional, update them in the same commit as the registry
# change.
# ---------------------------------------------------------------------------
EXPECTED_STDIO_TOOLS = sorted(
    [
        "delete_document",
        "explain_lineage",
        "get_citations",
        "get_document",
        "get_document_context",
        "get_retrieval_health",
        "list_chunks",
        "list_documents",
        "refresh_stale_source",
        "report_feedback",
        "search_documents",
        "search_memory",
        "upload_document",
        "verify_claim",
        "whoami",
    ]
)

EXPECTED_HTTP_TOOLS = sorted(
    [
        "delete_document",
        "explain_lineage",
        "get_document",
        "get_document_context",
        "get_retrieval_health",
        "list_chunks",
        "list_documents",
        "refresh_stale_source",
        "search_documents",
        "upload_document",
        "whoami",
    ]
)

# Backends for the IN-PROCESS stdio server, defaulting to the host ports
# docker-compose.yml publishes (the containers' own names -- postgres,
# weaviate, ... -- do not resolve from the test runner's host).
_LIVE_BACKENDS: dict[str, str] = {
    "database_url": os.environ.get(
        "DATABASE_URL", "postgresql://postgres:postgres@localhost:15432/knowledge_base"
    ),
    "mongodb_uri": os.environ.get("MONGODB_URI", "mongodb://localhost:27018"),
    "mongodb_db_name": os.environ.get("MONGODB_DB_NAME", "main"),
    "weaviate_url": os.environ.get("WEAVIATE_URL", "http://localhost:18080"),
    "weaviate_api_key": os.environ.get("WEAVIATE_API_KEY", "local-dev-weaviate-key"),
    "embedding_service_url": os.environ.get("EMBEDDING_SERVICE_URL", "http://localhost:18088"),
}

# Fixed (not random) marker so a re-run dedups onto the same document by
# content hash (#75) instead of growing the workspace on every run.
SEED_MARKER = "ZZMCPE2E"
SEED_FILENAME = "mcp-live-e2e-seed.md"
SEED_CONTENT = (
    f"# MCP live end-to-end probe {SEED_MARKER}\n\n"
    f"This document exists so the MCP transports can prove they retrieve real "
    f"indexed content. Marker: {SEED_MARKER}.\n"
)
SEED_QUERY = f"MCP live end-to-end probe {SEED_MARKER}"


def _require_stack(client: httpx.Client) -> None:
    """Skip (don't fail) when no healthy stack is reachable."""
    try:
        resp = client.get(f"{API_URL}/health", timeout=5)
    except httpx.HTTPError as exc:
        pytest.skip(f"public API not reachable at {API_URL}: {exc}")
    if resp.status_code != 200:
        pytest.skip(f"public API unhealthy at {API_URL}: HTTP {resp.status_code}")


@pytest.fixture(scope="module")
def client() -> httpx.Client:
    """REST client, and the skip gate for the whole file.

    Every test takes it -- including the ones whose body only speaks MCP and
    never touches ``c`` -- so that a missing stack SKIPS (the pattern
    ``test_compose_integration.py`` established) instead of failing with a
    connection error from inside an MCP handshake. Do not drop the parameter
    from a test that appears not to use it.
    """
    with httpx.Client(timeout=30) as c:
        _require_stack(c)
        yield c


@pytest.fixture
def live_backend_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Point the process-wide ``settings`` singleton at the compose backends.

    Mutates the singleton OBJECT rather than ``os.environ`` on purpose:
    ``tests/conftest.py`` imports ``src.models.api_key``, which imports
    ``src.config.settings`` and evaluates ``settings = get_settings()`` at
    collection time -- before any test module body runs -- so an
    ``os.environ`` write here would be read by nothing. Every module does
    ``from src.config.settings import settings`` (or calls the ``lru_cache``d
    ``get_settings()``), so they all hold this exact object and see the
    patched values. conftest's autouse ``_reset_service_singletons`` nulls
    ``_database`` / ``_search_service`` around every test, so the services
    this test builds are constructed AFTER the patch and read the new values.

    ``src/services/embedder.py`` is the one component that does NOT read
    ``Settings``: it reads ``os.environ["EMBEDDING_SERVICE_URL"]`` directly
    and memoizes an ``httpx.Client`` in a module global. So it needs BOTH an
    env var and an explicit reset of that global -- patching the settings
    object alone leaves it pointed at the in-network ``text-embeddings-
    inference`` hostname, which does not resolve from the test runner's host
    (found the hard way: ``Error: [Errno 8] nodename nor servname provided``).

    Only the stdio test needs this: the HTTP tests talk to the containerized
    app, which is already configured with the in-network hostnames.
    """
    import src.services.embedder as embedder

    def _drop_embedder_client() -> None:
        """Close, then null, the memoized client.

        Nulling alone would leak the previous ``httpx.Client``'s connection
        pool (sockets stay open until GC finalizes them), and the one on the
        way OUT points at a host port this fixture only made valid for the
        duration of the test.
        """
        if embedder._CLIENT is not None:
            embedder._CLIENT.close()
        embedder._CLIENT = None

    for field, value in _LIVE_BACKENDS.items():
        monkeypatch.setattr(app_settings, field, value)
    monkeypatch.setenv("EMBEDDING_SERVICE_URL", _LIVE_BACKENDS["embedding_service_url"])

    _drop_embedder_client()
    yield
    _drop_embedder_client()


@asynccontextmanager
async def mcp_http_session(api_key: str = API_KEY) -> AsyncIterator[ClientSession]:
    """Open an initialized MCP Streamable-HTTP session against the live stack.

    A plain ``asynccontextmanager`` used inside each test body, NOT a pytest
    fixture -- tried as a fixture first and reverted. ``streamablehttp_client``
    opens an anyio task group; pytest-asyncio finalizes an async generator
    fixture in a DIFFERENT task from the one that ran the test, and anyio
    refuses to close a cancel scope from another task ("RuntimeError: Attempted
    to exit cancel scope in a different task than it was entered in"), so every
    HTTP test errored at teardown even though its body passed. Entering and
    exiting inside one test coroutine keeps the scope in a single task.

    Parameterized by key (rather than closing over ``API_KEY``) so tenancy
    tests can open a session as a different principal without duplicating the
    handshake. The server runs ``stateless=True`` / ``json_response=True``
    (see ``mount_mcp_http``), so every JSON-RPC call is an independent POST
    whose key is re-validated -- there is no server-held session to keep warm
    between calls.

    ``X-API-Key`` is the whole auth story here: the ASGI gate reads
    ``x-api-key`` / ``authorization`` and hands them to REST's OWN
    ``get_api_key_info`` dependency. There is deliberately no
    ``X-Workspace-Id`` -- on MCP the workspace is a tool ARGUMENT, not a
    header.
    """
    async with streamablehttp_client(MCP_URL, headers={"X-API-Key": api_key}) as (
        read_stream,
        write_stream,
        _get_session_id,
    ):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            yield session


def _text(result: CallToolResult) -> str:
    """The single TextContent every MCP handler in this codebase returns."""
    assert result.content, f"tool returned no content: {result}"
    first = result.content[0]
    assert isinstance(first, TextContent), f"expected TextContent, got {type(first)}"
    return first.text


def _structured_payload(result: CallToolResult) -> dict:
    """Parse the ```json block ``server.py::_structured`` appends to its text.

    Decodes with ``json.JSONDecoder.raw_decode`` instead of searching for a
    closing ``` ``` `` fence with ``str.find``: the payload can embed a chunk
    of indexed document content that itself contains a literal triple
    backtick (perfectly legal inside a JSON string), and a naive fence
    search matches THAT occurrence instead of the block's real closing
    fence, truncating the JSON mid-string
    (``json.decoder.JSONDecodeError: Unterminated string``). ``raw_decode``
    parses exactly one JSON value starting at the given offset and reports
    where it ended, so backticks embedded inside JSON string content can
    never be mistaken for a fence.
    """
    text = _text(result)
    start = text.find("```json")
    assert start != -1, f"no structured JSON block in tool output: {text[:400]}"
    body = text[start + len("```json") :].lstrip()
    try:
        parsed, _ = json.JSONDecoder().raw_decode(body)
    except json.JSONDecodeError as exc:
        raise AssertionError(
            f"could not parse structured JSON block: {exc}\nbody[:400]={body[:400]!r}"
        ) from exc
    return parsed["structured"]


def _json_body(result: CallToolResult) -> dict:
    """Parse a handler that returns a bare ``model_dump_json()`` payload."""
    return json.loads(_text(result))


def _search_rest(client: httpx.Client, query: str) -> dict:
    resp = client.post(
        f"{API_URL}/v1/search",
        headers={**REST_HEADERS, "Content-Type": "application/json"},
        json={"query": query, "limit": 10},
    )
    assert resp.status_code == 200, f"search failed: {resp.status_code} {resp.text}"
    return resp.json()


@pytest.fixture(scope="module")
def seeded_document(client: httpx.Client) -> str:
    """Upload one marker document over REST and wait until it is retrievable.

    Seeded over REST rather than over MCP so the MCP retrieval assertions test
    RETRIEVAL, not "MCP can read back what MCP just wrote" -- and so each MCP
    test can run standalone instead of depending on an earlier test in the
    file having uploaded something.
    """
    resp = client.post(
        f"{API_URL}/v1/documents",
        headers=REST_HEADERS,
        files={"file": (SEED_FILENAME, SEED_CONTENT.encode("utf-8"), "text/markdown")},
    )
    assert resp.status_code == 201, f"seed upload failed: {resp.status_code} {resp.text}"
    document_id = resp.json()["document_id"]
    assert document_id

    deadline = time.monotonic() + TIMEOUT
    last: dict = {}
    while time.monotonic() < deadline:
        last = _search_rest(client, SEED_QUERY)
        if document_id in {r["document_id"] for r in last["results"]}:
            return document_id
        time.sleep(3)
    pytest.fail(
        f"seed document {document_id} did not become searchable within {TIMEOUT}s "
        f"(last total_results={last.get('total_results')})"
    )


# ---------------------------------------------------------------------------
# HTTP transport
# ---------------------------------------------------------------------------


# Smoke: the cheapest possible proof that a real MCP client can complete the
# handshake against the deployed app AND that the published tool surface is
# what we say it is. One initialize + one tools/list, no ingestion.
@pytest.mark.smoke
async def test_http_tools_list_pins_exposed_surface(client: httpx.Client) -> None:
    async with mcp_http_session() as session:
        result = await session.list_tools()

    assert sorted(tool.name for tool in result.tools) == EXPECTED_HTTP_TOOLS

    # The header-auth contract: no HTTP schema may advertise ``api_key``, or an
    # agent will hunt for the secret and echo it into its context (#220).
    for tool in result.tools:
        properties = (tool.inputSchema or {}).get("properties") or {}
        assert "api_key" not in properties, f"{tool.name} advertises api_key over HTTP"


# Smoke: the ingestion->retrieval path AS AN MCP AGENT SEES IT. The REST
# equivalent (test_compose_integration.py) cannot catch an MCP-only break --
# a broken tool schema, a dispatcher regression, or a transport-framing bug --
# because it never speaks MCP.
@pytest.mark.smoke
async def test_http_search_documents_roundtrip(client: httpx.Client, seeded_document: str) -> None:
    async with mcp_http_session() as session:
        result = await session.call_tool(
            "search_documents",
            {"query": SEED_QUERY, "workspace_id": WORKSPACE_ID, "limit": 10},
        )
    assert result.isError is False, f"search_documents failed: {_text(result)}"

    payload = _structured_payload(result)
    assert payload["workspaces_searched"] == [WORKSPACE_ID]

    hits = [r for r in payload["results"] if r["document_id"] == seeded_document]
    assert hits, (
        f"seeded document {seeded_document} not cited in MCP search results "
        f"(got {[r['document_id'] for r in payload['results']]})"
    )
    # The CONTENT, not just the id: proves the tool returns the indexed text an
    # agent would quote, not an empty shell with a matching id.
    assert any(SEED_MARKER in hit["content"] for hit in hits)
    assert SEED_MARKER in _text(result)


async def test_http_upload_get_delete_document_roundtrip(client: httpx.Client) -> None:
    """Write path over MCP: upload -> processed -> delete -> gone.

    Everything here goes through the tools, never REST -- an MCP-only agent
    (``claude mcp add --transport http``, no database credentials) must be able
    to complete the whole document lifecycle on its own.
    """
    async with mcp_http_session() as session:
        upload = await session.call_tool(
            "upload_document",
            {
                "filename": "mcp-lifecycle-probe.md",
                "content": (
                    "# MCP lifecycle probe ZZMCPLIFE\n\nUploaded, polled, and deleted "
                    "entirely over the MCP HTTP transport.\n"
                ),
                "workspace_id": WORKSPACE_ID,
            },
        )
        assert upload.isError is False, f"upload_document failed: {_text(upload)}"
        document_id = _json_body(upload)["document_id"]
        assert document_id

        # Poll get_document (not REST) until ingestion finishes.
        deadline = time.monotonic() + TIMEOUT
        status: str | None = None
        while time.monotonic() < deadline:
            fetched = await session.call_tool("get_document", {"document_id": document_id})
            assert fetched.isError is False, f"get_document failed: {_text(fetched)}"
            status = _json_body(fetched)["status"]
            if status == "processed":
                break
            await asyncio.sleep(3)
        assert status == "processed", (
            f"document {document_id} did not reach 'processed' within {TIMEOUT}s "
            f"(last status={status})"
        )

        deleted = await session.call_tool("delete_document", {"document_id": document_id})
        assert deleted.isError is False, f"delete_document failed: {_text(deleted)}"
        delete_payload = _structured_payload(deleted)
        assert delete_payload["deleted"] is True
        assert delete_payload["document_id"] == document_id

        # The document is gone AND the failure is branchable (#216): isError=True
        # plus a machine-readable error_class, not just prose an agent must regex.
        missing = await session.call_tool("get_document", {"document_id": document_id})

    assert missing.isError is True, f"deleted document still readable: {_text(missing)}"
    assert missing.structuredContent == {"error_class": "not_found"}
    assert "not found" in _text(missing).lower()


# ---------------------------------------------------------------------------
# stdio transport
# ---------------------------------------------------------------------------


async def test_stdio_surface_and_search(live_backend_settings: None, seeded_document: str) -> None:
    """The stdio server, in-process, against the live backends.

    ``create_connected_server_and_client_session`` swaps the pipe for
    in-memory streams; the server object is the real ``create_mcp_server()``
    and its handlers hit the same Postgres / Mongo / Weaviate / TEI the HTTP
    tests do (see ``live_backend_settings``). stdio keeps its own contract:
    all 14 tools (``http_exposed`` is ignored here) and ``api_key`` as a tool
    ARGUMENT rather than a header.
    """
    server = create_mcp_server()
    async with create_connected_server_and_client_session(server) as session:
        listed = await session.list_tools()
        assert sorted(tool.name for tool in listed.tools) == EXPECTED_STDIO_TOOLS

        # The mirror image of the HTTP assertion: stdio has no headers, so
        # ``api_key`` MUST stay in the advertised schema here. Pinning both
        # directions is what catches ``_strip_api_key`` mutating the shared
        # registry dict in place -- which would silently delete stdio's only
        # way to authenticate while the HTTP test stayed green.
        by_name = {tool.name: tool for tool in listed.tools}
        assert "api_key" in by_name["search_documents"].inputSchema["properties"]
        assert "api_key" in (by_name["search_documents"].inputSchema.get("required") or [])

        result = await session.call_tool(
            "search_documents",
            {
                "api_key": API_KEY,
                "query": SEED_QUERY,
                "workspace_id": WORKSPACE_ID,
                "limit": 10,
            },
        )
        assert result.isError is False, f"stdio search_documents failed: {_text(result)}"
        payload = _structured_payload(result)
        assert seeded_document in {r["document_id"] for r in payload["results"]}, (
            f"seeded document {seeded_document} not returned over stdio "
            f"(got {[r['document_id'] for r in payload['results']]})"
        )


# ---------------------------------------------------------------------------
# Evals flywheel across the MCP/REST seam
# ---------------------------------------------------------------------------


class MissingMcpEventIdError(Exception):
    """Raised at exactly ONE line: the ``event_id`` check below (#241).

    Exists so the xfail on ``test_http_report_feedback_closes_loop`` can be
    SCOPED to the known bug via ``raises=``. A bare ``xfail(strict=True)``
    swallows every call-phase exception, so an MCP search regression (a
    tool returning ``isError=True``), a transport-framing break, or a 500
    from the feedback endpoint would all be reported as a green "xfailed" --
    the known-broken test would silently absorb NEW breakage in the very
    path it exercises. With ``raises`` set, only this one exception counts
    as the expected failure; anything else is a real, loud FAILURE.
    """


@pytest.mark.xfail(
    strict=True,
    raises=MissingMcpEventIdError,
    reason=(
        "#241 (filed, then auto-closed IN ERROR by the #240 fix commit 48cbe72 -- "
        "PR #242's body said 'Closes #240' but the merge commit also closed #241): "
        "no MCP search ever mints an event_id. ``record_query_event`` is called "
        "only from src/api/v1/search.py, so _handle_search's structured payload "
        "carries {query, results, workspaces_searched} and nothing else -- while "
        "report_feedback's schema tells the agent to 'pass the event_id from the "
        "search response'. An MCP-only agent cannot close the evals loop. Remove "
        "this xfail (strict, so it fails loudly) when capture moves into the "
        "shared search path."
    ),
)
async def test_http_report_feedback_closes_loop(client: httpx.Client, seeded_document: str) -> None:
    """An event_id obtained over MCP must be durable enough for REST feedback.

    The #240 seam, one surface over: REST search now awaits the capture INSERT
    before advertising an id, so an id handed to a caller is always usable on
    the next round trip. This asserts the same guarantee holds for an id
    obtained over MCP. ``report_feedback`` itself is stdio-only
    (``http_exposed=False``), so the verdict is posted over REST --
    exactly the cross-surface hop a real agent would have to make.
    """
    async with mcp_http_session() as session:
        result = await session.call_tool(
            "search_documents",
            {"query": SEED_QUERY, "workspace_id": WORKSPACE_ID, "limit": 5},
        )

    # Preconditions. These are plain assertions on purpose: the xfail above is
    # scoped to MissingMcpEventIdError, so an AssertionError here is reported as a
    # genuine failure rather than being absorbed into the known #241 xfail.
    assert result.isError is False, f"search_documents failed: {_text(result)}"
    payload = _structured_payload(result)
    assert payload["results"], f"search returned no results to judge: {payload}"

    event_id = payload.get("event_id")
    if not event_id:
        raise MissingMcpEventIdError(
            "MCP search returned no event_id; report_feedback's schema promises one "
            f"(payload keys: {sorted(payload)})"
        )

    # Only reachable once #241 is fixed; a non-2xx here is a REAL failure (the
    # #240 durability invariant broken on the MCP-minted id), not an xfail.
    resp = client.post(
        f"{API_URL}/v1/evals/feedback",
        headers={**REST_HEADERS, "Content-Type": "application/json"},
        json={"event_id": event_id, "verdict": "answered"},
    )
    assert resp.status_code == 200, f"feedback failed: {resp.status_code} {resp.text}"
