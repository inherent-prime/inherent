"""MCP tool-call audit logging (inherent#393).

Before this module, MCP tool calls published NO audit event at all — only
REST's ``POST /v1/search`` did (``src/api/v1/search.py``). Every retrieval-
returning MCP tool call (``search_documents``, ``search_memory``,
``get_citations``, ``get_document_context``, ``list_chunks``, and any
vertical-pack profile tool built by ``tool_profiles.py``) is now logged with
its caller, timestamp, and returned chunk ids, for BOTH stdio/HTTP API-key
callers and OAuth callers alike.

One choke point, not one call per handler
------------------------------------------
Rather than have each of the ~20 tool handlers in ``server.py`` publish its
own audit event (easy to add a new tool and forget), this module is called
from the THREE places a tool call is actually dispatched:

    - ``server.py``'s stdio ``call_tool``
    - ``http_transport.py``'s HTTP ``call_tool`` (API-key callers)
    - ``http_transport.py``'s ``_call_tool_oauth`` (OAuth callers)

Each of those calls ``dispatch_and_audit`` in place of directly awaiting
``tool.handler``, and ``audit_denied`` at each point a call is rejected
before the handler ever runs (missing permission/scope, quota exceeded, no
linked OAuth identity). A tool not marked ``ToolDef.returns_chunk_content``
(the default) is dispatched/denied exactly as before, with no audit event —
this module only logs calls that can return retrieved chunk content, mirroring
REST's own "search calls" scope for #393.

Reporting returned chunk ids without changing every handler's signature
-------------------------------------------------------------------------
``ToolHandler`` is ``Callable[[APIKeyInfo, dict], Awaitable[list[TextContent]]]``
— it has no return channel for "which chunk ids did this call actually
return". Rather than widen that signature for every handler (most of which
have nothing to report), a handler that wants its returned ids captured
calls ``record_returned_chunk_ids`` (and, if it searched a specific
workspace set, ``record_workspace_ids``) — a small, typed, contextvar-based
mechanism that is a safe no-op when called outside an audited dispatch (e.g.
directly from a unit test), so no handler needs to guard the call itself.
"""

from __future__ import annotations

import asyncio
import time
from contextvars import ContextVar
from typing import Any, Literal

from src.models.api_key import APIKeyInfo
from src.services.audit_publisher import build_audit_event, publish_audit_event
from src.utils import get_logger

logger = get_logger(__name__)

Outcome = Literal["ok", "error", "denied"]

# Set by `dispatch_and_audit` before a handler runs and read back afterwards.
# `None` outside of an audited dispatch (direct handler unit tests, or a tool
# that doesn't return chunk content) -- `record_returned_chunk_ids` /
# `record_workspace_ids` are then no-ops, never a crash.
_returned_chunk_ids: ContextVar[list[str] | None] = ContextVar("mcp_audit_chunk_ids", default=None)
_reported_workspace_ids: ContextVar[list[str] | None] = ContextVar(
    "mcp_audit_workspace_ids", default=None
)


def record_returned_chunk_ids(chunk_ids: list[str]) -> None:
    """A handler calls this with the chunk_ids it is about to return, so the
    dispatch-loop choke point can attribute them to this call's audit event.

    Safe no-op outside an audited dispatch (see module docstring) — a handler
    never needs to check whether it is being called from an audited context.
    """
    holder = _returned_chunk_ids.get()
    if holder is not None:
        holder.clear()
        holder.extend(chunk_ids)


def record_workspace_ids(workspace_ids: list[str]) -> None:
    """A handler calls this with the workspace(s) it actually searched.

    Safe no-op outside an audited dispatch (see module docstring).
    """
    holder = _reported_workspace_ids.get()
    if holder is not None:
        holder.clear()
        holder.extend(workspace_ids)


