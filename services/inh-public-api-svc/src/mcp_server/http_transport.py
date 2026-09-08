"""Streamable HTTP transport for the MCP tool registry (#220).

The MCP server (``src/mcp_server/server.py``) was reachable only over stdio,
which serves self-hosters and internal development but no SaaS customer (no
production database credentials to run it locally under any packaging). This
module mounts the SAME ``_TOOLS`` registry on Streamable HTTP at ``POST /mcp``
inside this service's existing FastAPI app -- same process, same port as REST
-- so a customer connects with one line and nothing but an API key:

    claude mcp add --transport http inherent https://api.inherent.sh/mcp \\
      --header "X-API-Key: ink_..."

One registry, two transports
-----------------------------
Every tool still lives exactly once, in ``server.py``'s ``_TOOLS``. Nothing in
this module declares a tool description, schema, or handler a second time:

- The advertised surface is ``_TOOLS`` filtered to ``ToolDef.http_exposed``
  (``_http_tools`` below) -- excluding ``verify_claim`` / ``search_memory`` /
  ``get_citations`` (and, pending a follow-up decision, ``report_feedback``;
  see the comments on those ``ToolDef`` entries in ``server.py`` for why each
  is excluded). The exposed subset is therefore DATA on the registry, not a second
  hardcoded name list that could drift from it.
- Every exposed schema is the registry's OWN schema with ``api_key`` stripped
  (``_strip_api_key`` below) -- computed, not hand-duplicated. On HTTP the key
  comes from the ``X-API-Key`` / ``Authorization: Bearer`` header instead (see
  ``mount_mcp_http``'s ASGI auth gate); an agent that sees ``api_key`` in a
  schema will hunt for the secret and may echo it into context, logs, or
  transcripts (the issue's own rationale).
- Every handler is the SAME async function stdio calls. This module never
  reimplements retrieval, document, or upload logic.

stdio (``server.py::run_mcp_server``) is completely unaffected: same
registry, same handlers, same ``api_key``-in-schema contract, same
``list[TextContent]`` / ``isError=False`` convention it has always had.
"""

from __future__ import annotations

import copy
from contextvars import ContextVar

from fastapi import FastAPI
from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.types import CallToolResult, TextContent, Tool
from starlette.requests import Request
from starlette.types import Receive, Scope, Send

from src.mcp_server.server import _TOOLS, ToolDef, current_mcp_endpoint
from src.models.api_key import APIKeyInfo
from src.services.auth import get_api_key_info
from src.utils import get_logger

logger = get_logger(__name__)

# Carries the header-authenticated key from the ASGI auth gate
# (``mount_mcp_http``'s ``mcp_asgi_app``) into the low-level ``Server``'s
# ``call_tool`` dispatcher below. In stateless mode the MCP SDK's session
# manager processes each request in a task IT spawns
# (``StreamableHTTPSessionManager._handle_stateless_request`` ->
# ``task_group.start(run_stateless_server)``) rather than the coroutine that
# awaits ``handle_request`` directly -- but asyncio copies the current
# ``contextvars`` Context when a task is created, so a ``.set()`` in the
# awaiting coroutine (below) IS visible inside the spawned task. Same idiom
# ``src/middleware/request_context.py`` already uses for request-scoped state
# across this app's own middleware stack.
_current_key_info: ContextVar[APIKeyInfo | None] = ContextVar("mcp_http_key_info", default=None)

# Branchable failure classes surfaced in ``CallToolResult.structuredContent``
# (#216: "errors must set isError=True with a branchable failure class").
# Deliberately a small, flat set of strings rather than an exception
# hierarchy: every failure this dispatcher produces already exists as plain
# ``"Error: ..."`` text from the SAME shared handlers stdio calls (see
# ``_classify_handler_error`` below), so the class is metadata ABOUT that
# text, not a new source of truth to keep in sync with it.
FAILURE_CLASS_AUTHENTICATION = "authentication_failed"
FAILURE_CLASS_AUTHORIZATION = "authorization_failed"
FAILURE_CLASS_VALIDATION = "validation_error"
FAILURE_CLASS_UNKNOWN_TOOL = "unknown_tool"
FAILURE_CLASS_NOT_FOUND = "not_found"
FAILURE_CLASS_INTERNAL = "internal_error"
FAILURE_CLASS_TOOL_ERROR = "tool_error"


