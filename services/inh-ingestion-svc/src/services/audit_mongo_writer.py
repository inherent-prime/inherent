"""MongoDB writer for audit log documents.

Uses the ``motor`` async driver to write audit events into the
``audit_logs`` collection.  Provides a singleton client with
idempotent upsert semantics (DuplicateKeyError is swallowed).
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from typing import Any

import structlog
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo.errors import DuplicateKeyError

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Singleton motor client
# ---------------------------------------------------------------------------

_client: AsyncIOMotorClient | None = None
_client_uri: str | None = None
_lock = threading.Lock()


def get_mongo_client(mongo_uri: str) -> AsyncIOMotorClient:
    """Return a singleton AsyncIOMotorClient for the given URI.

    Raises:
        RuntimeError: If called with a different URI than the one used
            to initialize the singleton.  Callers must close the client
            via ``close_mongo_client()`` before switching URIs.
    """
    global _client, _client_uri
    if _client is None:
        with _lock:
            if _client is None:
                _client = AsyncIOMotorClient(mongo_uri)
                _client_uri = mongo_uri
                logger.info("Motor MongoDB client created")
                return _client
    if _client_uri != mongo_uri:
        raise RuntimeError("Mongo client already initialized with a different URI")
    return _client


async def close_mongo_client() -> None:
    """Close the singleton motor client (idempotent)."""
    global _client, _client_uri
    if _client is not None:
        _client.close()
        _client = None
        _client_uri = None
        logger.info("Motor MongoDB client closed")


# ---------------------------------------------------------------------------
# Upsert
# ---------------------------------------------------------------------------


def _parse_timestamp(value: Any) -> datetime:
    """Parse an ISO-format string to datetime, or return as-is if already datetime."""
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        # Handle the trailing 'Z' that some serializers emit
        cleaned = value.replace("Z", "+00:00")
        return datetime.fromisoformat(cleaned)
    return datetime.now(UTC)


async def upsert_audit_log(
    event: dict[str, Any],
    mongo_uri: str,
    db_name: str,
    collection_name: str = "audit_logs",
) -> bool:
    """Insert an audit log document. Returns True on insert, False on duplicate.

    The document shape matches the ``IAuditLog`` MongoDB schema defined in
    Phase A (intg-svc).  A ``feedback`` subdocument with null defaults is
    included for later user feedback.
    """
    client = get_mongo_client(mongo_uri)
    db = client[db_name]
    collection = db[collection_name]

    now = datetime.now(UTC)
    query_ts = _parse_timestamp(event.get("query_timestamp"))

    doc = {
        "_id": event["audit_id"],
        "audit_id": event["audit_id"],
        "workspace_id": event["workspace_id"],
        "user_id": event["user_id"],
        "api_key_id": event.get("api_key_id"),
        "source": event["source"],
        "query_type": event["query_type"],
        "query_text": event["query_text"],
        "query_filters": event.get("query_filters", {}),
        "result_count": event["result_count"],
        "result_snippets": event.get("result_snippets", []),
        "llm_response": event.get("llm_response"),
        "response_time_ms": event["response_time_ms"],
        "request_id": event.get("request_id"),
        "query_timestamp": query_ts,
        # Attribution fields (inherent#393): additive -- absent on every event
        # published before this change, so `.get(...)` with no default keeps
        # those documents storing exactly `None` for these paths, same as any
        # other optional field above (e.g. `llm_response`).
        "principal_type": event.get("principal_type"),
        "principal_id": event.get("principal_id"),
        "surface": event.get("surface"),
        "tool_name": event.get("tool_name"),
        "workspace_ids": event.get("workspace_ids", []),
        "outcome": event.get("outcome", "ok"),
        "feedback": {
            "rating": None,
            "note": None,
            "rated_at": None,
            "rated_by": None,
        },
        "created_at": now,
        "updated_at": now,
    }

    try:
        await collection.insert_one(doc)
        logger.info("Audit log inserted", audit_id=event["audit_id"])
        return True
    except DuplicateKeyError:
        logger.info("Audit log duplicate (idempotent skip)", audit_id=event["audit_id"])
        return False
