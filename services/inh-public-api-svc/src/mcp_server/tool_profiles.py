"""Per-workspace MCP tool profiles (inherent#392).

A vertical pack (inherent#390) may declare ``tools: [ToolProfile, ...]`` in
its manifest -- a named, pre-scoped search ("search_sections", filtered to
this pack's own tag schema, with its own default/max result limits). This
module turns those profiles into real MCP ``ToolDef`` entries, dynamically,
for a caller whose workspace is bound to a pack.

HTTP-only, by design
---------------------
Profile tools are wired into ``list_tools``/``call_tool`` on the Streamable
HTTP transport ONLY (``src/mcp_server/http_transport.py``) -- stdio
(``server.py::run_mcp_server``) is completely unaffected and its
``list_tools`` stays exactly ``_TOOLS``, byte for byte.

This is not a stylistic choice: stdio's ``list_tools`` callback takes no
request-scoped identity at all -- every stdio tool call carries its OWN
``api_key`` as a plain argument (see ``server.py``'s module docstring,
"HTTP transport parity"), but ``list_tools`` itself is asked once, before
any tool call, with no argument to authenticate. There is therefore no
caller identity available at the point stdio decides what to advertise, so
"expose different tools to different callers" has no way to work there.
HTTP, by contrast, authenticates the CONNECTION via the ``X-API-Key`` /
``Authorization`` header before either RPC even runs (``mount_mcp_http``'s
ASGI gate), and stashes the resolved ``APIKeyInfo`` in the
``_current_key_info`` contextvar that both ``list_tools`` and ``call_tool``
can read -- exactly the seam this module hangs off of.

Workspace resolution: the simplest safe choice
-----------------------------------------------
A caller may be authorized for zero, one, or many workspaces (#138), and
different workspaces may be bound to different packs, or the same pack, or
no pack at all. Rather than namespacing every profile tool's name by
workspace (which would make a pack's own tool names -- documented in the
pack itself -- unpredictable to a caller who doesn't yet know which
workspaces they'll be authorized for), ``resolve_effective_pack`` picks the
SIMPLEST SAFE rule:

    Profile tools are advertised only when the caller's authorized
    workspace set (``get_authorized_workspace_ids``) has EXACTLY ONE
    member, and that one workspace is bound to a pack with at least one
    tool profile.

A caller authorized for zero or several workspaces sees no profile tools at
all -- this covers "several workspaces, different packs" (no safe default)
and "several workspaces, the SAME pack" (still ambiguous: which of the
several would `search_sections(query=...)` search?) with one rule, and
never with a subtler special case that must itself be gotten right. A
workspace-scoped API key (the common case for anyone actually using a
pack-bound workspace) always resolves to exactly one, so this costs that
caller nothing.

Name collisions
----------------
A pack's tool profile name must never shadow one of core's own built-in
tool names (`search_documents`, `whoami`, ...) -- a caller could otherwise
be surprised that `search_documents` from the same conversation sometimes
means the built-in and sometimes means a pack's own re-declaration of it.
``build_profile_tools`` skips (never raises) a colliding profile name, with
a logged warning naming the pack and the tool -- see its docstring. This is
NOT enforced at pack load time (``inh_contracts.vertical_pack.load_vertical``
does not, and must not, know this service's own built-in tool names --
that would be a domain-generic contract module reaching back into one
consumer's private registry), so a pack otherwise loads and serves its
OTHER tool profiles unaffected by one bad name.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from mcp.types import TextContent

from src.models.api_key import APIKeyInfo
from src.services.auth import get_authorized_workspace_ids
from src.services.search import (
    TagFilterError,
    build_search_request,
    get_search_service,
)
from src.services.workspace_pack import resolve_workspace_pack
from src.utils import get_logger

if TYPE_CHECKING:
    from inh_contracts.vertical_pack import ToolProfile, Vertical

    from src.mcp_server.server import ToolDef

logger = get_logger(__name__)


async def resolve_effective_pack(key_info: APIKeyInfo, database) -> tuple[str, "Vertical"] | None:
    """Return ``(workspace_id, pack)`` for the ONE workspace+pack this
    caller's profile tools apply to, or ``None`` when there isn't one.

    ``None`` whenever: the caller is authorized for zero or MORE THAN ONE
    workspace (ambiguous -- see this module's docstring), the caller's one
    workspace has no pack bound, or that pack declares no tool profiles at
    all. Every case degrades to "no profile tools", never an error --
    exactly like ``resolve_workspace_pack`` itself degrading a broken/absent
    pack mapping to ``None`` rather than raising.
    """
    authorized = await get_authorized_workspace_ids(key_info, database)
    if len(authorized) != 1:
        return None
    workspace_id = authorized[0]
    vertical = resolve_workspace_pack(workspace_id)
    if vertical is None or not vertical.tools:
        return None
    return workspace_id, vertical


def _profile_input_schema(profile: "ToolProfile", vertical: "Vertical") -> dict:
    """Build a profile tool's ``inputSchema`` (inherent#392): required
    ``query``, optional ``limit`` bounded by the profile's own
    ``default_limit``/``max_limit``, and one optional parameter per
    ``filters`` field -- an enum tag field gets a JSON Schema ``enum`` of its
    allowed values, any other field type gets a plain string.
    """
    properties: dict = {
        "query": {"type": "string", "description": "The search query"},
        "limit": {
            "type": "integer",
            "description": f"Maximum results (1-{profile.max_limit}, default "
            f"{profile.default_limit})",
            "default": profile.default_limit,
        },
    }
    for field_name in profile.filters:
        field = vertical.tags.fields[field_name]  # #390's load_vertical already validated this
        prop: dict = {"type": "string", "description": f"Filter results by '{field_name}'"}
        if field.type == "enum" and field.values:
            prop["enum"] = field.values
        properties[field_name] = prop
    return {"type": "object", "properties": properties, "required": ["query"]}


def _make_profile_handler(profile: "ToolProfile", workspace_id: str):
    """Build the ``ToolHandler`` closure for one profile tool.

    Runs the existing search path (``build_search_request`` +
    ``SearchService.search`` -- the SAME helper/service ``search_documents``
    uses, #14/#392) against ``workspace_id`` with ``filters`` built from the
    caller's ``profile.filters`` arguments, then renders results in the same
    structured-output style as ``search_documents``
    (``server.py::_handle_search``): text, section heading (when the pack's
    chunking put one in ``metadata['section_heading']``, #390), tags,
    ``source_url`` (#391), document id/title, and score.
    """

    async def handler(key_info: APIKeyInfo, arguments: dict) -> list[TextContent]:
        from src.mcp_server.server import _structured  # local import: avoids a module cycle

        query = arguments.get("query", "")
        if not query:
            return [TextContent(type="text", text="Error: Query is required")]

        try:
            limit = int(arguments.get("limit", profile.default_limit))
        except (TypeError, ValueError):
            limit = profile.default_limit
        limit = max(1, min(profile.max_limit, limit))

        filters = {
            field: arguments[field] for field in profile.filters if arguments.get(field) is not None
        }

        request_params: dict = {"query": query, "limit": limit}
        if filters:
            request_params["filters"] = filters
        request = build_search_request(request_params)

        search_service = await get_search_service()
        try:
            response = await search_service.search(workspace_id, key_info.user_id, request)
        except TagFilterError as exc:
            # Friendly error (inherent#392): a caller passing a filter value
            # outside its enum, or (if the pack changed underneath a stale
            # binding) a field the pack no longer declares, gets a plain
            # "Error: ..." message -- never a raw 500/traceback.
            return [TextContent(type="text", text=f"Error: {exc}")]

        if not response.results:
            return _structured(
                f"No results found for: {query}",
                {"query": query, "results": [], "workspace_id": workspace_id},
            )

        summary = (
            f"Found {len(response.results)} results for '{query}' (profile: {profile.name}):\n\n"
        )
        structured_results = []
        for i, result in enumerate(response.results, 1):
            section_heading = (result.metadata or {}).get("section_heading")
            heading_note = f" — {section_heading}" if section_heading else ""
            summary += (
                f"**{i}. {result.document_name}**{heading_note} (score: {result.score:.2f})\n"
            )
            summary += f"Document ID: {result.document_id}\n"
            summary += (
                f"```\n{result.content[:500]}{'...' if len(result.content) > 500 else ''}\n```\n\n"
            )
            structured_results.append(
                {
                    "document_id": result.document_id,
                    "document_name": result.document_name,
                    "section_heading": section_heading,
                    "content": result.content,
                    "score": result.score,
                    "tags": result.tags,
                    "source_url": result.source_url,
                }
            )

        return _structured(
            summary.rstrip(),
            {
                "query": query,
                "results": structured_results,
                "workspace_id": workspace_id,
            },
        )

    return handler


def build_profile_tools(
    vertical: "Vertical", workspace_id: str, existing_names: set
) -> dict[str, "ToolDef"]:
    """Turn ``vertical.tools`` into real ``ToolDef`` entries.

    Skips (with a logged warning, never a raised error -- see this module's
    docstring) any profile whose ``name`` collides with a name in
    ``existing_names`` (core's own built-in tool names) -- that pack's OTHER
    tool profiles still load normally.
    """
    from src.mcp_server.server import ToolDef  # local import: avoids a module cycle

    tools: dict[str, ToolDef] = {}
    for profile in vertical.tools:
        if profile.name in existing_names:
            logger.warning(
                "vertical pack tool profile shadows a built-in tool name; skipped",
                pack=vertical.manifest.name,
                tool=profile.name,
            )
            continue
        tools[profile.name] = ToolDef(
            description=profile.description,
            input_schema=_profile_input_schema(profile, vertical),
            # Profile tools are search-shaped (a scoped, filtered query) --
            # same permission as search_documents/search_memory (#14).
            permission="search",
            handler=_make_profile_handler(profile, workspace_id),
        )
    return tools


async def resolve_profile_tools(key_info: APIKeyInfo, database) -> dict[str, "ToolDef"]:
    """Return this caller's profile-tool ``ToolDef``s for the HTTP
    transport's registry, or ``{}`` when none apply (see
    ``resolve_effective_pack``).

    ``existing_names`` is computed from ``server.py``'s live ``_TOOLS`` at
    call time (not imported at module load) to avoid a circular import
    between this module and ``server.py``.
    """
    from src.mcp_server.server import _TOOLS  # local import: avoids a module cycle

    resolved = await resolve_effective_pack(key_info, database)
    if resolved is None:
        return {}
    workspace_id, vertical = resolved
    return build_profile_tools(vertical, workspace_id, existing_names=set(_TOOLS))
