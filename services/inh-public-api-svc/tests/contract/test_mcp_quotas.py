"""Contract tests: per-identity entitlements/quotas wired into the MCP HTTP
dispatcher (#309).

Complements ``tests/unit/test_quotas.py`` (which exercises ``check_quota`` in
isolation): these tests drive the REAL ``call_tool`` / ``_call_tool_oauth``
handlers from ``http_transport.create_http_mcp_server()``, the same way
``tests/contract/test_mcp_http_transport.py`` does, to pin:

- **Default-open** (#309 design constraint #1): a caller with no entitlement
  record configured is unaffected -- the rate limiter is never touched.
- **The exact rejection shape** (#309 design constraint #3): ``isError=True``
  ``CallToolResult`` with a branchable ``quota_exceeded`` class in
  ``structuredContent``, naming the limit, its value, and (where applicable)
  the reset time -- the SAME shape ``_call_tool_oauth``'s
  ``insufficient_scope`` result already uses on this transport.
- **Quota denial short-circuits before the handler runs** (mirrors
  ``TestHttpCallToolDispatch::test_permission_denied_rejected_before_handler_runs``
  in ``test_mcp_http_transport.py``): a denied ``upload_document`` call never
  reaches the document-intake pipeline.
- Enforcement applies identically to an OAuth ``Principal``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import mcp.types as mcp_types
import pytest
from mcp.types import CallToolResult

from src.mcp_server import http_transport
from src.models.api_key import APIKeyInfo
from src.services.auth import Principal
from src.services.entitlements import Entitlements, set_entitlements_provider

pytestmark = [pytest.mark.contract]


def _key(
    permissions: list[str],
    *,
    workspace_id: str | None = "ws-1",
) -> APIKeyInfo:
    return APIKeyInfo(
        key_id="key-quota",
        user_id="user-quota",
        workspace_id=workspace_id,
        permissions=permissions,  # type: ignore[arg-type]
        rate_limit=100,
        expires_at=None,
        status="active",
    )


async def _call_http_tool(
    name: str, arguments: dict, key_info: APIKeyInfo | None
) -> CallToolResult:
    """Same helper as test_mcp_http_transport.py's -- duplicated locally
    (not imported cross-file) so this file has no coupling to that one's
    internals beyond the shared, already-public http_transport module."""
    server = http_transport.create_http_mcp_server()
    handler = server.request_handlers[mcp_types.CallToolRequest]
    token = http_transport._current_key_info.set(key_info)
    try:
        req = mcp_types.CallToolRequest(
            method="tools/call",
            params=mcp_types.CallToolRequestParams(name=name, arguments=arguments),
        )
        result = await handler(req)
    finally:
        http_transport._current_key_info.reset(token)
    return result.root


class _FixedProvider:
    def __init__(self, entitlements: Entitlements) -> None:
        self._entitlements = entitlements

    async def get_entitlements(self, principal: Principal) -> Entitlements:  # noqa: ARG002
        return self._entitlements


# =========================================================================== #
# Default-open: the pinned proof for #309 design constraint #1
# =========================================================================== #
class TestDefaultOpenBehaviorUnchanged:
    async def test_api_key_with_no_entitlements_configured_is_unaffected(self, sample_document):
        """An API-key principal with no entitlement record (the shipped
        NullEntitlementsProvider, the default in every test unless a test
        calls set_entitlements_provider) behaves EXACTLY as before #309:
        the call succeeds and the rate limiter is never consulted."""
        key = _key(["read"])
        db = AsyncMock()
        db.get_document_by_id = AsyncMock(return_value=sample_document)
        db.get_user_workspace_ids = AsyncMock(return_value=["ws-1"])

        with (
            patch("src.mcp_server.server.get_database", AsyncMock(return_value=db)),
            patch("src.mcp_server.quotas.get_rate_limiter") as mock_get_limiter,
        ):
            result = await _call_http_tool("get_document", {"document_id": "doc-1"}, key)

        assert result.isError is False
        mock_get_limiter.assert_not_called()


# =========================================================================== #
# Rejection shape (#309 design constraint #3)
# =========================================================================== #
class TestQuotaExceededShape:
    async def test_calls_per_minute_exceeded_returns_quota_exceeded_class(self, sample_document):
        set_entitlements_provider(_FixedProvider(Entitlements(calls_per_minute=1)))
        key = _key(["read"])
        db = AsyncMock()
        db.get_document_by_id = AsyncMock(return_value=sample_document)
        db.get_user_workspace_ids = AsyncMock(return_value=["ws-1"])

        with patch("src.mcp_server.server.get_database", AsyncMock(return_value=db)):
            first = await _call_http_tool("get_document", {"document_id": "doc-1"}, key)
            second = await _call_http_tool("get_document", {"document_id": "doc-1"}, key)

        assert first.isError is False
        assert second.isError is True
        assert second.structuredContent["error_class"] == "quota_exceeded"
        assert second.structuredContent["limit"] == "calls_per_minute"
        assert second.structuredContent["limit_value"] == 1
        assert second.structuredContent["reset_at"] is not None
        assert "calls_per_minute" in second.content[0].text

    async def test_quota_exceeded_carries_upgrade_url_when_configured(self, sample_document):
        set_entitlements_provider(
            _FixedProvider(
                Entitlements(calls_per_minute=1, upgrade_url="https://example.com/upgrade")
            )
        )
        key = _key(["read"])
        db = AsyncMock()
        db.get_document_by_id = AsyncMock(return_value=sample_document)
        db.get_user_workspace_ids = AsyncMock(return_value=["ws-1"])

        with patch("src.mcp_server.server.get_database", AsyncMock(return_value=db)):
            await _call_http_tool("get_document", {"document_id": "doc-1"}, key)
            second = await _call_http_tool("get_document", {"document_id": "doc-1"}, key)

        assert second.structuredContent["upgrade_url"] == "https://example.com/upgrade"
        assert "https://example.com/upgrade" in second.content[0].text

    async def test_max_documents_denial_has_no_reset_time(self):
        set_entitlements_provider(_FixedProvider(Entitlements(max_documents=1)))
        key = _key(["read", "write"])
        db = AsyncMock()
        db.user_owns_workspace_in_mongo = AsyncMock(return_value=True)
        db.get_document_count_for_workspaces = AsyncMock(return_value=1)

        with (
            patch("src.mcp_server.http_transport.get_database", AsyncMock(return_value=db)),
            patch("src.services.database.get_database", AsyncMock(return_value=db)),
        ):
            result = await _call_http_tool("upload_document", {"filename": "", "content": ""}, key)

        assert result.isError is True
        assert result.structuredContent["error_class"] == "quota_exceeded"
        assert result.structuredContent["limit"] == "max_documents"
        assert result.structuredContent["reset_at"] is None


# =========================================================================== #
# Denial short-circuits before the handler runs
# =========================================================================== #
class TestQuotaDenialPrecedesHandler:
    async def test_max_documents_at_cap_blocks_before_upload_handler_runs(self):
        """Same args that WOULD reach the handler's own 'content is
        required' validation error if dispatched -- if the response is the
        quota message rather than that validation message, the handler
        never ran."""
        set_entitlements_provider(_FixedProvider(Entitlements(max_documents=1)))
        key = _key(["read", "write"])
        db = AsyncMock()
        db.user_owns_workspace_in_mongo = AsyncMock(return_value=True)
        db.get_document_count_for_workspaces = AsyncMock(return_value=1)

        with (
            patch("src.mcp_server.http_transport.get_database", AsyncMock(return_value=db)),
            patch("src.services.database.get_database", AsyncMock(return_value=db)),
        ):
            result = await _call_http_tool("upload_document", {"filename": "", "content": ""}, key)

        assert result.structuredContent["error_class"] == "quota_exceeded"
        assert "content is required" not in result.content[0].text

    async def test_max_documents_under_cap_lets_upload_handler_run(self):
        """Same setup, but under the cap: the call reaches the real handler,
        which rejects the empty filename/content itself (validation_error,
        not quota_exceeded) -- proving the quota check let it through."""
        set_entitlements_provider(_FixedProvider(Entitlements(max_documents=5)))
        key = _key(["read", "write"])
        db = AsyncMock()
        db.user_owns_workspace_in_mongo = AsyncMock(return_value=True)
        db.get_document_count_for_workspaces = AsyncMock(return_value=1)

        with (
            patch("src.mcp_server.http_transport.get_database", AsyncMock(return_value=db)),
            patch("src.services.database.get_database", AsyncMock(return_value=db)),
        ):
            result = await _call_http_tool("upload_document", {"filename": "", "content": ""}, key)

        assert result.isError is True
        assert result.structuredContent["error_class"] == "validation_error"


# =========================================================================== #
# writes_per_day exhausted -> reads still work (acceptance criterion),
# exercised through the real dispatcher this time.
# =========================================================================== #
class TestWritesPerDayThroughDispatcher:
    async def test_write_quota_exhausted_read_tool_still_succeeds(self, sample_document):
        set_entitlements_provider(_FixedProvider(Entitlements(writes_per_day=1)))
        key = _key(["read", "write"])
        db = AsyncMock()
        db.user_owns_workspace_in_mongo = AsyncMock(return_value=True)
        db.get_document_by_id = AsyncMock(return_value=sample_document)
        db.get_user_workspace_ids = AsyncMock(return_value=["ws-1"])

        with (
            patch("src.mcp_server.server.get_database", AsyncMock(return_value=db)),
            patch("src.mcp_server.http_transport.get_database", AsyncMock(return_value=db)),
        ):
            # upload_document with empty filename/content is a write-tool
            # call that returns its own validation error WITHOUT touching
            # the database or MQ (see server.py's _handle_upload_document) --
            # exactly what's needed here: confirm the FIRST call reaches the
            # handler at all (not quota_exceeded) and consumes one write,
            # without needing to mock the full document-intake pipeline.
            first_write = await _call_http_tool(
                "upload_document", {"filename": "", "content": ""}, key
            )
            second_write = await _call_http_tool(
                "upload_document", {"filename": "", "content": ""}, key
            )
            read_result = await _call_http_tool("get_document", {"document_id": "doc-1"}, key)

        assert first_write.structuredContent["error_class"] != "quota_exceeded"
        assert second_write.isError is True
        assert second_write.structuredContent["error_class"] == "quota_exceeded"
        assert second_write.structuredContent["limit"] == "writes_per_day"
        assert read_result.isError is False


# =========================================================================== #
# OAuth principal: same enforcement, same shape (no workspace-dependent
# checks -- see http_transport._call_tool_oauth's docstring).
# =========================================================================== #
class TestOAuthPrincipalQuota:
    async def test_oauth_principal_over_calls_per_minute_gets_quota_exceeded(self):
        set_entitlements_provider(_FixedProvider(Entitlements(calls_per_minute=1)))
        principal = Principal(
            principal_id="oauth-user-1", principal_type="oauth", scopes=frozenset({"kb:read"})
        )
        first = await http_transport._call_tool_oauth("get_document", principal)
        second = await http_transport._call_tool_oauth("get_document", principal)

        # First call passes quota but hits the (expected, #295) "not yet
        # available" stub -- authentication_failed, not quota_exceeded.
        assert first.structuredContent["error_class"] == "authentication_failed"
        assert second.structuredContent["error_class"] == "quota_exceeded"
        assert second.structuredContent["limit"] == "calls_per_minute"

    async def test_oauth_principal_missing_scope_is_checked_before_quota(self):
        """Scope denial must still take precedence over quota -- an
        unauthorized call was never going anywhere near budget (mirrors the
        API-key path's permission-before-quota ordering)."""
        set_entitlements_provider(_FixedProvider(Entitlements(calls_per_minute=1)))
        principal = Principal(
            principal_id="oauth-user-2", principal_type="oauth", scopes=frozenset()
        )
        result = await http_transport._call_tool_oauth("get_document", principal)
        assert result.structuredContent["error"] == "insufficient_scope"
