"""Unit tests for the audit event publisher."""

from unittest.mock import AsyncMock, patch

import pytest

from src.services.audit_publisher import (
    MAX_QUERY_TEXT_LENGTH,
    MAX_RESULT_SNIPPETS,
    MAX_SNIPPET_LENGTH,
    build_audit_event,
    publish_audit_event,
    truncate_snippet,
)

# ---------------------------------------------------------------------------
# truncate_snippet
# ---------------------------------------------------------------------------


class TestTruncateSnippet:
    def test_under_limit(self):
        text = "short text"
        assert truncate_snippet(text) == text

    def test_at_limit(self):
        text = "x" * MAX_SNIPPET_LENGTH
        assert truncate_snippet(text) == text

    def test_over_limit(self):
        text = "y" * (MAX_SNIPPET_LENGTH + 50)
        result = truncate_snippet(text)
        assert len(result) == MAX_SNIPPET_LENGTH
        assert result == "y" * MAX_SNIPPET_LENGTH

    def test_custom_limit(self):
        assert truncate_snippet("hello world", max_length=5) == "hello"

    def test_empty_string(self):
        assert truncate_snippet("") == ""


# ---------------------------------------------------------------------------
# build_audit_event
# ---------------------------------------------------------------------------


class TestBuildAuditEvent:
    def _base_kwargs(self):
        return {
            "workspace_id": "ws-1",
            "user_id": "usr-1",
            "api_key_id": "key-1",
            "source": "api_key",
            "query_type": "semantic",
            "query_text": "test query",
            "result_count": 3,
            "response_time_ms": 42.5,
        }

    def test_valid_fields(self):
        event = build_audit_event(**self._base_kwargs())
        assert event["workspace_id"] == "ws-1"
        assert event["user_id"] == "usr-1"
        assert event["api_key_id"] == "key-1"
        assert event["source"] == "api_key"
        assert event["query_type"] == "semantic"
        assert event["query_text"] == "test query"
        assert event["result_count"] == 3
        assert event["response_time_ms"] == 42.5
        assert "audit_id" in event
        assert "query_timestamp" in event
        assert "request_id" in event

    def test_truncates_query_text(self):
        kwargs = self._base_kwargs()
        kwargs["query_text"] = "a" * 3000
        event = build_audit_event(**kwargs)
        assert len(event["query_text"]) == MAX_QUERY_TEXT_LENGTH

    def test_caps_snippets_at_five(self):
        snippets = [{"document_id": f"doc-{i}", "snippet": f"snippet {i}"} for i in range(10)]
        kwargs = self._base_kwargs()
        kwargs["result_snippets"] = snippets
        event = build_audit_event(**kwargs)
        assert len(event["result_snippets"]) == MAX_RESULT_SNIPPETS

    def test_truncates_snippet_content(self):
        snippets = [{"snippet": "z" * 500}]
        kwargs = self._base_kwargs()
        kwargs["result_snippets"] = snippets
        event = build_audit_event(**kwargs)
        assert len(event["result_snippets"][0]["snippet"]) == MAX_SNIPPET_LENGTH

    def test_empty_snippets(self):
        event = build_audit_event(**self._base_kwargs())
        assert event["result_snippets"] == []

    def test_default_query_filters(self):
        event = build_audit_event(**self._base_kwargs())
        assert event["query_filters"] == {}

    def test_custom_request_id(self):
        kwargs = self._base_kwargs()
        kwargs["request_id"] = "req-123"
        event = build_audit_event(**kwargs)
        assert event["request_id"] == "req-123"

    # -----------------------------------------------------------------
    # Attribution fields (inherent#393) — additive, omitted when absent
    # -----------------------------------------------------------------

    def test_attribution_fields_omitted_by_default(self):
        """A caller that omits every #393 kwarg (every pre-#393 call site,
        and every existing test) gets an event with none of the new keys —
        the additive contract the ingestion consumer/writer and prime's
        reader depend on."""
        event = build_audit_event(**self._base_kwargs())
        for key in (
            "principal_type",
            "principal_id",
            "surface",
            "tool_name",
            "workspace_ids",
            "outcome",
        ):
            assert key not in event

    def test_attribution_fields_included_when_provided(self):
        kwargs = self._base_kwargs()
        kwargs.update(
            principal_type="oauth",
            principal_id="oauth-subject-1",
            surface="mcp",
            tool_name="search_documents",
            workspace_ids=["ws-1", "ws-2"],
            outcome="denied",
        )
        event = build_audit_event(**kwargs)
        assert event["principal_type"] == "oauth"
        assert event["principal_id"] == "oauth-subject-1"
        assert event["surface"] == "mcp"
        assert event["tool_name"] == "search_documents"
        assert event["workspace_ids"] == ["ws-1", "ws-2"]
        assert event["outcome"] == "denied"


# ---------------------------------------------------------------------------
# publish_audit_event
# ---------------------------------------------------------------------------


class TestPublishAuditEvent:
    @pytest.mark.asyncio
    async def test_calls_mq_service(self):
        mock_mq = AsyncMock()
        mock_mq.publish = AsyncMock()

        with patch(
            "src.services.mq.get_mq_service",
            new=AsyncMock(return_value=mock_mq),
        ):
            event = {"audit_id": "test-id", "workspace_id": "ws-1"}
            await publish_audit_event(event)
            mock_mq.publish.assert_called_once()
            call_args = mock_mq.publish.call_args
            assert call_args[0][1] == event

    @pytest.mark.asyncio
    async def test_swallows_errors(self):
        """publish_audit_event must never raise, even when MQ fails."""
        with patch(
            "src.services.mq.get_mq_service",
            new=AsyncMock(side_effect=ConnectionError("redis down")),
        ):
            # Should NOT raise
            await publish_audit_event({"audit_id": "fail-test"})

    @pytest.mark.asyncio
    async def test_swallows_publish_errors(self):
        mock_mq = AsyncMock()
        mock_mq.publish = AsyncMock(side_effect=RuntimeError("publish failed"))

        with patch(
            "src.services.mq.get_mq_service",
            new=AsyncMock(return_value=mock_mq),
        ):
            await publish_audit_event({"audit_id": "fail-test-2"})
