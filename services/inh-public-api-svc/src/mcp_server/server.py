"""MCP Server implementation for AI agent integration.

Tool registry (#100)
--------------------
Every tool is declared exactly once, in the ``_TOOLS`` registry at the bottom
of this module: name -> ToolDef(description, input_schema, permission,
handler). ``list_tools`` and the ``call_tool`` dispatcher both iterate the
registry, so a tool cannot be advertised without being callable, callable
without being advertised, or dispatched without a permission — the four
previously disjoint registration points (permission map, Tool() entry,
dispatch elif, schema) cannot drift.

Permission parity (#14)
-----------------------
Every tool validates the supplied API key and then checks that the key carries
the permission the equivalent REST route requires (see ``src/services/auth.py``
and the per-route dependencies). A key missing the required permission gets a
clear ``Error: ...`` response and the tool body NEVER runs — exactly like the
REST 403 path. Each tool's permission lives on its ``_TOOLS`` entry.

Expiry parity (#180)
---------------------
``call_tool`` calls ``key_info.is_expired()`` itself, immediately after
``validate_api_key`` returns and before any registry/permission lookup —
mirroring REST's ``require_api_key`` (``src/services/auth.py``), which
likewise re-checks expiry rather than trusting the DB layer alone. Do not
remove this check on the assumption that "the DB query already filters
``expires_at``" — that is true of the current Postgres-backed
``DatabaseService`` but is not a contract every implementation is guaranteed
to uphold, and REST does not make that assumption either.

Search-feature parity (#14)
---------------------------
``search_documents`` / ``search_memory`` expose the same knobs as POST
/v1/search (search_mode, document_ids, include_context, context_window,
min_score, alpha) and build the SearchRequest through the shared
``build_search_request`` helper so the two surfaces never drift.

Output convention (#40)
-----------------------
Tools return ``list[TextContent]`` (existing convention). For the memory
primitives the text payload embeds a JSON ``structured`` block so agents can
parse the result deterministically while humans still get a readable summary.

Workspace scoping parity (#138)
--------------------------------
Every tool that needs "which workspaces can this key touch" calls
``src.services.auth.get_authorized_workspace_ids`` — the SAME rule REST's
``_resolve_workspace`` enforces: a workspace-scoped key (``APIKeyInfo.
workspace_id`` set) is bound to exactly that one workspace, never the
owning user's full workspace set. Do NOT call
``database.get_user_workspace_ids`` directly from a tool handler — that was
the #138 defect (a scoped key could reach any workspace its owner owned via
MCP, while REST correctly rejected the identical request with 403).

HTTP transport parity (#220)
-----------------------------
This module is the ONLY place a tool is declared. ``src/mcp_server/http_transport.py``
mounts a Streamable HTTP transport (``POST /mcp`` inside ``inh-public-api-svc``)
that derives its advertised surface from ``_TOOLS`` programmatically — filtering
on ``ToolDef.http_exposed`` and stripping ``api_key`` from every schema (the key
comes from the ``X-API-Key`` / ``Authorization`` header on HTTP, never a tool
argument) — rather than declaring a second copy of any tool. stdio (this
module's ``run_mcp_server``) is completely unaffected by the HTTP transport:
same registry, same handlers, same ``api_key``-in-schema contract. See
``http_transport.py`` for the HTTP-specific auth/permission/error wiring.

Upload parity (#87 Task 3)
---------------------------
``upload_document`` is the MCP counterpart of POST /v1/documents, but TEXT
content only: the tool accepts ``content`` as a UTF-8 string (not raw bytes),
so ``content_type`` must be one of the MCP-eligible types in
``inh_contracts.file_types.FILE_TYPE_REGISTRY`` — every spec whose
``surfaces`` includes ``"mcp"`` (see ``SUPPORTED_TEXT_MIME_TYPES`` below for
the live set derived from it, and ``docs/reference/file-types.md`` for the
generated, exhaustive list; #193 review: do not hardcode a type list here
again — this docstring is a plain string literal, not an f-string, so it
CANNOT be regenerated at import time the way ``SUPPORTED_TEXT_MIME_TYPES``
and the schema description below are; some MCP-eligible types are not
``text/*`` MIME types at all, per the registry's ``surfaces`` field, so
"TEXT content" describes the wire transport, not the declared MIME type).
Omitting ``content_type`` derives it from ``filename``'s
extension when recognized as MCP-eligible, falling back to
``text/plain`` otherwise (see ``_default_upload_content_type``; #208 --
was ``text/markdown`` until #208, which mislabelled Dockerfile/Makefile/
README/.gitignore/archive.tar.gz as markdown). Binary
uploads (PDF, DOCX, PNG, ...) remain REST-only by design — the tool rejects
an unsupported ``content_type`` with a message pointing the caller at
POST /v1/documents. Both surfaces share the exact same
validate/dedup/store/enqueue pipeline via ``src.services.document_intake``.
"""

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from inh_contracts.file_types import (
    explicitly_unsupported_message_for_extension,
    explicitly_unsupported_message_for_mime,
    get_spec_for_extension,
    mcp_mime_types,
    mime_type_for_extension,
)
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from src.config.constants import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE
from src.models.api_key import APIKeyInfo
from src.models.document import (
    DEFAULT_MAX_CHARS,
    MAX_MAX_CHARS,
    MIN_MAX_CHARS,
    windowed_document_context,
)
from src.models.evals import FeedbackRequest
from src.services.auth import describe_workspace_denial, get_authorized_workspace_ids
from src.services.compensation import mark_document_failed_with_retry
from src.services.database import get_database
from src.services.document_intake import intake_document
from src.services.eval_feedback import EventNotFoundError, submit_feedback
from src.services.eval_scorecard import build_scorecard
from src.services.lineage import build_lineage
from src.services.search import (
    SearchService,
    build_search_request,
    get_search_service,
)
from src.services.verify import verify_claim
from src.utils import get_logger

logger = get_logger(__name__)

# A tool handler receives the already-authenticated key and the raw arguments.
ToolHandler = Callable[["APIKeyInfo", dict], Awaitable[list[TextContent]]]

# The text MIME types upload_document accepts, derived from the single
# FILE_TYPE_REGISTRY (#117) instead of a `.startswith("text/")` guess over
# ALLOWED_MIME_TYPES. The registry's explicit `surfaces` field is what marks
# a type MCP-eligible, so this can never drift from intake_document's own
# understanding of the allow-list, and a future type can be text/*-shaped
# without being (or not being) MCP-safe without the two disagreeing.
# Binary types (PDF/DOCX/PNG) stay REST-only by design.
SUPPORTED_TEXT_MIME_TYPES = mcp_mime_types()

