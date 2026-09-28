"""Per-workspace MCP tool profiles (inherent#392).

Unit-level: mocks ``get_authorized_workspace_ids`` / ``resolve_workspace_pack``
/ ``get_search_service`` at ``src.mcp_server.tool_profiles``'s own boundary --
no real database, no real Weaviate. See ``tests/fixtures/handbook_pack`` (the
#390 generic fixture pack, one non-colliding tool profile) and
``tests/fixtures/collision_pack`` (a second fixture pack whose FIRST tool
profile deliberately shadows a core built-in name) for the fixture packs
these tests load.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from inh_contracts.vertical_discovery import discover_all_packs

from src.mcp_server import tool_profiles
from src.mcp_server.server import _TOOLS
from src.models.api_key import APIKeyInfo
from src.models.search import SearchResponse, SearchResult

pytestmark = pytest.mark.unit

FIXTURE_PACKS_DIR = Path(__file__).resolve().parents[1] / "fixtures"


def _packs() -> dict:
    return discover_all_packs(str(FIXTURE_PACKS_DIR)).packs


def _key(*, workspace_id: str | None = None, permissions=None) -> APIKeyInfo:
    return APIKeyInfo(
        key_id="key-1",
        user_id="user-1",
        workspace_id=workspace_id,
        permissions=permissions or ["read", "search"],
        rate_limit=100,
    )


# --------------------------------------------------------------------------- #
# resolve_effective_pack -- the "simplest safe" workspace-resolution rule
# --------------------------------------------------------------------------- #
class TestResolveEffectivePack:
    async def test_single_authorized_workspace_with_pack_resolves(self):
        vertical = _packs()["handbook"]
        key = _key(workspace_id="ws-1")
        with (
            patch.object(
                tool_profiles, "get_authorized_workspace_ids", AsyncMock(return_value=["ws-1"])
            ),
            patch.object(tool_profiles, "resolve_workspace_pack", return_value=vertical),
        ):
            resolved = await tool_profiles.resolve_effective_pack(key, database=object())
        assert resolved == ("ws-1", vertical)

    async def test_zero_authorized_workspaces_is_ambiguous(self):
        key = _key()
        with patch.object(
            tool_profiles, "get_authorized_workspace_ids", AsyncMock(return_value=[])
        ):
            resolved = await tool_profiles.resolve_effective_pack(key, database=object())
        assert resolved is None

    async def test_multiple_authorized_workspaces_is_ambiguous_even_with_one_shared_pack(self):
        """Simplest safe rule (documented in tool_profiles.py): MORE THAN ONE
        authorized workspace is always ambiguous, even when every one of
        them happens to share the same pack -- there is still no single
        workspace to run a bare `search_sections(query=...)` against."""
        vertical = _packs()["handbook"]
        key = _key()  # user-scoped key, owns several workspaces
        with (
            patch.object(
                tool_profiles,
                "get_authorized_workspace_ids",
                AsyncMock(return_value=["ws-1", "ws-2"]),
            ),
            patch.object(tool_profiles, "resolve_workspace_pack", return_value=vertical),
        ):
            resolved = await tool_profiles.resolve_effective_pack(key, database=object())
        assert resolved is None

    async def test_single_workspace_with_no_pack_bound_resolves_to_none(self):
        key = _key(workspace_id="ws-1")
        with (
            patch.object(
                tool_profiles, "get_authorized_workspace_ids", AsyncMock(return_value=["ws-1"])
            ),
            patch.object(tool_profiles, "resolve_workspace_pack", return_value=None),
        ):
            resolved = await tool_profiles.resolve_effective_pack(key, database=object())
        assert resolved is None


# --------------------------------------------------------------------------- #
# build_profile_tools -- schema shape + name-collision skip
# --------------------------------------------------------------------------- #
class TestBuildProfileTools:
    def test_builds_one_tooldef_per_profile(self):
        vertical = _packs()["handbook"]
        tools = tool_profiles.build_profile_tools(vertical, "ws-1", existing_names=set(_TOOLS))
        assert set(tools) == {"search_sections"}
        tool = tools["search_sections"]
        assert tool.permission == "search"

    def test_enum_filter_field_gets_a_json_schema_enum(self):
        vertical = _packs()["handbook"]
        tools = tool_profiles.build_profile_tools(vertical, "ws-1", existing_names=set(_TOOLS))
        props = tools["search_sections"].input_schema["properties"]
        assert props["section_type"]["enum"] == ["pricing", "security", "other"]
        # `product` is a plain string tag field -- no enum constraint.
        assert "enum" not in props["product"]
        assert props["query"]["type"] == "string"
        assert props["limit"]["default"] == 3  # the fixture pack's default_limit

    def test_query_is_the_only_required_field(self):
        vertical = _packs()["handbook"]
        tools = tool_profiles.build_profile_tools(vertical, "ws-1", existing_names=set(_TOOLS))
        assert tools["search_sections"].input_schema["required"] == ["query"]

    def test_colliding_tool_name_is_skipped_with_a_logged_warning(self):
        """A tool profile named exactly like a core built-in (here,
        `search_documents`) must never be registered -- skipped, not raised
        -- and the pack's OTHER (non-colliding) profile still loads."""
        vertical = _packs()["collision-pack"]
        assert {p.name for p in vertical.tools} == {"search_documents", "search_sections"}

        with patch.object(tool_profiles, "logger") as mock_logger:
            tools = tool_profiles.build_profile_tools(vertical, "ws-1", existing_names=set(_TOOLS))

        assert "search_documents" not in tools  # never shadowed
        assert "search_sections" in tools  # sibling profile unaffected
        mock_logger.warning.assert_called_once()
        warning_args = mock_logger.warning.call_args
        assert "shadows a built-in tool" in warning_args.args[0].lower()
        assert warning_args.kwargs["tool"] == "search_documents"
        assert warning_args.kwargs["pack"] == "collision-pack"