def _strip_api_key(schema: dict) -> dict:
    """Return a copy of ``schema`` with ``api_key`` removed from
    ``properties`` and ``required`` (#220).

    HTTP callers authenticate via the ``X-API-Key`` / ``Authorization``
    header (the ASGI auth gate in ``mount_mcp_http``), never a tool argument.
    Deep-copied so this NEVER mutates the dict object ``server.py``'s
    ``_TOOLS`` shares with stdio's ``list_tools`` -- an in-place ``pop`` here
    would silently remove ``api_key`` from stdio's advertised schema too,
    since both transports read the exact same registry entries.
    """
    stripped = copy.deepcopy(schema)
    properties = stripped.get("properties")
    if properties is not None:
        properties.pop("api_key", None)
    required = stripped.get("required")
    if required:
        stripped["required"] = [field for field in required if field != "api_key"]
    return stripped


def _http_tools() -> dict[str, ToolDef]:
    """The ``_TOOLS`` subset advertised on HTTP (#220).

    The ONLY place this filter is applied: both ``list_tools`` and
    ``call_tool`` in ``create_http_mcp_server`` below call this, so the
    advertised and enforced HTTP surfaces cannot drift from each other --
    mirroring the same guarantee ``server.py``'s ``_TOOLS`` registry already
    gives stdio (#100).
    """
    return {name: tool for name, tool in _TOOLS.items() if tool.http_exposed}


def _classify_handler_error(text: str) -> str:
    """Best-effort failure class for a handler's ``"Error: ..."`` text (#216).

    Every error path across every handler in ``src/mcp_server/server.py``
    returns a single ``TextContent`` whose text starts with the literal
    ``"Error: "`` prefix (a convention already consistent across that entire
    module) -- there is no richer typed error already flowing out of the
    shared handlers to branch on without rewriting every one of them, which
    stdio does not need and this issue does not ask for. This classifier
    reads that SAME prefix convention: "not found" -> not_found; the
    workspace/permission-denial wordings shared via
    ``describe_workspace_denial`` (and the "does not have '<perm>'
    permission" wording used at the top-level dispatch layer) ->
    authorization_failed; the "required" / "must be" / "cannot be empty"
    family of input-validation messages -> validation_error. Anything else
    still gets ``isError=True`` (the important, tested part of #216) under
    the generic ``tool_error`` class rather than silently falling through.
    """
    lowered = text.lower()
    if "not found" in lowered:
        return FAILURE_CLASS_NOT_FOUND
    if (
        "does not have" in lowered
        or "scoped to workspace" in lowered
        or "don't have access" in lowered
        or "no longer accessible" in lowered
    ):
        return FAILURE_CLASS_AUTHORIZATION
    if "required" in lowered or "must be" in lowered or "cannot be empty" in lowered:
        return FAILURE_CLASS_VALIDATION
    return FAILURE_CLASS_TOOL_ERROR


def _error_result(text: str, failure_class: str) -> CallToolResult:
    """Build an ``isError=True`` result carrying a branchable failure class.

    This is the defect #216 closes for HTTP: stdio's ``call_tool`` returns a
    plain ``list[TextContent]``, which the SDK's own wrapper sets
    ``isError=False`` on regardless of content (unchanged there on purpose --
    see ``server.py``'s module docstring; stdio behavior is out of scope for
    this issue). HTTP callers instead get ``isError=True`` plus the class in
    ``structuredContent`` so an agent can branch programmatically instead of
    string-matching the human-readable text.
    """
    return CallToolResult(
        content=[TextContent(type="text", text=text)],
        structuredContent={"error_class": failure_class},
        isError=True,
    )