# Human-readable rendering of the same list, used ONCE below on the
# `content_type` schema property's description (#193) -- previously a
# hand-typed "text/plain, text/markdown (default), text/csv, text/html"
# string that stopped matching SUPPORTED_TEXT_MIME_TYPES the moment #121/#122/
# #127 added YAML/TOML/XML/code/SRT/VTT (30 types today, several of them not
# text/*-prefixed at all -- e.g. application/typescript, application/x-sh).
# `list_tools` is the surface an MCP agent actually reads before ever opening
# docs/, so this string must be regenerated from the registry, not restated.
# Deliberately NOT also inlined into the tool-level `description` (review
# follow-up, coordinator adversarial pass): both descriptions ship on every
# `list_tools` response, so a second full copy there was ~640 duplicated
# characters of pure context cost with no reader who couldn't already see the
# property description in the same payload.
#
# Deliberately NOT a JSON Schema `enum` either (#193 coordinator review, item
# 6 -- tried and reverted): the MCP SDK's `call_tool` wrapper validates
# arguments against `inputSchema` via jsonschema BEFORE the handler ever
# runs (`validate_input=True` by default, see
# `mcp.server.lowlevel.server.Server.call_tool`). An `enum` restricted to
# SUPPORTED_TEXT_MIME_TYPES would make the framework reject any
# EXPLICITLY-declared out-of-set value -- including the ones this tool
# deliberately accepts at the schema level so its OWN handler can give
# actionable guidance (a legacy `.doc` gets a "convert to .docx" message; a
# binary type like `application/pdf` gets "use POST /v1/documents instead")
# -- with a generic "'application/pdf' is not one of [...]" error instead,
# silently deleting that guidance. Proven by running the existing contract
# suite with `enum` added: `test_legacy_doc_rejected_with_explicit_content_type`,
# `test_binary_content_type_rejected_with_rest_only_message`, and
# `test_unsupported_text_content_type_rejected_at_mcp_boundary` all failed
# this way. A prose list costs the same bytes but does not intercept calls
# before the handler runs.
_SUPPORTED_TEXT_MIME_TYPES_TEXT = ", ".join(SUPPORTED_TEXT_MIME_TYPES)


@dataclass(frozen=True)
class ToolDef:
    """Everything the server needs to know about one MCP tool (#100).

    Declared once in the ``_TOOLS`` registry (bottom of this module, after the
    handlers it references). ``list_tools`` and ``call_tool`` both iterate the
    registry, so advertisement, dispatch, schema, and permission can't drift.
    """

    description: str
    input_schema: dict
    permission: str  # mirrors the REST per-route dependency (#14)
    handler: ToolHandler
    # Whether this tool is advertised on the Streamable HTTP transport (#220).
    # Default True: a tool is on HTTP unless explicitly opted out here, so
    # exclusion is DATA on the registry entry itself (see http_transport.py's
    # ``list_tools``), not a second hardcoded name list that could drift from
    # this one. stdio (this module) ignores the flag entirely -- every tool
    # stays reachable over stdio regardless of its HTTP exposure.
    http_exposed: bool = True


# Schema shared by the two search-shaped tools so they stay identical (#14/#40).
_SEARCH_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "api_key": {"type": "string", "description": "Your Inherent API key"},
        "query": {"type": "string", "description": "The search query"},
        "workspace_id": {
            "type": "string",
            "description": "Optional: specific workspace to search. If omitted, searches every "
            "workspace your key is authorized for (a workspace-scoped key: exactly its bound "
            "workspace; a key with no fixed workspace: every workspace you own).",
        },
        "limit": {
            "type": "integer",
            "description": "Maximum number of results (1-100, default 10)",
            "default": 10,
        },
        "min_score": {
            "type": "number",
            "description": "Minimum similarity score in [0,1] (default 0.0)",
            "default": 0.0,
        },
        "document_ids": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Optional: restrict the search to these document IDs",
        },
        "search_mode": {
            "type": "string",
            "enum": ["semantic", "hybrid", "keyword"],
            "description": "Retrieval strategy (default semantic)",
            "default": "semantic",
        },
        "alpha": {
            "type": "number",
            "description": "Hybrid fusion weight in [0,1] (1.0=vector-heavy, 0.0=keyword-heavy); only used when search_mode=hybrid",
            "default": 0.7,
        },
        # include_context / context_window were advertised but never honored by
        # _run_search (a silent no-op). Use the dedicated get_document_context
        # tool for surrounding chunks instead (#29).
    },
    "required": ["api_key", "query"],
}

# Schema for report_feedback (evals v1): an agent's verdict on one captured
# search event (see src/models/evals.py FeedbackRequest).
_FEEDBACK_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "api_key": {"type": "string", "description": "Your Inherent API key"},
        "event_id": {
            "type": "string",
            "description": "The event_id returned on the search response you are judging",
        },
        "verdict": {
            "type": "string",
            "enum": ["answered", "partial", "not_relevant"],
            "description": "Did the returned evidence answer the query?",
        },
        "useful_chunk_ids": {
            "type": "array",
            "items": {"type": "string"},
            "description": "chunk_ids from the results that actually answered it",
        },
        "note": {"type": "string", "description": "Optional short explanation"},
    },
    "required": ["api_key", "event_id", "verdict"],
}

# Schema for get_retrieval_health (evals v1): the workspace scorecard.
_HEALTH_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "api_key": {"type": "string", "description": "Your Inherent API key"},
        "workspace_id": {"type": "string", "description": "Workspace to report on"},
    },
    "required": ["api_key", "workspace_id"],
}


