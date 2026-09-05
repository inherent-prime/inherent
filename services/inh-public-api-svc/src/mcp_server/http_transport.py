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

OAuth 2.1 resource-server support (#295)
-----------------------------------------
Flag-gated by ``settings.oauth_enabled`` (default False -- see
``src/config/settings.py``). ``mount_mcp_http``'s ASGI auth gate now
dispatches on credential SHAPE: an ``X-API-Key`` header or an
``Authorization: Bearer ink_...`` value still resolves through the
UNCHANGED ``get_api_key_info`` path above; only an ``Authorization: Bearer
<non-ink_ token>`` value, and only while OAuth is enabled, is verified as an
OAuth access token instead (``src.services.auth.verify_oauth_token``). With
the flag off, this module's behavior -- including every 401's
``WWW-Authenticate`` header -- is byte-identical to before #295; see
``tests/unit/test_oauth_config_gate.py``. See ``src/services/auth.py``'s
"OAuth 2.1 resource-server support" section for the ``Principal`` /
``TokenValidationError`` / challenge-building pieces this module wires in.

Per-identity entitlements and quotas (#309)
--------------------------------------------
Between the permission/scope check and ``tool.handler`` dispatch in both
``call_tool`` and ``_call_tool_oauth``, ``quotas.check_quota`` is given the
same ``Principal`` #295 already resolved and answers "has this identity got
budget left". A denial short-circuits BEFORE the handler runs, in the same
``isError=True`` ``CallToolResult`` shape (``structuredContent.error_class``)
every other rejection on this transport already uses -- see
``_quota_exceeded_result`` and ``FAILURE_CLASS_QUOTA_EXCEEDED`` below, and
``src/mcp_server/quotas.py`` for the enforcement logic, its fail-open (infra
failure) vs. fail-closed (genuine exhaustion) split, and why an identity
with no entitlements configured touches neither the rate limiter nor the
database (default-open, unchanged behavior for every caller today).
"""

from __future__ import annotations

import copy
from contextvars import ContextVar
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, status
from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.types import CallToolResult, TextContent, Tool
from starlette.requests import Request
from starlette.types import Receive, Scope, Send

from src.config import settings
from src.mcp_server.quotas import QuotaDenial, check_quota, publish_usage_event
from src.mcp_server.server import _TOOLS, ToolDef, current_mcp_endpoint
from src.models.api_key import APIKeyInfo
from src.services.auth import (
    PERMISSION_SCOPE_MAP,
    Principal,
    TokenValidationError,
    build_www_authenticate,
    get_api_key_info,
    get_authorized_workspace_ids,
    verify_oauth_token,
)
from src.services.database import get_database
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

# Carries an OAuth-authenticated caller (#295) alongside `_current_key_info`
# above -- a SEPARATE contextvar rather than widening `_current_key_info`'s
# type, so every existing test that sets `_current_key_info` directly to a
# plain `APIKeyInfo` (see tests/contract/test_mcp_http_transport.py's
# `_call_http_tool` helper) keeps working completely unedited. Exactly one
# of the two is ever set on an authenticated request; `call_tool` below
# checks `_current_key_info` first (unchanged code path) and falls back to
# this one only when that is None.
_current_oauth_principal: ContextVar[Principal | None] = ContextVar(
    "mcp_http_oauth_principal", default=None
)

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
# A principal is over one of its per-identity entitlements/quotas (#309) --
# see quotas.py's module docstring for the fail-open/fail-closed split this
# class is only ever emitted on the fail-CLOSED (genuine exhaustion) side of.
FAILURE_CLASS_QUOTA_EXCEEDED = "quota_exceeded"


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


def _quota_exceeded_result(denial: QuotaDenial) -> CallToolResult:
    """``isError=True`` result for a per-identity quota rejection (#309).

    Deliberately the SAME shape ``_call_tool_oauth``'s ``insufficient_scope``
    result already uses (design constraint #3: "Follow whatever error shape
    http_transport.py already uses; do not invent a second one") --
    ``isError=True`` at HTTP 200 (see ``_call_tool_oauth``'s docstring for
    why no other HTTP status is reachable from inside ``tools/call``: the
    same ``StreamableHTTPSessionManager(json_response=True)`` constraint
    applies here, not just to OAuth scope denial), a human-readable
    ``content`` message, and a branchable ``structuredContent`` naming the
    limit, its value, when it resets (``None``/omitted for ``max_documents``,
    which has no time window -- see ``QuotaDenial``'s docstring), and the
    operator's upgrade URL when configured -- everything #309's "Behaviour on
    exhaustion" section asks for.
    """
    message = f"Error: '{denial.limit_name}' limit exceeded (limit: {denial.limit})"
    if denial.reset_at is not None:
        reset_iso = datetime.fromtimestamp(denial.reset_at, tz=timezone.utc).isoformat()
        message += f"; resets at {reset_iso}"
    if denial.upgrade_url:
        message += f". Raise this limit: {denial.upgrade_url}"

    structured: dict = {
        "error_class": FAILURE_CLASS_QUOTA_EXCEEDED,
        "limit": denial.limit_name,
        "limit_value": denial.limit,
        "reset_at": denial.reset_at,
    }
    if denial.upgrade_url:
        structured["upgrade_url"] = denial.upgrade_url

    return CallToolResult(
        content=[TextContent(type="text", text=message)],
        structuredContent=structured,
        isError=True,
    )


async def _workspace_ids_for_quota(key_info: APIKeyInfo) -> list[str]:
    """Lazily resolve the workspace set an API-key ``max_documents`` check
    should count against -- a small async helper (rather than inlining the
    two awaits at the ``check_quota`` call site) so it can be handed to
    ``check_quota`` as a zero-arg callable and only ever actually run when a
    ``max_documents`` limit is configured (see ``quotas._check_max_documents``'s
    docstring for why that laziness matters for the default-open path)."""
    database = await get_database()
    return await get_authorized_workspace_ids(key_info, database)


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
        if key_info is None:
            # Not an API-key caller -- check the OAuth contextvar (#295)
            # before falling back to the "nothing authenticated" defensive
            # branch. Kept as a separate branch (not merged into the
            # code below) so the API-key path -- and every test pinned to
            # it -- is untouched byte-for-byte.
            oauth_principal = _current_oauth_principal.get()
            if oauth_principal is not None:
                return await _call_tool_oauth(name, oauth_principal)

            # pragma: no cover - defensive; the ASGI gate always sets one of
            # the two contextvars above before handle_request ever runs.
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

        # Per-identity entitlement/quota enforcement (#309), keyed off the
        # SAME Principal seam #295 introduced -- runs after permission (an
        # unauthorized call was never going anywhere near budget) and before
        # dispatch (a quota-exceeded call must never reach tool.handler).
        # See quotas.py's module docstring for the fail-open/fail-closed
        # split and why this costs an unlimited (default) principal nothing.
        principal = Principal.from_api_key(key_info)
        denial = await check_quota(
            principal,
            name,
            tool.permission,
            workspace_ids_for_max_documents=lambda: _workspace_ids_for_quota(key_info),
        )
        if denial is not None:
            publish_usage_event(principal, name, allowed=False)
            return _quota_exceeded_result(denial)

        try:
            content = await tool.handler(key_info, arguments)
        except Exception as exc:  # noqa: BLE001 - must not crash the transport
            logger.error("MCP HTTP tool error", tool=name, error=str(exc))
            return _error_result(f"Error: {exc}", FAILURE_CLASS_INTERNAL)

        publish_usage_event(principal, name, allowed=True)

        if content and isinstance(content[0], TextContent) and content[0].text.startswith("Error:"):
            return _error_result(content[0].text, _classify_handler_error(content[0].text))

        return CallToolResult(content=content, isError=False)

    return server


async def _call_tool_oauth(name: str, principal: Principal) -> CallToolResult:
    """OAuth-authenticated `call_tool` dispatch (#295).

    Scope-checks the call against the token's granted scopes -- the same
    per-tool enforcement point the API-key path uses
    (`key_info.has_permission`), just keyed on `PERMISSION_SCOPE_MAP`
    instead. A token missing the tool's required scope gets the spec's
    `insufficient_scope` shape -- `structuredContent` carries
    `error="insufficient_scope"` and the `scope` a client would need to
    request on its next authorization attempt -- but as a JSON-RPC-level
    `CallToolResult(isError=True)` at HTTP 200, NOT a transport-level 403.

    That is a deliberate, unavoidable divergence from #295's literal
    acceptance-criteria wording ("returns 403 with error=insufficient_scope"),
    not an oversight: this function runs INSIDE the MCP SDK's low-level
    `Server`'s message dispatch, already deep in one JSON-RPC request/response
    cycle by the time it is called (the scope needed depends on `name`, which
    only exists inside the parsed `tools/call` body -- it cannot be known
    before that point). With this transport mounted `json_response=True`,
    `StreamableHTTPServerTransport._handle_post_request` always wraps the
    response to a parsed JSON-RPC request in HTTP 200 via
    `_create_json_response(response_message)` (no `status_code` override
    parameter is threaded through from a tool handler) -- and the low-level
    `Server`'s own request loop catches any exception raised from within a
    `call_tool` handler and folds it into a JSON-RPC error, never an ASGI
    response of its own. So this dispatcher has no mechanism to make the
    surrounding HTTP response anything other than 200, and raising instead of
    returning would only trade a clear `isError=True` result for a generic
    JSON-RPC INTERNAL_ERROR -- worse for the client, not better. A true
    RFC 6750 403 challenge stays reserved for CONNECTION-level rejection
    (missing/invalid/expired bearer -- see `_oauth_401` / `mcp_asgi_app`
    below), which runs before the session manager ever parses the body and
    can therefore still raise a real `HTTPException` the ASGI stack turns
    into a genuine HTTP status. Per-tool scope is not a connection property,
    so it cannot use that path. `docs/reference/mcp-tools.md` and
    `src/config/settings.py` describe this actual shape, not a 403 that never
    fires; see `tests/security/test_oauth_token_validation.py::
    TestInsufficientScope` for the pinned status/body/headers.

    Deliberately stops there rather than invoking `tool.handler`: every
    handler in `server.py` takes an `APIKeyInfo` and, through it, a
    `user_id`/`workspace_id` this repo has no way to derive from an OAuth
    token yet -- mapping the token's `sub` (Clerk identity) to an Inherent
    user/workspace needs the account link issue #295 explicitly scopes OUT
    ("the resource-server half only"; identity/entitlement resolution is
    #309's territory). Executing a tool against a fabricated or unscoped
    identity would be the exact fail-OPEN issue #295's own comments warn
    against, so a validated-but-unresolvable OAuth caller gets a clear,
    honest rejection instead of a guessed workspace.
    """
    tool = _http_tools().get(name)
    if tool is None:
        # Same undifferentiated message as the API-key path (see the
        # comment on that branch above) -- whether the tool never existed or
        # is HTTP-excluded is not something either caller should be able to
        # probe for.
        return _error_result(f"Error: Unknown tool '{name}'", FAILURE_CLASS_UNKNOWN_TOOL)

    required_scope = PERMISSION_SCOPE_MAP.get(tool.permission, tool.permission)
    if not principal.has_scope(required_scope):
        return CallToolResult(
            content=[
                TextContent(
                    type="text",
                    text=f"Error: token missing required scope '{required_scope}'",
                )
            ],
            structuredContent={
                "error_class": FAILURE_CLASS_AUTHORIZATION,
                "error": "insufficient_scope",
                "scope": required_scope,
            },
            isError=True,
        )

    # Per-identity quota enforcement (#309) applies to the OAuth principal
    # exactly as it does to an API-key one -- both are the same `Principal`
    # seam. No `workspace_ids_for_max_documents` provider is available here:
    # OAuth callers have no workspace resolution yet (see this function's own
    # docstring on why it stops before `tool.handler`), so a configured
    # `max_documents` limit fails OPEN with a loud log for this path rather
    # than silently never firing (see `quotas._check_max_documents`). The
    # other three limits (calls_per_minute/calls_per_month/writes_per_day)
    # need no workspace context and are fully enforced here, ready for the
    # day OAuth execution itself lands without further changes to this call.
    denial = await check_quota(principal, name, tool.permission)
    if denial is not None:
        publish_usage_event(principal, name, allowed=False)
        return _quota_exceeded_result(denial)

    return _error_result(
        "Error: OAuth-authenticated tool execution is not yet available -- "
        "identity resolution for bearer tokens is tracked separately from "
        "#295's resource-server auth contract",
        FAILURE_CLASS_AUTHENTICATION,
    )


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


def _looks_like_oauth_bearer(authorization: str | None) -> bool:
    """True for an `Authorization: Bearer <token>` header whose token is NOT
    an Inherent API key (#295's credential-shape dispatch).

    API keys are always issued with the `ink_` prefix (see
    `Makefile`'s `DEV_API_KEY`); a client is free to send one via either
    `X-API-Key` or `Authorization: Bearer ink_...` and both must keep
    resolving through the unchanged `get_api_key_info` path -- only a
    `Bearer` credential that does NOT start with `ink_` is treated as an
    OAuth access token.
    """
    if not authorization or not authorization.startswith("Bearer "):
        return False
    token = authorization[len("Bearer ") :]
    return not token.startswith("ink_")


def _resource_metadata_url(request: Request) -> str:
    """Absolute URL of this deployment's RFC 9728 metadata document,
    derived from the live request's own scheme+host rather than a fixed
    setting -- so the SAME challenge is correct in dev/staging/prod without
    a second base-URL knob that could drift from `error_base_url` (#222) or
    from whatever host the client actually reached."""
    return f"{str(request.base_url).rstrip('/')}/.well-known/oauth-protected-resource"


def _oauth_401(request: Request, *, error: str) -> HTTPException:
    """401 for a rejected OAuth bearer credential (#295).

    `detail` is a generic message -- never anything derived from the
    token or from a PyJWT exception's own text (see
    `verify_oauth_token`'s docstring on why that's never safe to surface).
    """
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired bearer token",
        headers={
            "WWW-Authenticate": build_www_authenticate(_resource_metadata_url(request), error=error)
        },
    )


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
        x_api_key = request.headers.get("x-api-key")
        authorization = request.headers.get("authorization")

        key_info: APIKeyInfo | None = None
        oauth_principal: Principal | None = None

        # Dispatch on credential SHAPE (#295, design constraint #5):
        # `X-API-Key` / `Authorization: Bearer ink_...` -> the existing
        # path, byte-for-byte unchanged below; `Authorization: Bearer
        # <anything else>`, only when OAuth is enabled -> the new OAuth
        # path. Everything else (including EVERY case when
        # `settings.oauth_enabled` is False) falls through to the original
        # `get_api_key_info` call unchanged -- this `if` is the ENTIRE
        # surface through which OAuth can affect behavior, so "oauth
        # disabled" really is "this module behaves exactly as it did before
        # #295" by construction, not by care taken elsewhere.
        if settings.oauth_enabled and _looks_like_oauth_bearer(authorization):
            assert authorization is not None  # narrowed by _looks_like_oauth_bearer
            token_str = authorization[len("Bearer ") :]
            try:
                claims = await verify_oauth_token(token_str)
            except TokenValidationError as exc:
                # `exc.reason` (e.g. "token_expired", "invalid_audience") is
                # for server-side diagnosis only -- see
                # `TokenValidationError`'s docstring for why it never reaches
                # the client. Logged as a bare reason code, never with the
                # token or any exception message that might echo it.
                logger.warning("oauth_token_rejected", reason=exc.reason)
                raise _oauth_401(request, error="invalid_token") from None
            oauth_principal = Principal.from_oauth_claims(claims)
        else:
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
            try:
                key_info = await get_api_key_info(
                    x_api_key=x_api_key,
                    authorization=authorization,
                )
            except HTTPException as exc:
                # Advertise BOTH supported schemes on a 401 when OAuth is
                # enabled (#295, design constraint #1) -- never silently
                # replacing the `ApiKey` challenge REST/stdio clients
                # already rely on, just adding `Bearer` alongside it. `detail`
                # (the human-readable message) is untouched; only the
                # `WWW-Authenticate` header value changes, and only when
                # OAuth is enabled -- with it disabled this `except` block
                # re-raises `exc` completely unmodified, so the 401 stays
                # byte-identical to pre-#295 behavior.
                if settings.oauth_enabled and exc.status_code == status.HTTP_401_UNAUTHORIZED:
                    exc.headers = {
                        **(exc.headers or {}),
                        "WWW-Authenticate": build_www_authenticate(_resource_metadata_url(request)),
                    }
                raise

        token = _current_key_info.set(key_info)
        oauth_token = _current_oauth_principal.set(oauth_principal)
        endpoint_token = current_mcp_endpoint.set(str(request.base_url).rstrip("/"))
        try:
            await session_manager.handle_request(scope, receive, send)
        finally:
            current_mcp_endpoint.reset(endpoint_token)
            _current_key_info.reset(token)
            _current_oauth_principal.reset(oauth_token)

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
