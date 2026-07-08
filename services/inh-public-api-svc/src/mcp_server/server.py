"""MCP Server implementation for AI agent integration.

Permission parity (#14)
-----------------------
Every tool validates the supplied API key and then checks that the key carries
the permission the equivalent REST route requires (see ``src/services/auth.py``
and the per-route dependencies). A key missing the required permission gets a
clear ``Error: ...`` response and the tool body NEVER runs — exactly like the
REST 403 path. Permission map:

    search_documents / search_memory   -> "search"
    get_document_context / list_documents -> "read"
    get_citations                       -> "search"
    verify_claim                        -> "read"
    explain_lineage                     -> "read"
    refresh_stale_source                -> "write"
    report_feedback / get_retrieval_health -> "search"
    delete_document                     -> "write"

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
"""

import json

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from src.config.constants import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE
from src.models.api_key import APIKeyInfo
from src.models.evals import FeedbackRequest
from src.services.database import get_database
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

# Required permission per tool — mirrors the REST per-route dependencies (#14).
_TOOL_PERMISSIONS: dict[str, str] = {
    "search_documents": "search",
    "search_memory": "search",
    "get_citations": "search",
    "get_document_context": "read",
    "list_documents": "read",
    "verify_claim": "read",
    "explain_lineage": "read",
    "refresh_stale_source": "write",
    "report_feedback": "search",
    "get_retrieval_health": "search",
    "delete_document": "write",
}