def create_mcp_server() -> Server:
    """Create and configure the MCP server."""
    server = Server("inherent-knowledge-base")

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        """List available MCP tools straight from the registry (#100)."""
        return [
            Tool(name=name, description=tool.description, inputSchema=tool.input_schema)
            for name, tool in _TOOLS.items()
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[TextContent]:
        """Handle tool calls: authenticate, enforce permission, then dispatch."""
        try:
            api_key = arguments.get("api_key")
            if not api_key:
                return [TextContent(type="text", text="Error: API key is required")]

            # Validate API key
            database = await get_database()
            key_info = await database.validate_api_key(api_key)

            if not key_info:
                return [TextContent(type="text", text="Error: Invalid or expired API key")]

            # Expiry parity with REST (#180): require_api_key
            # (src/services/auth.py) calls key_info.is_expired() itself after
            # validate_api_key returns, rather than trusting the DB layer
            # alone to have filtered an expired row. The real Postgres-backed
            # DatabaseService.validate_api_key happens to filter expiry too
            # (so this was not exploitable through it), but MCP's dispatcher
            # previously had no check of its own — any alternate
            # DatabaseService implementation that returned an already-expired
            # key_info would be silently accepted here while REST rejected
            # the identical key. Checked before the registry lookup so an
            # expired key never reaches permission checks or a tool handler.
            if key_info.is_expired():
                return [TextContent(type="text", text="Error: API key has expired")]

            # Registry lookup (#100): advertisement, permission, and dispatch
            # all come from the same ToolDef, so they cannot disagree.
            tool = _TOOLS.get(name)
            if tool is None:
                return [TextContent(type="text", text=f"Error: Unknown tool '{name}'")]

            # Permission parity with REST (#14): check BEFORE executing the body
            # so a denied key never reaches the search/db/verify services.
            if not key_info.has_permission(tool.permission):
                return [
                    TextContent(
                        type="text",
                        text=f"Error: API key does not have '{tool.permission}' permission",
                    )
                ]

            return await tool.handler(key_info, arguments)

        except Exception as e:
            logger.error("MCP tool error", tool=name, error=str(e))
            return [TextContent(type="text", text=f"Error: {str(e)}")]

    return server


def _structured(summary: str, payload: object) -> list[TextContent]:
    """Wrap a human summary plus a machine-parseable JSON block (#40).

    The text content keeps the existing list[TextContent] convention while
    embedding a ``structured`` JSON object agents can parse deterministically.
    """
    block = json.dumps({"structured": payload}, default=str)
    return [TextContent(type="text", text=f"{summary}\n\n```json\n{block}\n```")]


async def _get_workspace_ids(
    key_info: APIKeyInfo, requested_workspace_id: str | None
) -> tuple[list[str], str | None]:
    """
    Determine which workspace IDs to use for a query.

    Authorisation comes from ``get_authorized_workspace_ids`` — the SAME rule
    REST's ``_resolve_workspace`` enforces (#138): a workspace-scoped key is
    bound to exactly its one workspace (never the user's full owned set),
    while a user-scoped key may use any workspace the user owns. Before the
    #138 fix this function called ``database.get_user_workspace_ids``
    directly, which only ever reflected the user's full owned set and ignored
    ``key_info.workspace_id`` entirely — a scoped key could reach any
    workspace its owner owned via MCP even though REST rejected that same
    request with 403.

    The rejection text comes from ``describe_workspace_denial`` — the SAME
    wording REST's ``_resolve_workspace`` raises (#138 follow-up). A generic
    "you don't have access" reads to an agent as "that workspace doesn't
    exist" and invites it to guess other ids or give up; naming a scoped
    key's own bound workspace costs nothing (it's the caller's own grant) and
    lets the caller retry immediately with the right id.

    Returns:
        tuple of (workspace_ids list, error message or None)
    """
    database = await get_database()
    authorized = await get_authorized_workspace_ids(key_info, database)

    if requested_workspace_id:
        # User specified a workspace - verify it is in the key's authorised set.
        if requested_workspace_id not in authorized:
            return [], f"Error: {describe_workspace_denial(key_info, requested_workspace_id)}"
        return [requested_workspace_id], None
    else:
        # No workspace specified - use every workspace the key is authorised
        # for (exactly one, for a scoped key; every owned workspace otherwise).
        if not authorized:
            return [], "No workspaces found. Upload documents to create a workspace."
        return authorized, None


async def _run_search(
    key_info: APIKeyInfo,
    arguments: dict,
) -> tuple[list, list[str], str | None]:
    """Shared retrieval used by search_documents/search_memory/get_citations.

    Builds the SearchRequest via the shared ``build_search_request`` helper (so
    it matches REST exactly, #14), fans out over the authorised workspaces, and
    returns (results, workspaces_searched, error). ``results`` items are
    ``(workspace_id, SearchResult)`` tuples sorted by score and truncated to the
    requested limit. ``workspaces_searched`` is the ACTUAL set queried — not
    the caller's ``workspace_id`` argument, which is often absent — so callers
    can state real coverage instead of assuming "all workspaces" (#138
    follow-up: a workspace-scoped key silently narrows this to one, and the
    caller must be able to see that, not just guess it from an unqualified
    "across all workspaces" claim).
    """
    requested_workspace_id = arguments.get("workspace_id")
    query = arguments.get("query", "")
    if not query:
        return [], [], "Error: Query is required"

    workspace_ids, error = await _get_workspace_ids(key_info, requested_workspace_id)
    if error:
        return [], [], error

    request = build_search_request(arguments)
    search_service: SearchService = await get_search_service()

    tagged: list[tuple[str, object]] = []
    for workspace_id in workspace_ids:
        response = await search_service.search(workspace_id, key_info.user_id, request)
        for result in response.results:
            tagged.append((workspace_id, result))

    tagged.sort(key=_search_rank_key)
    return tagged[: request.limit], workspace_ids, None


def _coverage_note(workspace_ids: list[str]) -> str:
    """Describe the ACTUAL set of workspaces a call covered, for use in both
    the human summary and (as ``workspaces_searched``) the structured JSON
    payload (#138 follow-up).

    Never says "all workspaces" — that phrase is only true for a user-scoped
    key with no narrower request, and reads as false coverage for a
    workspace-scoped key (which is authorised for exactly one, regardless of
    how many its owner owns) or for an explicit single-workspace request.
    Stating the real, named set costs nothing and lets an agent verify
    coverage instead of trusting prose.
    """
    if len(workspace_ids) == 1:
        return f" in workspace '{workspace_ids[0]}'"
    if not workspace_ids:
        return ""
    return f" across your {len(workspace_ids)} authorized workspaces ({', '.join(workspace_ids)})"


def _search_rank_key(pair: tuple[str, object]) -> tuple[float, str, str]:
    """Stable sort key for merged multi-workspace results (#28).

    Sort by score descending, then by (chunk_id, document_id) so equal-scored
    results at the top-k cutoff order deterministically across identical
    requests — matching the REST path. Workspaces are iterated in a set's
    (nondeterministic) order, so score alone is not stable.
    """
    result = pair[1]
    return (-result.score, result.chunk_id, result.document_id)  # type: ignore[attr-defined]


async def _handle_search(key_info: APIKeyInfo, arguments: dict) -> list[TextContent]:
    """Handle search_documents / search_memory tools (#14/#40).

    The summary states the ACTUAL set of workspaces searched via
    ``_coverage_note``, and the structured payload carries the same set as
    ``workspaces_searched`` (#138 follow-up) — narrowing to a scoped key's one
    workspace is correct behavior, but claiming "across all workspaces" while
    only one was searched is a false affirmation an agent has no way to catch
    from prose alone. ``workspaces_searched`` gives it a programmatic check.
    """
    tagged, workspace_ids, error = await _run_search(key_info, arguments)
    if error:
        return [TextContent(type="text", text=error)]

    query = arguments.get("query", "")
    note = _coverage_note(workspace_ids)
    if not tagged:
        return _structured(
            f"No results found for: {query}{note}",
            {"query": query, "results": [], "workspaces_searched": workspace_ids},
        )

    summary = f"Found {len(tagged)} results for '{query}'{note}:\n\n"
    structured_results = []
    for i, (workspace_id, result) in enumerate(tagged, 1):
        summary += f"**{i}. {result.document_name}** (score: {result.score:.2f})\n"
        summary += f"Document ID: {result.document_id} | Workspace: {workspace_id}\n"
        content = result.content
        summary += f"```\n{content[:500]}{'...' if len(content) > 500 else ''}\n```\n\n"
        structured_results.append(
            {
                "workspace_id": workspace_id,
                "chunk_id": result.chunk_id,
                "document_id": result.document_id,
                "document_name": result.document_name,
                "content": result.content,
                "score": result.score,
                "score_source": result.score_source,
                "is_stale": result.is_stale,
                "source_uri": result.source_uri,
                "content_hash": result.content_hash,
            }
        )

    return _structured(
        summary.rstrip(),
        {"query": query, "results": structured_results, "workspaces_searched": workspace_ids},
    )


async def _handle_get_citations(key_info: APIKeyInfo, arguments: dict) -> list[TextContent]:
    """Handle get_citations: run a search and return the Citation objects (#40).

    Carries ``workspaces_searched`` in the structured payload for the same
    reason ``_handle_search`` does (#138 follow-up): the caller must be able
    to verify actual coverage, not infer it from result count alone.
    """
    tagged, workspace_ids, error = await _run_search(key_info, arguments)
    if error:
        return [TextContent(type="text", text=error)]

    query = arguments.get("query", "")
    citations = []
    for workspace_id, result in tagged:
        if result.citation is not None:
            citations.append({"workspace_id": workspace_id, **result.citation.model_dump()})

    if not citations:
        return _structured(
            f"No citations found for: {query}",
            {"query": query, "citations": [], "workspaces_searched": workspace_ids},
        )

    note = _coverage_note(workspace_ids)
    summary = f"Found {len(citations)} citations for '{query}'{note}:\n\n"
    for i, cit in enumerate(citations, 1):
        summary += (
            f"**{i}. {cit['document_name']}** (score: {cit['score']:.2f}) chunk {cit['chunk_id']}\n"
        )

    return _structured(
        summary.rstrip(),
        {"query": query, "citations": citations, "workspaces_searched": workspace_ids},
    )


async def _handle_get_context(key_info: APIKeyInfo, arguments: dict) -> list[TextContent]:
    """Handle get_document_context tool.

    Delegates lookup + authorization to ``_resolve_document_for_user`` — the
    SAME check every other document-scoped tool uses — instead of duplicating
    it inline. Before #138 follow-up this handler had its own copy of the
    check with its own distinguishable "you don't have access" message,
    which was a cross-workspace existence oracle REST doesn't have; routing
    through the shared helper means the undifferentiated-not-found rule is
    expressed in exactly one place and this handler cannot drift from it.

    Bounded via ``windowed_document_context`` (#219) — the SAME function and
    the SAME ``DEFAULT_MAX_CHARS`` default REST's ``GET
    /v1/chunks/{document_id}/context`` uses, so this tool (the surface the
    issue calls out as most at risk: an agent can blow its own context window
    with a single call) cannot silently drift to a different bound. Optional
    ``max_chars`` / ``offset`` arguments let an agent ask for less, or page
    through the rest, exactly like REST's query params.
    """
    document_id = arguments.get("document_id", "")

    if not document_id:
        return [TextContent(type="text", text="Error: Document ID is required")]

    document, _, error = await _resolve_document_for_user(key_info, document_id)
    if error:
        return [TextContent(type="text", text=error)]

    # Clamp malformed/out-of-range arguments to the nearest valid value
    # instead of raising — same style as list_documents' page/page_size
    # clamp above (an agent's tool-call arguments are free-form, unlike a
    # typed REST query param that FastAPI validates for us).
    try:
        max_chars = min(
            MAX_MAX_CHARS, max(MIN_MAX_CHARS, int(arguments.get("max_chars", DEFAULT_MAX_CHARS)))
        )
    except (TypeError, ValueError):
        max_chars = DEFAULT_MAX_CHARS
    try:
        offset = max(0, int(arguments.get("offset", 0)))
    except (TypeError, ValueError):
        offset = 0

    database = await get_database()
    chunks = await database.get_document_chunks_by_doc_id(document_id)
    window = windowed_document_context(chunks, offset=offset, max_chars=max_chars)

    result_text = f"# {document.name}\n\n"
    result_text += f"**Source:** {document.source_type}\n"
    result_text += f"**Size:** {document.size_bytes:,} bytes\n"
    result_text += f"**Chunks in this page:** {len(window.chunks)} of {len(chunks)}\n"
    result_text += f"**Workspace:** {document.workspace_id}\n\n"
    result_text += "---\n\n"
    result_text += window.full_text

    payload = {
        "document_id": document.id,
        "truncated": window.truncated,
        "total_chars": window.total_chars,
        "offset": window.offset,
        "next_offset": window.next_offset,
    }
    return _structured(result_text, payload)


async def _handle_list_documents(key_info: APIKeyInfo, arguments: dict) -> list[TextContent]:
    """Handle list_documents tool.

    States the ACTUAL set of workspaces listed via ``_coverage_note`` and
    carries it as ``workspaces_searched`` in a trailing structured JSON block
    (#138 follow-up) — this handler previously said "across all workspaces"
    even when a scoped key narrowed the listing to exactly one.
    """
    # Clamp to the same bounds the REST route enforces (page>=1,
    # 1<=page_size<=MAX_PAGE_SIZE) so an agent can't request a negative SQL
    # OFFSET or dump the whole tenant in one call (#13).
    try:
        page = max(1, int(arguments.get("page", 1)))
    except (TypeError, ValueError):
        page = 1
    try:
        page_size = min(MAX_PAGE_SIZE, max(1, int(arguments.get("page_size", DEFAULT_PAGE_SIZE))))
    except (TypeError, ValueError):
        page_size = DEFAULT_PAGE_SIZE
    requested_workspace_id = arguments.get("workspace_id")

    # Get workspace IDs to list from
    workspace_ids, error = await _get_workspace_ids(key_info, requested_workspace_id)
    if error:
        return [TextContent(type="text", text=error)]

    database = await get_database()

    if requested_workspace_id:
        # Single workspace
        documents, total = await database.get_documents(requested_workspace_id, page, page_size)
    else:
        # Multiple workspaces
        documents, total = await database.get_documents_multi_workspace(
            workspace_ids, page, page_size
        )

    if not documents:
        return _structured(
            f"No documents found{_coverage_note(workspace_ids)}",
            {"total": 0, "page": page, "workspaces_searched": workspace_ids},
        )

    result_text = (
        f"Found {total} documents{_coverage_note(workspace_ids)} (showing page {page}):\n\n"
    )
    for doc in documents:
        result_text += f"- **{doc.name}**\n"
        result_text += f"  ID: `{doc.id}`\n"
        result_text += f"  Type: {doc.source_type} | Size: {doc.size_bytes:,} bytes\n"
        result_text += f"  Chunks: {doc.chunk_count} | Status: {doc.status} | Workspace: {doc.workspace_id}\n\n"

    return _structured(
        result_text.rstrip(),
        {"total": total, "page": page, "workspaces_searched": workspace_ids},
    )


async def _handle_verify_claim(key_info: APIKeyInfo, arguments: dict) -> list[TextContent]:
    """Handle verify_claim tool — reuses src/services/verify.verify_claim (#40)."""
    claim = arguments.get("claim", "")
    evidence = arguments.get("evidence") or []

    if not claim:
        return [TextContent(type="text", text="Error: Claim is required")]
    if not isinstance(evidence, list):
        return [TextContent(type="text", text="Error: Evidence must be a list of strings")]

    verdict = verify_claim(claim, [str(e) for e in evidence])
    summary = (
        f"Claim support: **{verdict.support_level}** (score: {verdict.score:.2f})\n{verdict.reason}"
    )
    return _structured(summary, verdict.model_dump())


async def _resolve_document_for_user(key_info: APIKeyInfo, document_id: str):
    """Fetch a document by id and verify the key is authorised for its workspace.

    Authorisation via ``get_authorized_workspace_ids`` (#138): a
    workspace-scoped key must own the document's exact workspace, not merely
    any workspace its owning user has.

    Returns an UNDIFFERENTIATED "not found" for both a missing document and
    one that exists but is in a workspace the key isn't authorised for (#138
    follow-up) — matching REST's ``GET /v1/documents/{id}``
    (``src/api/v1/documents.py``), whose workspace-scoped query returns
    ``None`` in both cases and so always answers `404`. A distinct "you
    don't have access" message here would be a cross-workspace EXISTENCE
    ORACLE: a caller could iterate document ids and learn exactly which ones
    exist in a workspace it cannot read (e.g. a contractor key scoped to
    ``ws_tier1`` probing which ids exist in ``ws_tier2``) — precisely what
    REST's undifferentiated 404 exists to prevent. Do not reintroduce a
    distinguishable message for the unauthorized branch.

    Returns (document, workspace_ids, error_text). On any access failure the
    error_text is set and the document is None, so callers return without
    ever reading further data.
    """
    database = await get_database()
    document = await database.get_document_by_id(document_id)
    not_found = f"Error: Document '{document_id}' not found"
    if not document:
        return None, [], not_found
    authorized = await get_authorized_workspace_ids(key_info, database)
    if document.workspace_id not in authorized:
        return None, authorized, not_found
    return document, authorized, None


async def _handle_explain_lineage(key_info: APIKeyInfo, arguments: dict) -> list[TextContent]:
    """Handle explain_lineage: return provenance + freshness for a doc/chunk (#40).

    Reuses already-ingested data only (no new business logic): the document row
    and its chunks, with provenance fields (``source_uri``, ``content_hash``,
    ``ingested_at``) read from chunk/document metadata. ``is_stale`` is computed
    with the SAME freshness logic the search path uses
    (``SearchService._compute_is_stale``), so lineage and search agree.
    """
    document_id = arguments.get("document_id", "")
    chunk_id = arguments.get("chunk_id")
    if not document_id:
        return [TextContent(type="text", text="Error: Document ID is required")]

    document, _, error = await _resolve_document_for_user(key_info, document_id)
    if error:
        return [TextContent(type="text", text=error)]

    database = await get_database()
    chunks = await database.get_document_chunks_by_doc_id(document_id)

    try:
        lineage = build_lineage(document, chunks, chunk_id=chunk_id)
    except KeyError:
        return [
            TextContent(
                type="text",
                text=f"Error: Chunk '{chunk_id}' not found in document '{document_id}'",
            )
        ]

    summary = (
        f"Lineage for **{lineage.document_name}** ({lineage.document_id})\n"
        f"Source: {lineage.source_uri or 'unknown'} | "
        f"Stale: {lineage.is_stale} | Ingested: {lineage.ingested_at or 'unknown'}"
    )
    return _structured(summary, lineage.model_dump())


async def _handle_refresh_stale_source(key_info: APIKeyInfo, arguments: dict) -> list[TextContent]:
    """Handle refresh_stale_source: re-trigger ingestion for a document (#40).

    Mirrors POST /v1/documents/{id}/refresh: rebuild the stored upload event and
    re-publish it to the ingestion MQ topic so existing chunks are replaced and
    their ``ingested_at`` reset (clearing ``is_stale``). Reuses the same database
    + MQ services; no new business logic.
    """
    document_id = arguments.get("document_id", "")
    if not document_id:
        return [TextContent(type="text", text="Error: Document ID is required")]

    document, _, error = await _resolve_document_for_user(key_info, document_id)
    if error:
        return [TextContent(type="text", text=error)]

    database = await get_database()
    workspace_id = document.workspace_id
    fields = await database.get_document_upload_fields(document_id, workspace_id)
    if not fields:
        return [TextContent(type="text", text=f"Error: Document '{document_id}' not found")]

    from datetime import datetime, timezone

    from src.config import settings
    from src.services.mq import get_mq_service

    await database.create_or_reset_pending_document(
        document_id=fields["document_id"],
        workspace_id=fields["workspace_id"],
        user_id=fields["user_id"],
        filename=fields["filename"],
        original_filename=fields["original_filename"],
        content_type=fields["content_type"],
        size_bytes=fields["size_bytes"] or 0,
        storage_backend=fields["storage_backend"],
        storage_path=fields["storage_path"],
        storage_bucket=fields.get("storage_bucket"),
        storage_url=fields.get("storage_url"),
    )

    now_iso = datetime.now(timezone.utc).isoformat()
    mq_message = {
        "event_type": "document.uploaded",
        "document_id": fields["document_id"],
        "workspace_id": fields["workspace_id"],
        "user_id": fields["user_id"],
        "filename": fields["filename"],
        "original_filename": fields["original_filename"],
        "content_type": fields["content_type"],
        "size_bytes": fields["size_bytes"],
        "storage_backend": fields["storage_backend"],
        "storage_path": fields["storage_path"],
        "storage_bucket": fields.get("storage_bucket"),
        "storage_url": fields.get("storage_url"),
        "timestamp": now_iso,
        "contract_version": "1.0.0",
    }

    mq = await get_mq_service()
    try:
        await mq.publish(settings.mq_topic_document_uploaded, mq_message)
    except Exception as exc:
        # Compensate the pending reset above (#98). The document was just moved
        # to 'pending'; if the enqueue fails it will never be re-ingested, so we
        # must mark it failed — exactly as the REST twin
        # (POST /v1/documents/{id}/refresh) does — instead of stranding it as
        # permanently 'pending'. Both surfaces must leave the SAME state on an MQ
        # outage (dual-surface failure parity, CLAUDE.md). The mark is retried
        # with backoff; on exhaustion the helper emits the CRITICAL log + metric
        # that flag the orphaned 'pending' row (#99).
        logger.error(
            "MQ publish failed during refresh — re-ingestion not enqueued",
            error=str(exc),
            document_id=document_id,
        )
        await mark_document_failed_with_retry(
            database,
            document_id,
            workspace_id,
            "refresh enqueue failed",
            operation="refresh_enqueue",
        )
        return [
            TextContent(
                type="text",
                text=(
                    "Error: failed to queue the document for re-processing. Please try again later."
                ),
            )
        ]

    payload = {
        "document_id": fields["document_id"],
        "workspace_id": fields["workspace_id"],
        "status": "pending",
    }
    return _structured(
        f"Document '{document.name}' ({document_id}) queued for re-ingestion (refresh).",
        payload,
    )


async def _handle_report_feedback(key_info: APIKeyInfo, arguments: dict) -> list[TextContent]:
    """Record agent feedback on a captured search event (evals v1).

    Delegates to the shared ``submit_feedback`` service (same promotion rules
    REST uses at POST /v1/evals/feedback) so the two surfaces never drift.
    ``workspace_ids`` comes from ``get_authorized_workspace_ids`` (#138) so a
    workspace-scoped key can only promote/attach feedback within its own
    workspace, matching REST's ``[auth.workspace_id]`` (src/api/v1/evals.py).
    """
    database = await get_database()
    workspace_ids = await get_authorized_workspace_ids(key_info, database)
    req = FeedbackRequest(
        event_id=arguments["event_id"],
        verdict=arguments["verdict"],
        useful_chunk_ids=arguments.get("useful_chunk_ids"),
        note=arguments.get("note"),
    )
    try:
        result = await submit_feedback(database, workspace_ids=workspace_ids, req=req)
    except EventNotFoundError:
        return [
            TextContent(
                type="text",
                text=f"Error: unknown or expired event_id '{req.event_id}'",
            )
        ]
    return [TextContent(type="text", text=result.model_dump_json())]


async def _handle_get_retrieval_health(key_info: APIKeyInfo, arguments: dict) -> list[TextContent]:
    """Return the workspace scorecard so agents can calibrate trust (evals v1).

    Enforces the same authorised-workspace check every other tool uses (#138:
    ``get_authorized_workspace_ids`` — key binding, not the user's full owned
    set) before handing the workspace_id to ``build_scorecard``. The
    rejection uses ``describe_workspace_denial`` — the same wording every
    other workspace-argument rejection uses (#138 follow-up: this used to be
    a THIRD distinct wording, "workspace not accessible with this key",
    alongside REST's and ``_get_workspace_ids``'s).
    """
    database = await get_database()
    workspace_ids = await get_authorized_workspace_ids(key_info, database)
    workspace_id = arguments["workspace_id"]
    if workspace_id not in workspace_ids:
        return [
            TextContent(
                type="text", text=f"Error: {describe_workspace_denial(key_info, workspace_id)}"
            )
        ]
    scorecard = await build_scorecard(database, workspace_id=workspace_id)
    return [TextContent(type="text", text=scorecard.model_dump_json())]


async def _handle_delete_document(key_info: APIKeyInfo, arguments: dict) -> list[TextContent]:
    """Handle delete_document: retract a document from every store (#87).

    Mirrors DELETE /v1/documents/{id}: same access check as the other
    document-scoped tools (the caller must own the document's workspace), then
    the shared deletion orchestrator removes vectors, the database row +
    chunks, and best-effort the stored bytes. A vector-store failure raises
    into the dispatcher's error path, leaving the document intact (retryable).
    """
    document_id = arguments.get("document_id", "")
    if not document_id:
        return [TextContent(type="text", text="Error: Document ID is required")]

    document, _, error = await _resolve_document_for_user(key_info, document_id)
    if error:
        return [TextContent(type="text", text=error)]

    from src.services.deletion import delete_document_everywhere

    database = await get_database()
    outcome = await delete_document_everywhere(database, document_id, document.workspace_id)
    if not outcome.found:
        return [TextContent(type="text", text=f"Error: Document '{document_id}' not found")]

    payload = {
        "document_id": document_id,
        "workspace_id": document.workspace_id,
        "deleted": True,
        "chunks_deleted": outcome.chunks_deleted,
        "vectors_deleted": outcome.vectors_deleted,
        "storage_deleted": outcome.storage_deleted,
    }
    return _structured(
        f"Document '{document.name}' ({document_id}) permanently deleted "
        f"({outcome.chunks_deleted} chunks, {outcome.vectors_deleted} vectors removed).",
        payload,
    )


async def _handle_get_document(key_info: APIKeyInfo, arguments: dict) -> list[TextContent]:
    """Handle get_document: return one document's metadata as JSON (#87 parity).

    Same access check as ``_handle_get_context`` / ``_resolve_document_for_user``
    (get_document_by_id then verify the caller owns the workspace) but skips
    fetching chunks/full_text — this is the metadata-only counterpart of GET
    /v1/documents/{id}.
    """
    document_id = arguments.get("document_id", "")
    if not document_id:
        return [TextContent(type="text", text="Error: Document ID is required")]

    document, _, error = await _resolve_document_for_user(key_info, document_id)
    if error:
        return [TextContent(type="text", text=error)]

    return [TextContent(type="text", text=document.model_dump_json())]


async def _handle_list_chunks(key_info: APIKeyInfo, arguments: dict) -> list[TextContent]:
    """Handle list_chunks: return a document's chunks as JSON (#87 parity).

    Same access check as ``get_document`` (the caller must own the document's
    workspace) — same data as GET /v1/chunks/{document_id}.
    """
    document_id = arguments.get("document_id", "")
    if not document_id:
        return [TextContent(type="text", text="Error: Document ID is required")]

    document, _, error = await _resolve_document_for_user(key_info, document_id)
    if error:
        return [TextContent(type="text", text=error)]

    database = await get_database()
    chunks = await database.get_document_chunks_by_doc_id(document.id)
    payload = [chunk.model_dump() for chunk in chunks]
    return _structured(f"{len(chunks)} chunks for document '{document.id}'", payload)


async def _resolve_single_workspace_for_upload(
    key_info: APIKeyInfo, requested_workspace_id: str | None
) -> tuple[str | None, str | None]:
    """Resolve exactly one target workspace for an upload.

    Unlike read/search tools (which fan out over every owned workspace) or
    the document-scoped write tools (delete_document / refresh_stale_source,
    which resolve their workspace FROM the existing document), upload has no
    document yet and must write to exactly one workspace. So:

    - ``requested_workspace_id`` given: validate ownership via the same
      ``_get_workspace_ids`` check every other tool uses (tenant scoping),
      then use it.
    - omitted: the caller must own EXACTLY one workspace, or the call is
      rejected asking them to disambiguate with ``workspace_id`` — silently
      picking one of several owned workspaces would be a surprising place to
      write data.

    Returns (workspace_id, error_text); on error workspace_id is None.
    """
    if requested_workspace_id:
        workspace_ids, error = await _get_workspace_ids(key_info, requested_workspace_id)
        if error:
            return None, error
        return workspace_ids[0], None

    # #138: authorised set, not the user's full owned set — a scoped key
    # narrows to its one workspace here too (len(owned) == 1), never forcing
    # disambiguation among workspaces the key isn't even bound to.
    database = await get_database()
    owned = await get_authorized_workspace_ids(key_info, database)
    if not owned:
        return None, "Error: No workspaces found. Upload documents to create a workspace."
    if len(owned) > 1:
        return None, (
            "Error: You have access to multiple workspaces; pass 'workspace_id' to "
            "specify which one to upload to."
        )
    return owned[0], None


def _default_upload_content_type(filename: str) -> str:
    """The ``content_type`` ``upload_document`` uses when the caller omits
    it. This is a SERVER-side fallback only -- it is documented in the
    ``content_type`` property's description text, not advertised as a JSON
    Schema ``default`` (#193 coordinator review BLOCKER: the schema used to
    carry ``"default": "text/markdown"``, which many MCP clients auto-fill
    onto an omitted argument before the server ever sees the omission,
    turning every upload into an explicit "text/markdown" declaration and
    short-circuiting the extension derivation below for every caller whose
    client does that -- removed for that reason; see the ``content_type``
    property's description in ``_TOOLS["upload_document"]`` for the honest,
    client-visible explanation of this fallback).

    Derived from `filename`'s extension when the registry recognizes it AND
    that type is MCP-eligible (e.g. ``notes.txt`` -> ``text/plain``,
    ``data.csv`` -> ``text/csv``) -- falls back to ``text/plain`` only for an
    unrecognized or absent extension (#208: was ``text/markdown`` until
    #208 -- confidently mislabelling ``Dockerfile``, ``Makefile``,
    ``README``, ``.gitignore``, and ``archive.tar.gz`` as markdown, which
    none of them are. ``text/plain`` is the honest generic for "a text file
    whose format we did not recognize," not a guess dressed up as a real
    answer). Historically (#117 review BLOCKER 2) the default was a flat
    ``"text/markdown"`` regardless of filename, which meant the tool's own
    documented default broke itself the moment #117's extension-consistency
    check landed: calling ``upload_document(filename="notes.txt", ...)`` and
    omitting the optional `content_type` got ``notes.txt`` defaulted to
    ``text/markdown`` and then rejected as a mismatch against its own
    ``.txt`` extension.

    #197 fix: resolving the spec's ``mime_types[0]`` unconditionally was
    correct only by accident, because every spec registered at the time
    described exactly ONE format. The "code" spec (#122) pools 22 MIME
    aliases across 21 distinct languages under one registry entry sharing an
    extractor -- ``mime_types[0]`` for THAT spec is just "text/x-python",
    the first entry in the pool, not a stand-in for "whichever language this
    file actually is". Every code file uploaded with `content_type` omitted
    was therefore mislabelled identically regardless of its real extension
    (a .go file stored as "text/x-python"). ``mime_type_for_extension``
    (inh_contracts) consults the spec's per-extension override when one
    exists and only falls back to ``mime_types[0]`` when it doesn't -- for
    every pre-#197 spec (one override-free format each), behavior is
    unchanged.
    """
    if "." not in filename:
        return "text/plain"
    extension = "." + filename.rsplit(".", 1)[-1]
    spec = get_spec_for_extension(extension)
    if spec is not None and "mcp" in spec.surfaces:
        return mime_type_for_extension(spec, extension)
    return "text/plain"


async def _handle_upload_document(key_info: APIKeyInfo, arguments: dict) -> list[TextContent]:
    """Handle upload_document: text-only counterpart of POST /v1/documents (#87).

    Rejects empty content and non-``text/*`` content types up front (binary
    uploads are REST-only by design — the tool has no way to accept raw
    bytes). Resolves a single target workspace (see
    ``_resolve_single_workspace_for_upload``) then UTF-8 encodes the text and
    delegates validation/dedup/storage/enqueue to the shared
    ``intake_document`` service — the exact same pipeline POST /v1/documents
    uses, so the two surfaces cannot drift.
    """
    filename = arguments.get("filename", "")
    content = arguments.get("content", "")
    declared_content_type = arguments.get("content_type")

    if not filename:
        return [TextContent(type="text", text="Error: filename is required")]
    if not content:
        return [TextContent(type="text", text="Error: content is required and cannot be empty")]

    # Explicitly-unsupported formats (#124/#126 review blocker 3) -- checked
    # BEFORE the content_type default is resolved below, against BOTH the
    # declared content_type (if the caller passed one) and the filename
    # extension. The extension check is what closes the actual hole: this
    # tool's content_type is OPTIONAL and defaults from the filename
    # extension when omitted (see `_default_upload_content_type`), and
    # '.doc'/'.msg' have no FILE_TYPE_REGISTRY entry to derive a content
    # type from -- so a caller who omits content_type for "report.doc" used
    # to silently default to "text/markdown" (MCP-eligible) and sail
    # straight through as if it were prose, the exact accept-then-garble
    # outcome both issues forbid. Sourced from the SAME
    # inh_contracts.EXPLICITLY_UNSUPPORTED table REST's intake_document
    # reads, so the two surfaces cannot drift on which formats this covers.
    rejection_message = (
        declared_content_type and explicitly_unsupported_message_for_mime(declared_content_type)
    ) or explicitly_unsupported_message_for_extension(filename)
    if rejection_message is not None:
        return [TextContent(type="text", text=f"Error: {rejection_message}")]

    content_type = declared_content_type or _default_upload_content_type(filename)

    if content_type not in SUPPORTED_TEXT_MIME_TYPES:
        return [
            TextContent(
                type="text",
                text=(
                    f"Error: upload_document accepts only these text content types: "
                    f"{', '.join(SUPPORTED_TEXT_MIME_TYPES)} (got '{content_type}'). "
                    f"Other formats (PDF, DOCX, PNG, ...) are REST-only by design — use "
                    f"POST /v1/documents instead."
                ),
            )
        ]

    workspace_id, error = await _resolve_single_workspace_for_upload(
        key_info, arguments.get("workspace_id")
    )
    if error:
        return [TextContent(type="text", text=error)]
    assert workspace_id is not None  # narrowed by the error check above

    database = await get_database()
    result = await intake_document(
        database=database,
        workspace_id=workspace_id,
        user_id=key_info.user_id,
        content_bytes=content.encode("utf-8"),
        filename=filename,
        content_type=content_type,
    )
    return [TextContent(type="text", text=result.model_dump_json())]


# =============================================================================
# Tool registry — THE single place a tool exists (#100)
# =============================================================================
# Adding a tool = adding one entry here (plus its handler above). list_tools,
# permission enforcement, and dispatch all derive from this dict, so a tool can
# never be advertised-but-unusable or callable-but-hidden. Defined after the
# handlers so the entries can reference them directly.

_TOOLS: dict[str, ToolDef] = {
    "search_documents": ToolDef(
        description="Search for relevant documents and chunks using semantic, hybrid, or "
        "keyword search. Omit workspace_id to search every workspace your key is authorized "
        "for (a workspace-scoped key: exactly its bound workspace). Requires 'search' "
        "permission.",
        input_schema=_SEARCH_INPUT_SCHEMA,
        permission="search",
        handler=_handle_search,
    ),
    "search_memory": ToolDef(
        description="Memory primitive: retrieve evidence chunks for a query (canonical "
        "agent search). Same parameters and behaviour as search_documents; returns "
        "structured results with scores and provenance. Requires 'search' permission.",
        input_schema=_SEARCH_INPUT_SCHEMA,
        permission="search",
        handler=_handle_search,
        # Excluded from HTTP (#220): identical behavior to search_documents --
        # two tools doing one job costs every HTTP agent permanent context
        # overhead with no capability gained. Unchanged on stdio.
        http_exposed=False,
    ),
    "get_citations": ToolDef(
        description="Run a search and return the claim-level Citation objects attached to "
        "each result (chunk_id, document, character spans, score, provenance, freshness) "
        "so an answer can cite its evidence. Requires 'search' permission.",
        input_schema=_SEARCH_INPUT_SCHEMA,
        permission="search",
        handler=_handle_get_citations,
        # Excluded from HTTP (#220): same params/endpoint as search_documents,
        # whose results already carry a full `citation` object per result
        # (chunk_id, document_name, content, start_char, end_char). Unchanged
        # on stdio.
        http_exposed=False,
    ),
    "get_document_context": ToolDef(
        description="Get a bounded window of a document's content for context. Response is "
        "capped by max_chars (default 20,000 chars, ~5,000 tokens) so one call can't exhaust "
        "your context window; check the structured `truncated` flag and, if true, re-call with "
        "offset=next_offset for the rest. Requires 'read' permission.",
        input_schema={
            "type": "object",
            "properties": {
                "api_key": {"type": "string", "description": "Your Inherent API key"},
                "document_id": {
                    "type": "string",
                    "description": "The document ID to retrieve",
                },
                "max_chars": {
                    "type": "integer",
                    "description": "Max characters of text to return in this call "
                    "(default 20,000; capped at 100,000). Ask for less if you only need a "
                    "preview.",
                    "default": 20000,
                },
                "offset": {
                    "type": "integer",
                    "description": "Character offset to resume from. Use the previous call's "
                    "structured `next_offset` to page through a truncated document.",
                    "default": 0,
                },
            },
            "required": ["api_key", "document_id"],
        },
        permission="read",
        handler=_handle_get_context,
    ),
    "list_documents": ToolDef(
        description="List all documents. Omit workspace_id to list from every workspace "
        "your key is authorized for (a workspace-scoped key: exactly its bound workspace). "
        "Requires 'read' permission.",
        input_schema={
            "type": "object",
            "properties": {
                "api_key": {"type": "string", "description": "Your Inherent API key"},
                "workspace_id": {
                    "type": "string",
                    "description": "Optional: specific workspace. If omitted, lists from every "
                    "workspace your key is authorized for (a workspace-scoped key: exactly its "
                    "bound workspace).",
                },
                "page": {
                    "type": "integer",
                    "description": "Page number (default 1)",
                    "default": 1,
                },
                "page_size": {
                    "type": "integer",
                    "description": "Items per page (default 20)",
                    "default": 20,
                },
            },
            "required": ["api_key"],
        },
        permission="read",
        handler=_handle_list_documents,
    ),
    "verify_claim": ToolDef(
        description="Memory primitive: verify how well a list of evidence passages "
        "supports a claim (offline lexical strategy). Returns support_level "
        "(strong/weak/none), score and reason. Requires 'read' permission.",
        input_schema={
            "type": "object",
            "properties": {
                "api_key": {"type": "string", "description": "Your Inherent API key"},
                "claim": {
                    "type": "string",
                    "description": "The natural-language claim to verify",
                },
                "evidence": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Candidate supporting passages (e.g. retrieved chunk contents)",
                },
            },
            "required": ["api_key", "claim"],
        },
        permission="read",
        handler=_handle_verify_claim,
        # Excluded from HTTP (#220): src/services/verify.py is an offline
        # lexical token-overlap counter with no LLM and no negation handling
        # -- it scores "Neither party may cancel this Agreement at any time"
        # as strong support (0.833) for the claim "Either party may cancel
        # this Agreement at any time" against that exact sentence as
        # evidence. Sound as an internal pre-filter; unsafe under a tool name
        # ("verify_claim") an HTTP agent reads as entailment. Kept on stdio
        # (self-hosters/internal dev, who read this docstring) and REST;
        # restore to HTTP once it is NLI- or LLM-backed.
        http_exposed=False,
    ),
    "explain_lineage": ToolDef(
        description="Memory primitive: explain a document's (or chunk's) provenance and "
        "freshness — source_uri, content_hash, ingested_at, is_stale and document_name — "
        "from already-ingested data. Requires 'read' permission.",
        input_schema={
            "type": "object",
            "properties": {
                "api_key": {"type": "string", "description": "Your Inherent API key"},
                "document_id": {
                    "type": "string",
                    "description": "The document ID to explain",
                },
                "chunk_id": {
                    "type": "string",
                    "description": "Optional: a specific chunk ID for chunk-level provenance",
                },
            },
            "required": ["api_key", "document_id"],
        },
        permission="read",
        handler=_handle_explain_lineage,
    ),
    "refresh_stale_source": ToolDef(
        description="Memory primitive: re-ingest an already-uploaded document to clear "
        "stale evidence (same logic as POST /v1/documents/{id}/refresh). Requires "
        "'write' permission.",
        input_schema={
            "type": "object",
            "properties": {
                "api_key": {"type": "string", "description": "Your Inherent API key"},
                "document_id": {
                    "type": "string",
                    "description": "The document ID to refresh (re-ingest)",
                },
            },
            "required": ["api_key", "document_id"],
        },
        permission="write",
        handler=_handle_refresh_stale_source,
    ),
    "report_feedback": ToolDef(
        description="ALWAYS call this after using search results: report whether the "
        "returned evidence answered your query. Your feedback builds this workspace's "
        "retrieval eval set and improves future quality measurement. Pass the "
        "event_id from the search response. Requires 'search' permission.",
        input_schema=_FEEDBACK_INPUT_SCHEMA,
        permission="search",
        handler=_handle_report_feedback,
        # Excluded from HTTP (#220): the issue's "10, not 13" acceptance list
        # enumerates exactly search_documents/list_documents/get_document/
        # list_chunks/get_document_context/explain_lineage/upload_document/
        # delete_document/refresh_stale_source/get_retrieval_health --
        # report_feedback (added by evals v1, after #220 was filed) is not
        # among them and not among the 3 tools the issue explicitly excludes
        # either. Left off HTTP for now rather than silently growing the
        # "10" to 11 on judgment call; file a follow-up issue if it should
        # ship on HTTP. Unchanged on stdio.
        http_exposed=False,
    ),
    "get_retrieval_health": ToolDef(
        description="Get the retrieval-quality scorecard for a workspace: answer rate, "
        "verdict distribution, corpus gaps, labeled-case count, and last eval run. Use "
        "it to calibrate how much to trust search results from this corpus. Requires "
        "'search' permission.",
        input_schema=_HEALTH_INPUT_SCHEMA,
        permission="search",
        handler=_handle_get_retrieval_health,
    ),
    "delete_document": ToolDef(
        description="Memory primitive: permanently delete a document and all of its "
        "derived data — vectors, chunks, and stored bytes (same logic as DELETE "
        "/v1/documents/{id}). Use to retract knowledge that should no longer be "
        "retrievable. Requires 'write' permission.",
        input_schema={
            "type": "object",
            "properties": {
                "api_key": {"type": "string", "description": "Your Inherent API key"},
                "document_id": {
                    "type": "string",
                    "description": "The document ID to delete",
                },
            },
            "required": ["api_key", "document_id"],
        },
        permission="write",
        handler=_handle_delete_document,
    ),
    "get_document": ToolDef(
        description="Get a single document's metadata (name, source_type, mime_type, "
        "size, chunk_count, status, timestamps) — same data as GET "
        "/v1/documents/{id}. Requires 'read' permission.",
        input_schema={
            "type": "object",
            "properties": {
                "api_key": {"type": "string", "description": "Your Inherent API key"},
                "document_id": {
                    "type": "string",
                    "description": "The document ID to retrieve",
                },
            },
            "required": ["api_key", "document_id"],
        },
        permission="read",
        handler=_handle_get_document,
    ),
    "list_chunks": ToolDef(
        description="List all chunks belonging to a document (id, content, chunk_index, "
        "token_count) — same data as GET /v1/chunks/{document_id}. Requires 'read' "
        "permission.",
        input_schema={
            "type": "object",
            "properties": {
                "api_key": {"type": "string", "description": "Your Inherent API key"},
                "document_id": {
                    "type": "string",
                    "description": "The document ID whose chunks to list",
                },
            },
            "required": ["api_key", "document_id"],
        },
        permission="read",
        handler=_handle_list_chunks,
    ),
    "upload_document": ToolDef(
        # Deliberately does NOT repeat the full type list here (that copy
        # lives once, below, on the `content_type` property) -- restating it
        # in both places doubled ~640 chars of every `list_tools` response
        # for no benefit, since a client reading this tool's schema sees the
        # property description in the same payload. Recommends omission
        # rather than an explicit declaration because that's the one path
        # #197 guarantees gets the RIGHT type from the filename automatically
        # (see the `content_type` property's description for why an explicit
        # value must never be second-guessed against the filename).
        description="Upload TEXT content for ingestion (same pipeline as POST "
        "/v1/documents, minus binary files — PDF/DOCX/PNG uploads are REST-only by "
        "design). Content is UTF-8 text. Omit content_type — the server derives it "
        "from filename's extension (.py -> text/x-python, .md -> text/markdown, "
        ".csv -> text/csv, .yaml -> application/yaml, .sql -> application/sql, and "
        "more; full list: docs/reference/file-types.md). See the content_type "
        "parameter below only if you need to declare a type explicitly. Requires "
        "'write' permission.",
        input_schema={
            "type": "object",
            "properties": {
                "api_key": {"type": "string", "description": "Your Inherent API key"},
                "filename": {
                    "type": "string",
                    "description": "Name to store the document under",
                },
                "content": {
                    "type": "string",
                    "description": "The document's text content (UTF-8)",
                },
                "content_type": {
                    "type": "string",
                    "description": "MIME type of the content. Must be one of the "
                    f"{len(SUPPORTED_TEXT_MIME_TYPES)} MCP-eligible types: "
                    f"{_SUPPORTED_TEXT_MIME_TYPES_TEXT}. Binary types are rejected — "
                    "use POST /v1/documents for binary uploads. RECOMMENDED: omit "
                    "this field. When omitted, the type is derived from filename's "
                    "extension when recognized (e.g. main.go -> text/x-go), falling "
                    "back to text/plain only for an unrecognized/absent "
                    "extension (e.g. Dockerfile, Makefile, README, .gitignore, "
                    "archive.tar.gz). There is deliberately no schema `default` here: "
                    "many MCP clients auto-fill an omitted argument from its "
                    "advertised default BEFORE the server ever sees the call, which "
                    "would turn every filename-based derivation into the same fixed "
                    "value regardless of the real extension. A value YOU declare "
                    "explicitly is always honored as-is and never re-derived from "
                    "the filename.",
                },
                "workspace_id": {
                    "type": "string",
                    "description": "Optional: target workspace. Required if your key "
                    "has access to more than one workspace.",
                },
            },
            "required": ["api_key", "filename", "content"],
        },
        permission="write",
        handler=_handle_upload_document,
    ),
}

# Derived view kept for callers/tests that only need the permission map.
_TOOL_PERMISSIONS: dict[str, str] = {name: tool.permission for name, tool in _TOOLS.items()}


async def run_mcp_server() -> None:
    """Run the MCP server via stdio."""
    server = create_mcp_server()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())