# --------------------------------------------------------------------------- #
# resolve_profile_tools -- the end-to-end "no pack -> {}" contract
# --------------------------------------------------------------------------- #
class TestResolveProfileTools:
    async def test_no_pack_bound_returns_empty_dict(self):
        key = _key(workspace_id="ws-1")
        with (
            patch.object(
                tool_profiles, "get_authorized_workspace_ids", AsyncMock(return_value=["ws-1"])
            ),
            patch.object(tool_profiles, "resolve_workspace_pack", return_value=None),
        ):
            tools = await tool_profiles.resolve_profile_tools(key, database=object())
        assert tools == {}

    async def test_pack_bound_returns_its_profile_tools(self):
        vertical = _packs()["handbook"]
        key = _key(workspace_id="ws-1")
        with (
            patch.object(
                tool_profiles, "get_authorized_workspace_ids", AsyncMock(return_value=["ws-1"])
            ),
            patch.object(tool_profiles, "resolve_workspace_pack", return_value=vertical),
        ):
            tools = await tool_profiles.resolve_profile_tools(key, database=object())
        assert set(tools) == {"search_sections"}


# --------------------------------------------------------------------------- #
# The profile tool handler -- search dispatch + friendly TagFilterError
# --------------------------------------------------------------------------- #
class TestProfileToolHandler:
    def _tool(self, vertical):
        return tool_profiles.build_profile_tools(vertical, "ws-1", existing_names=set(_TOOLS))[
            "search_sections"
        ]

    async def test_missing_query_is_a_friendly_error(self):
        vertical = _packs()["handbook"]
        tool = self._tool(vertical)
        result = await tool.handler(_key(), {})
        assert result[0].text == "Error: Query is required"

    async def test_runs_search_and_returns_structured_results(self):
        vertical = _packs()["handbook"]
        tool = self._tool(vertical)
        response = SearchResponse(
            results=[
                SearchResult(
                    chunk_id="chunk-1",
                    document_id="doc-1",
                    document_name="Handbook.md",
                    content="Section text about pricing.",
                    score=0.91,
                    metadata={"section_heading": "3.2 PRICING"},
                    tags={"section_type": "pricing"},
                    source_url="https://drive.example/doc-1",
                )
            ],
            query="pricing",
            total_results=1,
            processing_time_ms=1.0,
            search_mode="semantic",
        )
        search_service = AsyncMock()
        search_service.search = AsyncMock(return_value=response)
        with patch.object(
            tool_profiles, "get_search_service", AsyncMock(return_value=search_service)
        ):
            result = await tool.handler(_key(), {"query": "pricing", "section_type": "pricing"})

        text = result[0].text
        assert "3.2 PRICING" in text
        import json

        structured = json.loads(text.split("```json\n", 1)[1].rsplit("\n```", 1)[0])["structured"]
        item = structured["results"][0]
        assert item["document_id"] == "doc-1"
        assert item["section_heading"] == "3.2 PRICING"
        assert item["tags"] == {"section_type": "pricing"}
        assert item["source_url"] == "https://drive.example/doc-1"
        assert item["score"] == 0.91

        # The filter argument was actually forwarded to the search request.
        called_request = search_service.search.await_args.args[2]
        assert called_request.filters == {"section_type": "pricing"}
        assert called_request.limit == 3  # the fixture pack's default_limit

    async def test_limit_is_clamped_to_the_profiles_max_limit(self):
        vertical = _packs()["handbook"]
        tool = self._tool(vertical)
        response = SearchResponse(
            results=[], query="q", total_results=0, processing_time_ms=1.0, search_mode="semantic"
        )
        search_service = AsyncMock()
        search_service.search = AsyncMock(return_value=response)
        with patch.object(
            tool_profiles, "get_search_service", AsyncMock(return_value=search_service)
        ):
            await tool.handler(_key(), {"query": "q", "limit": 999})
        called_request = search_service.search.await_args.args[2]
        assert called_request.limit == 10  # the fixture pack's max_limit

    async def test_tag_filter_error_is_a_friendly_message_not_a_raise(self):
        """inherent#392: TagFilterError must surface as a normal "Error: ..."
        tool result, never propagate as an unhandled exception."""
        from src.services.search import TagFilterError

        vertical = _packs()["handbook"]
        tool = self._tool(vertical)
        search_service = AsyncMock()
        search_service.search = AsyncMock(
            side_effect=TagFilterError("unknown filter field(s) ['bogus']")
        )
        with patch.object(
            tool_profiles, "get_search_service", AsyncMock(return_value=search_service)
        ):
            result = await tool.handler(_key(), {"query": "q", "section_type": "pricing"})
        assert result[0].text.startswith("Error: ")
        assert "unknown filter field" in result[0].text