# Schema shared by the two search-shaped tools so they stay identical (#14/#40).
_SEARCH_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "api_key": {"type": "string", "description": "Your Inherent API key"},
        "query": {"type": "string", "description": "The search query"},
        "workspace_id": {
            "type": "string",
            "description": "Optional: specific workspace to search. If omitted, searches all your workspaces.",
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
        """List available MCP tools with versioned, documented input schemas."""
        return [
            Tool(
                name="search_documents",
                description="Search for relevant documents and chunks using semantic, hybrid, or "
                "keyword search. Omit workspace_id to search across ALL your workspaces. "
                "Requires 'search' permission.",
                inputSchema=_SEARCH_INPUT_SCHEMA,
            ),
            Tool(
                name="search_memory",
                description="Memory primitive: retrieve evidence chunks for a query (canonical "
                "agent search). Same parameters and behaviour as search_documents; returns "
                "structured results with scores and provenance. Requires 'search' permission.",
                inputSchema=_SEARCH_INPUT_SCHEMA,
            ),
            Tool(
                name="get_citations",
                description="Run a search and return the claim-level Citation objects attached to "
                "each result (chunk_id, document, character spans, score, provenance, freshness) "
                "so an answer can cite its evidence. Requires 'search' permission.",
                inputSchema=_SEARCH_INPUT_SCHEMA,
            ),
            Tool(
                name="get_document_context",
                description="Get the full content of a document for context. Requires 'read' "
                "permission.",
                inputSchema={
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
            ),
            Tool(
                name="list_documents",
                description="List all documents. Omit workspace_id to list from ALL your "
                "workspaces. Requires 'read' permission.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "api_key": {"type": "string", "description": "Your Inherent API key"},
                        "workspace_id": {
                            "type": "string",
                            "description": "Optional: specific workspace. If omitted, lists from all your workspaces.",
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
            ),
            Tool(
                name="verify_claim",
                description="Memory primitive: verify how well a list of evidence passages "
                "supports a claim (offline lexical strategy). Returns support_level "
                "(strong/weak/none), score and reason. Requires 'read' permission.",
                inputSchema={
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
            ),
            Tool(
                name="explain_lineage",
                description="Memory primitive: explain a document's (or chunk's) provenance and "
                "freshness — source_uri, content_hash, ingested_at, is_stale and document_name — "
                "from already-ingested data. Requires 'read' permission.",
                inputSchema={
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
            ),
            Tool(
                name="refresh_stale_source",
                description="Memory primitive: re-ingest an already-uploaded document to clear "
                "stale evidence (same logic as POST /v1/documents/{id}/refresh). Requires "
                "'write' permission.",
                inputSchema={
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
            ),
            Tool(
                name="report_feedback",
                description="ALWAYS call this after using search results: report whether the "
                "returned evidence answered your query. Your feedback builds this workspace's "
                "retrieval eval set and improves future quality measurement. Pass the "
                "event_id from the search response. Requires 'search' permission.",
                inputSchema=_FEEDBACK_INPUT_SCHEMA,
            ),
            Tool(
                name="get_retrieval_health",
                description="Get the retrieval-quality scorecard for a workspace: answer rate, "
                "verdict distribution, corpus gaps, labeled-case count, and last eval run. Use "
                "it to calibrate how much to trust search results from this corpus. Requires "
                "'search' permission.",
                inputSchema=_HEALTH_INPUT_SCHEMA,
            ),
            Tool(
                name="delete_document",
                description="Memory primitive: permanently delete a document and all of its "
                "derived data — vectors, chunks, and stored bytes (same logic as DELETE "
                "/v1/documents/{id}). Use to retract knowledge that should no longer be "
                "retrievable. Requires 'write' permission.",
                inputSchema={
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
            ),
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

            # Permission parity with REST (#14): check BEFORE executing the body
            # so a denied key never reaches the search/db/verify services.
            required = _TOOL_PERMISSIONS.get(name)
            if required is None:
                return [TextContent(type="text", text=f"Error: Unknown tool '{name}'")]
            if not key_info.has_permission(required):
                return [
                    TextContent(
                        type="text",
                        text=f"Error: API key does not have '{required}' permission",
                    )
                ]

            if name in ("search_documents", "search_memory"):
                return await _handle_search(key_info, arguments)
            elif name == "get_citations":
                return await _handle_get_citations(key_info, arguments)
            elif name == "get_document_context":
                return await _handle_get_context(key_info, arguments)
            elif name == "list_documents":
                return await _handle_list_documents(key_info, arguments)
            elif name == "verify_claim":
                return await _handle_verify_claim(key_info, arguments)
            elif name == "explain_lineage":
                return await _handle_explain_lineage(key_info, arguments)
            elif name == "refresh_stale_source":
                return await _handle_refresh_stale_source(key_info, arguments)
            elif name == "report_feedback":
                return await _handle_report_feedback(key_info, arguments)
            elif name == "get_retrieval_health":
                return await _handle_get_retrieval_health(key_info, arguments)
            elif name == "delete_document":
                return await _handle_delete_document(key_info, arguments)
            else:  # pragma: no cover - guarded by _TOOL_PERMISSIONS above
                return [TextContent(type="text", text=f"Error: Unknown tool '{name}'")]

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

    Returns:
        tuple of (workspace_ids list, error message or None)
    """
    database = await get_database()

    if requested_workspace_id:
        # User specified a workspace - verify they have access
        user_workspaces = await database.get_user_workspace_ids(key_info.user_id)
        if requested_workspace_id not in user_workspaces:
            return [], f"Error: You don't have access to workspace '{requested_workspace_id}'"
        return [requested_workspace_id], None
    else:
        # No workspace specified - use all user's workspaces
        user_workspaces = await database.get_user_workspace_ids(key_info.user_id)
        if not user_workspaces:
            return [], "No workspaces found. Upload documents to create a workspace."
        return user_workspaces, None


async def _run_search(
    key_info: APIKeyInfo,
    arguments: dict,
) -> tuple[list, str | None, str | None]:
    """Shared retrieval used by search_documents/search_memory/get_citations.

    Builds the SearchRequest via the shared ``build_search_request`` helper (so
    it matches REST exactly, #14), fans out over the authorised workspaces, and
    returns (results, requested_workspace_id, error). ``results`` items are
    ``(workspace_id, SearchResult)`` tuples sorted by score and truncated to the
    requested limit.
    """
    requested_workspace_id = arguments.get("workspace_id")
    query = arguments.get("query", "")
    if not query:
        return [], requested_workspace_id, "Error: Query is required"

    workspace_ids, error = await _get_workspace_ids(key_info, requested_workspace_id)
    if error:
        return [], requested_workspace_id, error

    request = build_search_request(arguments)
    search_service: SearchService = await get_search_service()

    tagged: list[tuple[str, object]] = []
    for workspace_id in workspace_ids:
        response = await search_service.search(workspace_id, key_info.user_id, request)
        for result in response.results:
            tagged.append((workspace_id, result))

    tagged.sort(key=_search_rank_key)
    return tagged[: request.limit], requested_workspace_id, None


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
    """Handle search_documents / search_memory tools (#14/#40)."""
    tagged, requested_workspace_id, error = await _run_search(key_info, arguments)
    if error:
        return [TextContent(type="text", text=error)]

    query = arguments.get("query", "")
    if not tagged:
        note = (
            f" in workspace '{requested_workspace_id}'"
            if requested_workspace_id
            else " across your workspaces"
        )
        return _structured(f"No results found for: {query}{note}", {"query": query, "results": []})

    note = (
        f" in workspace '{requested_workspace_id}'"
        if requested_workspace_id
        else " across all workspaces"
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

    return _structured(summary.rstrip(), {"query": query, "results": structured_results})


async def _handle_get_citations(key_info: APIKeyInfo, arguments: dict) -> list[TextContent]:
    """Handle get_citations: run a search and return the Citation objects (#40)."""
    tagged, requested_workspace_id, error = await _run_search(key_info, arguments)
    if error:
        return [TextContent(type="text", text=error)]

    query = arguments.get("query", "")
    citations = []
    for workspace_id, result in tagged:
        if result.citation is not None:
            citations.append({"workspace_id": workspace_id, **result.citation.model_dump()})

    if not citations:
        return _structured(f"No citations found for: {query}", {"query": query, "citations": []})

    summary = f"Found {len(citations)} citations for '{query}':\n\n"
    for i, cit in enumerate(citations, 1):
        summary += (
            f"**{i}. {cit['document_name']}** (score: {cit['score']:.2f}) "
            f"chunk {cit['chunk_id']}\n"
        )

    return _structured(summary.rstrip(), {"query": query, "citations": citations})


async def _handle_get_context(key_info: APIKeyInfo, arguments: dict) -> list[TextContent]:
    """Handle get_document_context tool."""
    document_id = arguments.get("document_id", "")

    if not document_id:
        return [TextContent(type="text", text="Error: Document ID is required")]

    database = await get_database()

    # Get document and verify user has access
    document = await database.get_document_by_id(document_id)

    if not document:
        return [TextContent(type="text", text=f"Error: Document '{document_id}' not found")]

    # Verify user has access to this workspace
    user_workspaces = await database.get_user_workspace_ids(key_info.user_id)
    if document.workspace_id not in user_workspaces:
        return [
            TextContent(
                type="text", text=f"Error: You don't have access to document '{document_id}'"
            )
        ]

    chunks = await database.get_document_chunks_by_doc_id(document_id)
    full_text = "\n\n".join(chunk.content for chunk in chunks)

    result_text = f"# {document.name}\n\n"
    result_text += f"**Source:** {document.source_type}\n"
    result_text += f"**Size:** {document.size_bytes:,} bytes\n"
    result_text += f"**Chunks:** {len(chunks)}\n"
    result_text += f"**Workspace:** {document.workspace_id}\n\n"
    result_text += "---\n\n"
    result_text += full_text

    return [TextContent(type="text", text=result_text)]


async def _handle_list_documents(key_info: APIKeyInfo, arguments: dict) -> list[TextContent]:
    """Handle list_documents tool."""
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
        workspace_note = (
            f" in workspace '{requested_workspace_id}'" if requested_workspace_id else ""
        )
        return [TextContent(type="text", text=f"No documents found{workspace_note}")]

    workspace_note = (
        f" in workspace '{requested_workspace_id}'"
        if requested_workspace_id
        else " across all workspaces"
    )
    result_text = f"Found {total} documents{workspace_note} (showing page {page}):\n\n"
    for doc in documents:
        result_text += f"- **{doc.name}**\n"
        result_text += f"  ID: `{doc.id}`\n"
        result_text += f"  Type: {doc.source_type} | Size: {doc.size_bytes:,} bytes\n"
        result_text += f"  Chunks: {doc.chunk_count} | Status: {doc.status} | Workspace: {doc.workspace_id}\n\n"

    return [TextContent(type="text", text=result_text)]


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
        f"Claim support: **{verdict.support_level}** (score: {verdict.score:.2f})\n"
        f"{verdict.reason}"
    )
    return _structured(summary, verdict.model_dump())


async def _resolve_document_for_user(key_info: APIKeyInfo, document_id: str):
    """Fetch a document by id and verify the user owns its workspace.

    Returns (document, workspace_ids, error_text). On any access failure the
    error_text is set and the document is None, so callers return without ever
    reading further data.
    """
    database = await get_database()
    document = await database.get_document_by_id(document_id)
    if not document:
        return None, [], f"Error: Document '{document_id}' not found"
    user_workspaces = await database.get_user_workspace_ids(key_info.user_id)
    if document.workspace_id not in user_workspaces:
        return None, user_workspaces, f"Error: You don't have access to document '{document_id}'"
    return document, user_workspaces, None


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
    await mq.publish(settings.mq_topic_document_uploaded, mq_message)

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
    """
    database = await get_database()
    workspace_ids = await database.get_user_workspace_ids(key_info.user_id)
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

    Enforces the same workspace-ownership check every other tool uses before
    handing the workspace_id to ``build_scorecard``.
    """
    database = await get_database()
    workspace_ids = await database.get_user_workspace_ids(key_info.user_id)
    workspace_id = arguments["workspace_id"]
    if workspace_id not in workspace_ids:
        return [TextContent(type="text", text="Error: workspace not accessible with this key")]
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


async def run_mcp_server() -> None:
    """Run the MCP server via stdio."""
    server = create_mcp_server()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())
