"""Whose vectors does a request touch? The workspace OWNER's (prime#331).

Weaviate keeps one tenant per user inside a workspace collection, and
``processed_documents.user_id`` records that tenant. For a team workspace
every member must see the whole workspace, so the tenant ("data-plane
identity") is always the workspace OWNER (``workspaces.user_id`` in Mongo),
never the individual caller. The caller stays the identity for everything
about *who did it*: audit events, eval capture, feedback, quotas,
entitlements, ``whoami`` and ``processed_documents.uploaded_by``.

Legacy / standalone workspaces that have no Mongo record keep today's
behaviour: the caller's own id. That is safe because membership comes from
Mongo only, so a caller can only reach such a workspace as its legacy owner,
never as a member.

The lookup is live (no cache) and raises if Mongo is unreachable, like the
other authorization reads.
"""

from src.services.database import DatabaseService


async def data_plane_user_ids(
    database: DatabaseService, workspace_ids: list[str], caller_user_id: str
) -> dict[str, str]:
    """``{workspace_id: tenant user id}`` for every id in ``workspace_ids``."""
    owners = await database.get_workspace_owners_in_mongo(workspace_ids)
    return {ws: owners.get(ws, caller_user_id) for ws in workspace_ids}


async def data_plane_user_id(
    database: DatabaseService, workspace_id: str, caller_user_id: str
) -> str:
    """The tenant user id for one workspace (see module docstring)."""
    return (await data_plane_user_ids(database, [workspace_id], caller_user_id))[workspace_id]