def _principal_from_key_info(key_info: APIKeyInfo) -> tuple[str, str]:
    """Derive ``(principal_type, principal_id)`` from ``key_info.key_id``.

    An OAuth caller is synthesized with ``key_id="oauth:<principal_id>"``
    (``http_transport._api_key_info_for_oauth``, inherent#392's follow-up) --
    reading that prefix back out is the single source of truth for both
    transports and both principal types, so neither dispatch loop needs to
    thread an extra "was this OAuth" parameter through just for audit
    logging; everything downstream already carries a ``key_id`` shaped this
    way.
    """
    key_id = key_info.key_id
    if key_id.startswith("oauth:"):
        return "oauth", key_id[len("oauth:") :]
    return "api_key", key_id


def _query_text(arguments: dict) -> str:
    """Best-effort human-readable "what was asked" for the audit event.

    Search-shaped tools carry ``query``; document-scoped tools
    (``get_document_context``, ``list_chunks``) carry ``document_id``
    instead -- fall back to that so the event is never blank.
    """
    query = arguments.get("query")
    if query:
        return str(query)
    document_id = arguments.get("document_id")
    if document_id:
        return f"document_id={document_id}"
    return ""


def _fire_and_forget_publish(event: dict[str, Any], *, tool_name: str) -> None:
    """Schedule ``publish_audit_event`` without blocking or failing the tool
    call — the MCP-side equivalent of REST's ``BackgroundTasks.add_task``.

    There is no request/response framework here to hand the task to, so a
    plain ``asyncio.create_task`` is used instead; ``publish_audit_event``
    itself already swallows every exception and only logs (see
    ``src/services/audit_publisher.py``), so a failure to publish can never
    surface as a tool-call failure. If no running event loop is available
    (defensive; every dispatch site here runs inside one), the event is
    dropped with a warning rather than raising.
    """
    try:
        asyncio.create_task(publish_audit_event(event))
    except RuntimeError:
        logger.warning("mcp_audit_publish_skipped_no_running_loop", tool=tool_name)


def _publish(
    *,
    tool_name: str,
    arguments: dict,
    surface: str,
    outcome: Outcome,
    principal_type: str,
    principal_id: str,
    user_id: str,
    api_key_id: str,
    chunk_ids: list[str],
    workspace_ids: list[str],
    elapsed_ms: float,
) -> None:
    """Build one audit event for an MCP tool call and fire-and-forget it."""
    event = build_audit_event(
        # `workspace_id` (the legacy single-value field, #41) is the first
        # of the actually-queried set when there is one, else "unknown" --
        # a denied/errored call with no resolved workspace yet still needs
        # SOME value since the field is required upstream (see
        # audit_publisher.build_audit_event and the ingestion validator's
        # required-fields list).
        workspace_id=workspace_ids[0] if workspace_ids else "unknown",
        user_id=user_id,
        api_key_id=api_key_id,
        # `source` stays one of REST's existing values (never "mcp") so
        # prime's audit-log reader — whose Mongoose schema enums `source` to
        # exactly {api_key, dashboard, chat} — never has to change; the NEW
        # `surface` field (below) is what distinguishes an MCP call from a
        # REST one.
        source="api_key",
        query_type="search",
        query_text=_query_text(arguments),
        result_count=len(chunk_ids),
        returned_chunk_ids=chunk_ids,
        response_time_ms=elapsed_ms,
        principal_type=principal_type,
        principal_id=principal_id,
        surface=surface,
        tool_name=tool_name,
        workspace_ids=workspace_ids,
        outcome=outcome,
    )
    _fire_and_forget_publish(event, tool_name=tool_name)


