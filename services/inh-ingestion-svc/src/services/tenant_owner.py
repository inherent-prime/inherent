"""Which tenant does an uploaded document belong to? The workspace OWNER's.

Weaviate keeps one tenant per user inside a workspace collection, and every
member of a team workspace must see the whole workspace (prime#331). So a
document is always stored under the workspace owner's ``user_id``
(``workspaces.user_id`` in the Mongo control plane), whichever producer sent
it: the public API already puts the owner in ``user_id`` (and the actual
uploader in ``uploaded_by``), while another producer (the platform's own
``document.uploaded`` events) sends the uploading member as ``user_id``.
Resolving here makes both end up in the same tenant.

- Workspace found in Mongo -> its owner.
- Workspace NOT in Mongo (a standalone deployment with no control plane, or a
  legacy workspace) -> the event's own ``user_id``: today's behaviour.
- Mongo unreachable -> the error propagates (an MQ message is redelivered, a
  ``POST /ingest`` gets a 5xx): silently falling back would file a member's
  document into a private tenant nobody else can search. Deployments that run
  without Mongo at all set ``WORKSPACE_OWNER_LOOKUP_ENABLED=false``.

Runs in plain application code (trigger / route), never inside a workflow.
"""

from __future__ import annotations

from typing import Any

from src.config.settings import Settings
from src.services.audit_mongo_writer import get_mongo_client


def _id_variants(workspace_id: str) -> list[Any]:
    """Workspace ``_id`` as a string and, when it looks like one, an ObjectId
    (Mongoose stores ``_id`` as ObjectId)."""
    from bson import ObjectId
    from bson.errors import InvalidId

    variants: list[Any] = [workspace_id]
    try:
        variants.append(ObjectId(workspace_id))
    except (InvalidId, TypeError):
        pass
    return variants


async def resolve_tenant_user_id(settings: Settings, workspace_id: str, event_user_id: str) -> str:
    """The user id whose Weaviate tenant/``processed_documents.user_id`` a
    document uploaded to ``workspace_id`` belongs to (see module docstring)."""
    if not settings.workspace_owner_lookup_enabled:
        return event_user_id

    client = get_mongo_client(settings.mongodb_uri)
    doc = await client[settings.mongodb_db_name]["workspaces"].find_one(
        {"_id": {"$in": _id_variants(workspace_id)}}, {"user_id": 1}
    )
    if doc is None or doc.get("user_id") is None:
        return event_user_id
    return str(doc["user_id"])
