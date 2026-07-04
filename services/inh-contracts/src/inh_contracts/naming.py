"""Weaviate naming — the SINGLE source of truth (#12).

Both the ingestion service and the public API derive Weaviate collection and
tenant names from raw workspace/user ids. The two services MUST agree
byte-for-byte or search will query the wrong collection/tenant.

The derivation is *injective*: distinct ids always map to distinct names. The
previous implementation stripped every non-alphanumeric character, so ids that
differed only in punctuation (``ws-123``, ``ws_123``, ``ws123``) collapsed onto
one collection/tenant — a cross-tenant data leak (#1). We now base32-encode the
raw id: RFC4648 base32 emits only ``A-Z`` and ``2-7`` (all valid in Weaviate
collection *and* tenant names) and is reversible, so no two ids can collide.

Golden behavior (anti-drift, asserted by both services' contract tests):
- ws_local_001    -> Workspace_O5ZV63DPMNQWYXZQGAYQ
- ws-123          -> Workspace_O5ZS2MJSGM
- local-dev-user  -> User_NRXWGYLMFVSGK5RNOVZWK4Q
- user_001        -> User_OVZWK4S7GAYDC
"""

import base64

# Multi-tenant naming prefixes.
WORKSPACE_COLLECTION_PREFIX = "Workspace_"
USER_TENANT_PREFIX = "User_"


def _encode_id(raw_id: str) -> str:
    """Injectively encode a raw id into Weaviate's allowed name charset.

    base32 (RFC4648) emits only ``A-Z`` and ``2-7`` — valid in both collection
    and tenant names — and is a reversible bijection, so distinct ids can never
    produce the same name. Trailing ``=`` padding (not allowed in names) is
    stripped; because it is purely positional it does not affect injectivity.
    """
    return base64.b32encode(raw_id.encode("utf-8")).decode("ascii").rstrip("=")


def get_workspace_collection_name(workspace_id: str) -> str:
    """Generate a valid, collision-free Weaviate collection name from workspace ID.

    Weaviate collection names must start with an uppercase letter and contain
    only alphanumeric characters and underscores — satisfied by the
    ``Workspace_`` prefix plus the base32-encoded id.
    """
    return f"{WORKSPACE_COLLECTION_PREFIX}{_encode_id(workspace_id)}"


def get_user_tenant_name(user_id: str) -> str:
    """Generate a valid, collision-free Weaviate tenant name from user ID."""
    return f"{USER_TENANT_PREFIX}{_encode_id(user_id)}"