async def dispatch_and_audit(
    tool: Any,  # ToolDef -- Any to avoid a server.py <-> audit.py import cycle
    name: str,
    key_info: APIKeyInfo,
    arguments: dict,
    *,
    surface: str = "mcp",
) -> list:
    """Run ``tool.handler`` and, for a retrieval-returning tool, publish
    exactly one audit event for the call.

    A tool NOT marked ``returns_chunk_content`` is dispatched exactly as
    before, with no audit event and no contextvar bookkeeping overhead.

    Fire-and-forget, like REST's audit publishing: this can never block or
    fail the tool call. On a handler exception, the audit event is published
    with ``outcome="error"`` and the exception is then re-raised UNCHANGED,
    so every existing caller's own error handling (the try/except around
    ``tool.handler`` in each of the three dispatch loops) is unaffected.
    A handler that instead returns its error as ``"Error: ..."`` text (the
    established convention across this module's handlers, see
    ``server.py``'s docstring) is also logged with ``outcome="error"`` and
    no returned chunk ids, never "ok" with zero results — those are not the
    same thing to an auditor.
    """
    if not getattr(tool, "returns_chunk_content", False):
        return await tool.handler(key_info, arguments)

    principal_type, principal_id = _principal_from_key_info(key_info)
    chunk_token = _returned_chunk_ids.set([])
    workspace_token = _reported_workspace_ids.set([])
    started = time.time()
    try:
        content = await tool.handler(key_info, arguments)
    except Exception:
        _publish(
            tool_name=name,
            arguments=arguments,
            surface=surface,
            outcome="error",
            principal_type=principal_type,
            principal_id=principal_id,
            user_id=key_info.user_id,
            api_key_id=key_info.key_id,
            chunk_ids=[],
            workspace_ids=list(_reported_workspace_ids.get() or []),
            elapsed_ms=(time.time() - started) * 1000,
        )
        raise
    finally:
        chunk_ids = list(_returned_chunk_ids.get() or [])
        workspace_ids = list(_reported_workspace_ids.get() or [])
        _returned_chunk_ids.reset(chunk_token)
        _reported_workspace_ids.reset(workspace_token)

    # Handlers report failure as "Error: ..." text rather than raising (the
    # established convention -- see server.py's module docstring), so that
    # is the other place an "error" outcome comes from.
    is_error = bool(content) and getattr(content[0], "text", "").startswith("Error:")
    _publish(
        tool_name=name,
        arguments=arguments,
        surface=surface,
        outcome="error" if is_error else "ok",
        principal_type=principal_type,
        principal_id=principal_id,
        user_id=key_info.user_id,
        api_key_id=key_info.key_id,
        chunk_ids=[] if is_error else chunk_ids,
        workspace_ids=workspace_ids,
        elapsed_ms=(time.time() - started) * 1000,
    )
    return content


def audit_denied(
    tool: Any,  # ToolDef
    name: str,
    key_info: APIKeyInfo,
    arguments: dict,
    *,
    surface: str = "mcp",
) -> None:
    """Publish an ``outcome="denied"`` audit event for a call rejected
    before ``tool.handler`` ever ran (missing permission/scope, quota
    exceeded) — only when the tool is retrieval-returning; see this module's
    docstring for the same gate ``dispatch_and_audit`` applies.
    """
    if not getattr(tool, "returns_chunk_content", False):
        return
    principal_type, principal_id = _principal_from_key_info(key_info)
    _publish(
        tool_name=name,
        arguments=arguments,
        surface=surface,
        outcome="denied",
        principal_type=principal_type,
        principal_id=principal_id,
        user_id=key_info.user_id,
        api_key_id=key_info.key_id,
        chunk_ids=[],
        workspace_ids=[],
        elapsed_ms=0.0,
    )


def audit_denied_unresolved_oauth(
    tool: Any,  # ToolDef
    name: str,
    principal_id: str,
    arguments: dict,
    *,
    surface: str = "mcp",
) -> None:
    """Publish a ``denied`` audit event for an OAuth caller who passed
    scope/quota checks but has NO Inherent identity linked
    (``Principal.resolved_user_id is None`` — see
    ``http_transport._call_tool_oauth``'s docstring). There is no
    synthesized ``APIKeyInfo`` at this point (no resolved ``user_id`` to put
    one together with), so this takes the token's bare subject directly
    instead of going through ``_principal_from_key_info``.
    """
    if not getattr(tool, "returns_chunk_content", False):
        return
    _publish(
        tool_name=name,
        arguments=arguments,
        surface=surface,
        outcome="denied",
        principal_type="oauth",
        principal_id=principal_id,
        user_id="unknown",
        api_key_id=f"oauth:{principal_id}",
        chunk_ids=[],
        workspace_ids=[],
        elapsed_ms=0.0,
    )
