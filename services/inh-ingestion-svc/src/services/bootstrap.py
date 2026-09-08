"""Seed the principal required by a checkout-free release stack.

This ports the two identity-store upserts from ``scripts/dev/bootstrap.sh``.
It intentionally seeds one principal: the script's second principal remains a
local contributor-only tenancy fixture and must never reach adopter stacks.
"""

from __future__ import annotations

import hashlib
import json
from uuid import uuid4

import psycopg2
import structlog
from motor.motor_asyncio import AsyncIOMotorClient

from src.config.settings import Settings

logger = structlog.get_logger(__name__)

_PERMISSIONS = ["read", "write", "search"]
_ACTIONS = ("seed", "create", "revoke")
_REVOKE_API_KEY = """
UPDATE api_keys SET status = 'revoked'
WHERE key_prefix = %s AND status = 'active';
"""
_UPSERT_API_KEY = """
INSERT INTO api_keys
    (key_id, key_hash, key_prefix, user_id, workspace_id, name,
     status, permissions, rate_limit)
VALUES (%s, %s, %s, %s, %s, %s, 'active', %s::jsonb, %s)
ON CONFLICT (key_hash) DO UPDATE
SET status = 'active',
    user_id = EXCLUDED.user_id,
    workspace_id = EXCLUDED.workspace_id,
    permissions = EXCLUDED.permissions;
"""


async def run_bootstrap(settings: Settings) -> None:
    """Dispatch one bootstrap action; every action is idempotent or refuses.

    ``seed`` is what compose runs at start-up: one API key and its owned
    workspace. ``create`` and ``revoke`` back `inherent keys create|revoke`,
    which reach this container instead of opening a database of their own.
    """
    action = (settings.bootstrap_action or "seed").strip().lower()
    if action not in _ACTIONS:
        raise ValueError(f"BOOTSTRAP_ACTION must be one of {', '.join(_ACTIONS)}; got {action!r}")

    if action == "revoke":
        _revoke_key(settings)
        return

    key_prefix = _write_api_key(settings)
    # Both actions must leave the key's workspace existing in Mongo. `seed`
    # owns the workspace and keeps its name current; `create` only guarantees
    # existence, because it must not reset a workspace the operator renamed.
    await _upsert_workspace(settings, rename=action == "seed")

    # This reaches CI and user logs: emit only the display-safe prefix.
    logger.info(
        "Bootstrap principal ready",
        action=action,
        key_prefix=key_prefix,
        workspace_id=settings.bootstrap_workspace_id,
    )


def _write_api_key(settings: Settings) -> str:
    """Upsert one api_keys row. Returns the display-safe prefix."""
    api_key = settings.bootstrap_api_key
    workspace_id = settings.bootstrap_workspace_id
    user_id = settings.bootstrap_user_id

    # The public API rejects every other prefix. Validate all bootstrap input
    # before opening either store so malformed configuration cannot partly seed.
    if not api_key or not api_key.startswith("ink_"):
        raise ValueError("BOOTSTRAP_API_KEY must start with 'ink_'")
    if not workspace_id or not user_id:
        raise ValueError("BOOTSTRAP_WORKSPACE_ID and BOOTSTRAP_USER_ID must be set")

    key_prefix = api_key[:12]
    key_hash = hashlib.sha256(api_key.encode()).hexdigest()

    # Mint every attempted insert's id. The conflict arbiter is key_hash, while
    # key_id is independently UNIQUE; reusing a fixed id would break rotation.
    with psycopg2.connect(settings.database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                _UPSERT_API_KEY,
                (
                    str(uuid4()),
                    key_hash,
                    key_prefix,
                    user_id,
                    workspace_id,
                    settings.bootstrap_key_name,
                    json.dumps(_PERMISSIONS),
                    1000,
                ),
            )
    return key_prefix


async def _upsert_workspace(settings: Settings, *, rename: bool) -> None:
    """Ensure the Mongo workspace this key is bound to exists.

    Without this, `create` minted a key pointing at a workspace that was never
    created: `whoami` then reported `workspace_id` set but `workspace_ids`
    empty, and every request with that key failed authorization with 403.

    ``rename=False`` writes the fields only when the document is inserted, so
    re-running against an existing workspace cannot overwrite an operator's
    chosen name.
    """
    fields = {
        "user_id": settings.bootstrap_user_id,
        "name": settings.bootstrap_workspace_name,
    }
    client: AsyncIOMotorClient = AsyncIOMotorClient(settings.mongodb_uri)
    try:
        # Ownership lookup returns str(_id), so the document id is the workspace
        # id itself rather than a separate generated Mongo ObjectId.
        await client[settings.mongodb_db_name].workspaces.update_one(
            {"_id": settings.bootstrap_workspace_id},
            {"$set": fields} if rename else {"$setOnInsert": fields},
            upsert=True,
        )
    finally:
        client.close()


def _revoke_key(settings: Settings) -> None:
    """Revoke exactly one active key by prefix, or fail without changing state.

    A prefix is not unique by construction, so an ambiguous match must abort
    rather than revoke several keys at once. Raising inside the connection
    context rolls the UPDATE back.
    """
    prefix = (settings.bootstrap_key_prefix or "").strip()
    if not prefix.startswith("ink_"):
        raise ValueError("BOOTSTRAP_KEY_PREFIX must be an 'ink_' key prefix")

    with psycopg2.connect(settings.database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(_REVOKE_API_KEY, (prefix,))
            matched = cursor.rowcount
            if matched == 0:
                raise ValueError(f"No active API key with prefix {prefix}")
            if matched > 1:
                raise ValueError(
                    f"{matched} active keys share prefix {prefix}; refusing to revoke them all"
                )

    logger.info("API key revoked", key_prefix=prefix)
