"""Workspace roles: who may do what inside one workspace (prime#331).

The control plane (Mongo ``workspaces``) stores the owner in ``user_id`` and
everyone else in ``members: [{user_id, role, added_at, added_by}]``. Documents
written before members existed have no ``members`` field at all and behave
exactly as before: the owner is the only person with access.

Role matrix (the API key / OAuth token permission still applies on top: the
effective permission is the INTERSECTION of the two):

    role     read  search  write (upload/delete/edit documents, chunks, conversations)
    owner     yes    yes    yes
    admin     yes    yes    yes
    member    yes    yes    yes
    viewer    yes    yes    no

A role this module does not recognise grants NOTHING (fail closed): a typo in
the control plane must not silently hand out access.
"""

from typing import Any, Literal, get_args

WorkspaceRole = Literal["owner", "admin", "member", "viewer"]

VALID_ROLES: frozenset[str] = frozenset(get_args(WorkspaceRole))

# Roles that may perform write operations. Everything else is read-only.
WRITE_ROLES: frozenset[str] = frozenset({"owner", "admin", "member"})

# Higher wins when a user is (unexpectedly) listed more than once.
_ROLE_RANK: dict[str, int] = {"viewer": 0, "member": 1, "admin": 2, "owner": 3}


def role_in_workspace_doc(doc: dict[str, Any], user_id: str) -> WorkspaceRole | None:
    """The role ``user_id`` holds in one ``workspaces`` document, or None.

    Ids are compared as strings because Mongoose may store either a string or
    an ObjectId for ``user_id`` on both the owner field and each member.
    """
    if doc.get("user_id") is not None and str(doc["user_id"]) == user_id:
        return "owner"

    best: WorkspaceRole | None = None
    for member in doc.get("members") or []:
        if not isinstance(member, dict) or str(member.get("user_id")) != user_id:
            continue
        role = member.get("role")
        if role in VALID_ROLES and (best is None or _ROLE_RANK[role] > _ROLE_RANK[best]):
            best = role
    return best