def create_http_mcp_server() -> Server:
    """Build the Streamable HTTP MCP server (#220).

    A SEPARATE ``mcp.server.Server`` instance from stdio's
    ``server.create_mcp_server`` -- different auth source (header vs. tool
    argument), different advertised surface, different
    error convention (``isError=True`` + failure class vs. stdio's unchanged
    plain-text errors) -- but built from the EXACT SAME ``_TOOLS`` registry
    via ``_http_tools`` / ``_strip_api_key``. There is no second tool
    declaration anywhere in this module.
    """
    server = Server("inherent-knowledge-base")

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        """Advertise exactly the ``http_exposed`` subset of ``_TOOLS``,
        api_key-free (#220)."""
        return [
            Tool(
                name=name,
                description=tool.description,
                inputSchema=_strip_api_key(tool.input_schema),
            )
            for name, tool in _http_tools().items()
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> CallToolResult:
        """Enforce per-tool permission, dispatch, and classify failures.

        Connection-level authentication already happened in the ASGI auth
        gate (``mount_mcp_http``'s ``mcp_asgi_app``), which populates
        ``_current_key_info`` and rejects a missing/invalid/expired key with
        the SAME 401 REST returns -- before the MCP session manager, and
        therefore before any JSON-RPC framing, ever sees the request. The
        only auth-shaped check left here is PERMISSION, which is per-tool and
        so cannot be enforced any earlier than this (REST parity, #14): a key
        lacking the tool's permission is rejected BEFORE ``tool.handler``
        runs, exactly like stdio's dispatcher and REST's route dependencies.
        """
        key_info = _current_key_info.get()
        if key_info is None:  # pragma: no cover - defensive; the ASGI gate always sets this
            logger.error("MCP HTTP call_tool invoked with no authenticated key in context")
            return _error_result(
                "Error: authentication context missing", FAILURE_CLASS_AUTHENTICATION
            )

        tool = _http_tools().get(name)
        if tool is None:
            # Deliberately the SAME message whether the tool never existed or
            # exists but is HTTP-excluded -- distinguishing them would let a
            # caller probe which tools are hidden vs. absent, the same
            # undifferentiated-error principle
            # ``server.py::_resolve_document_for_user`` applies to
            # cross-workspace document existence (#138).
            return _error_result(f"Error: Unknown tool '{name}'", FAILURE_CLASS_UNKNOWN_TOOL)

        if not key_info.has_permission(tool.permission):
            return _error_result(
                f"Error: API key does not have '{tool.permission}' permission",
                FAILURE_CLASS_AUTHORIZATION,
            )

        try:
            content = await tool.handler(key_info, arguments)
        except Exception as exc:  # noqa: BLE001 - must not crash the transport
            logger.error("MCP HTTP tool error", tool=name, error=str(exc))
            return _error_result(f"Error: {exc}", FAILURE_CLASS_INTERNAL)

        if content and isinstance(content[0], TextContent) and content[0].text.startswith("Error:"):
            return _error_result(content[0].text, _classify_handler_error(content[0].text))

        return CallToolResult(content=content, isError=False)

    return server


class _StreamableHTTPEndpoint:
    """Adapts the raw ``mcp_asgi_app`` coroutine (defined inside
    ``mount_mcp_http`` below) into an object Starlette's ``Route`` recognizes
    as an ASGI app rather than a ``func(request) -> Response`` endpoint.

    ``Route.__init__`` inspects its ``endpoint``: a plain function or method
    is always wrapped via ``request_response()`` (which expects a
    ``Response`` return); only a callable that is NEITHER -- e.g. an instance
    of this class -- is used AS THE ASGI APP DIRECTLY, unwrapped. That is
    what gives the session manager the real ``send`` callable it needs to
    write its own response (see ``mount_mcp_http``'s ``add_route`` call).
    """

    def __init__(self, handler):
        self._handler = handler

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self._handler(scope, receive, send)


def mount_mcp_http(app: FastAPI) -> StreamableHTTPSessionManager:
    """Mount the Streamable HTTP MCP transport at ``POST /mcp`` (#220).

    Design choices, each pinned by a test in
    ``tests/contract/test_mcp_http_transport.py``:

    - ``stateless=True``: every JSON-RPC call (initialize, tools/list,
      tools/call) is one independent HTTP request with no server-held session
      between them. A SaaS customer's key is re-validated on every call
      (immediate effect for revocation/expiry, #180) and the server holds no
      per-client session state that could leak across tenants or grow
      unbounded over a long-lived connection.
    - ``json_response=True``: ``POST /mcp`` returns a single JSON body
      instead of an SSE stream. This app's middleware stack
      (``AuthenticationMiddleware``, ``RateLimitingMiddleware``,
      ``AuditLoggingMiddleware``, ...) is built on Starlette's
      ``BaseHTTPMiddleware``, which is documented to interfere with
      long-lived streaming ASGI responses; a plain JSON response is not
      streaming and is unaffected by that. None of these tools send
      server-initiated notifications, so SSE buys nothing on this surface.
    - Auth is a plain HTTP gate (``mcp_asgi_app`` below), routed through the
      EXACT SAME ``src.services.auth.get_api_key_info`` REST's routes depend
      on, so a missing/invalid/expired key gets the SAME 401 REST returns --
      before the MCP session manager, and therefore before any JSON-RPC
      framing, ever sees the request. Tool-level PERMISSION denial (per-tool,
      not per-connection) is instead an ``isError=True`` ``CallToolResult``
      from ``call_tool`` above.
    - Mounted on the SAME app/port as REST (NOT ``settings.mcp_port``, which
      stays unused): the whole point of #220 is that MCP rides the same ASGI
      middleware stack REST does, so rate limiting (#213), audit logging,
      CORS, and security headers all apply to ``/mcp`` BY CONSTRUCTION rather
      than by a second, hand-maintained copy of each.

    Returns the ``StreamableHTTPSessionManager`` so ``src/main.py``'s
    lifespan can enter its ``.run()`` context -- required before
    ``handle_request`` will accept any request (its task group is ``None``
    until ``.run()`` has been entered).
    """
    http_server = create_http_mcp_server()
    session_manager = StreamableHTTPSessionManager(
        app=http_server,
        json_response=True,
        stateless=True,
    )

    async def mcp_asgi_app(scope: Scope, receive: Receive, send: Send) -> None:
        """Raw ASGI endpoint for ``/mcp``: header auth, then hand off.

        A raw ASGI callable (wrapped as ``_StreamableHTTPEndpoint`` and
        registered via ``add_route`` below, not a FastAPI ``@app.post``
        route) because the session manager needs the actual ``send``
        callable to write its own response -- a normal FastAPI endpoint only
        ever returns a ``Response`` object for Starlette's routing to send on
        its behalf, and cannot itself stream or hold the connection open the
        way the MCP transport controls its own framing.
        """
        request = Request(scope, receive)
        # Reuses REST's OWN dependency function directly (not "equivalent
        # logic re-implemented here") -- called as a plain coroutine with the
        # header values instead of through FastAPI's DI, which is exactly how
        # `Depends(get_api_key_info)` already resolves it on every REST
        # route. This is what makes #138 (workspace-scoped key binding) and
        # #180 (expiry) closed BY CONSTRUCTION for MCP: any future fix to
        # `require_api_key` / `get_api_key_info` applies to both surfaces the
        # moment it lands, with nothing in this module to keep in sync.
        # Raises `fastapi.HTTPException` on a missing/invalid/expired key,
        # which propagates out of this ASGI callable exactly like it would
        # out of a REST route handler -- Starlette's `ExceptionMiddleware`
        # (the SAME handler `setup_exception_handlers` registers for REST)
        # converts it to the SAME RFC 7807 401 response REST returns.
        key_info = await get_api_key_info(
            x_api_key=request.headers.get("x-api-key"),
            authorization=request.headers.get("authorization"),
        )

        token = _current_key_info.set(key_info)
        endpoint_token = current_mcp_endpoint.set(str(request.base_url).rstrip("/"))
        try:
            await session_manager.handle_request(scope, receive, send)
        finally:
            current_mcp_endpoint.reset(endpoint_token)
            _current_key_info.reset(token)

    # `add_route`, NOT `app.mount("/mcp", mcp_asgi_app)` -- tried and
    # reverted. `Mount`'s path regex requires a `/` (or more) AFTER the mount
    # path to match, so the BARE path the issue's install line requires
    # (`POST /mcp`, no trailing slash) never matches a `Mount("/mcp", ...)`;
    # only `/mcp/...` does, and Starlette's router 307-redirects `/mcp` to
    # `/mcp/` to compensate. Most HTTP clients follow a 307 (which preserves
    # method+body), but the issue asks for `/mcp` to work directly, not via
    # an extra redirect round trip. `add_route` matches `/mcp` exactly with
    # no such redirect (see `_StreamableHTTPEndpoint`'s docstring for why the
    # endpoint must be wrapped in a class instance to get there unwrapped).
    app.add_route("/mcp", _StreamableHTTPEndpoint(mcp_asgi_app), methods=["GET", "POST", "DELETE"])
    return session_manager
