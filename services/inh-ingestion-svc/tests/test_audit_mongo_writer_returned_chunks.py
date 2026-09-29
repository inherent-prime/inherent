"""Audit Mongo writer persists the returned chunk ids (inherent#41 / #393).

The public API publishes every retrieval audit event with
``returned_chunk_ids`` -- the provenance link from an audit record back to the
exact chunks a caller was shown (#41), which #393 extends to every MCP tool call
incl. pack profile tools. The writer built its document from a fixed field list
that omitted it, so the ids were silently dropped on the way into Mongo
``audit_logs`` (found by the live pilot-flow E2E). No live Mongo needed.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.services.audit_mongo_writer import upsert_audit_log


def _event(**extra) -> dict:
    return {
        "audit_id": "a1",
        "workspace_id": "ws1",
        "user_id": "u1",
        "api_key_id": "k1",
        "source": "api_key",
        "query_type": "search",
        "query_text": "q",
        "result_count": 2,
        "response_time_ms": 1.5,
        "request_id": "r1",
        "query_timestamp": "2026-01-01T00:00:00Z",
        **extra,
    }


async def _inserted(event: dict) -> dict:
    collection = MagicMock()
    collection.insert_one = AsyncMock()
    client = {"main": {"audit_logs": collection}}
    with patch("src.services.audit_mongo_writer.get_mongo_client", return_value=client):
        await upsert_audit_log(event, "mongodb://x", "main")
    return collection.insert_one.call_args.args[0]


@pytest.mark.asyncio
async def test_returned_chunk_ids_are_persisted():
    doc = await _inserted(_event(returned_chunk_ids=["c1", "c2"]))
    assert doc["returned_chunk_ids"] == ["c1", "c2"]


@pytest.mark.asyncio
async def test_missing_returned_chunk_ids_stores_an_empty_list():
    """An event published before the field existed (or a non-retrieval call)."""
    doc = await _inserted(_event())
    assert doc["returned_chunk_ids"] == []
