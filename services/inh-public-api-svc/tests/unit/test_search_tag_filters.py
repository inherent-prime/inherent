"""Vertical pack tag filter tests (inherent#390 item 5).

Unit-level: mocks the Weaviate HTTP client and `resolve_workspace_pack` (the
documented workspace->pack binding extension point, see
src/services/workspace_pack.py) -- no live services needed.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from inh_contracts.vertical_pack import load_vertical

from src.models.search import SearchRequest
from src.services.search import SearchService, TagFilterError, _get_workspace_collection_name


def _load_fixture_pack():
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "handbook_pack"
    return load_vertical(root)


@pytest.fixture
def search_service():
    mock_database = MagicMock()
    return SearchService(mock_database, "http://weaviate:8080")


@pytest.fixture(autouse=True)
def stub_embed_query(monkeypatch):
    def _fake(text: str) -> tuple[float, ...]:
        return tuple(0.0 for _ in range(384))

    monkeypatch.setattr("src.services.embedder.embed_query", _fake, raising=False)
    monkeypatch.setattr("src.services.search.embed_query", _fake, raising=False)


class TestValidateTagFilters:
    def test_no_filters_is_a_noop(self):
        SearchService._validate_tag_filters("ws1", None)  # must not raise
        SearchService._validate_tag_filters("ws1", {})

    def test_filters_without_bound_pack_raises(self):
        with patch("src.services.search.resolve_workspace_pack", return_value=None):
            with pytest.raises(TagFilterError, match="bound to a vertical pack"):
                SearchService._validate_tag_filters("ws1", {"section_type": "pricing"})

    def test_unknown_field_raises(self):
        vertical = _load_fixture_pack()
        with patch("src.services.search.resolve_workspace_pack", return_value=vertical):
            with pytest.raises(TagFilterError, match="unknown filter field"):
                SearchService._validate_tag_filters("ws1", {"not_a_real_field": "x"})

    def test_known_field_passes(self):
        vertical = _load_fixture_pack()
        with patch("src.services.search.resolve_workspace_pack", return_value=vertical):
            SearchService._validate_tag_filters("ws1", {"section_type": "pricing"})


class TestTagFilterOperands:
    def test_single_value_field(self):
        operands = SearchService._tag_filter_operands({"section_type": "pricing"})
        assert len(operands) == 1
        assert '"tags"' in operands[0]
        assert "ContainsAny" in operands[0]
        assert "section_type=pricing" in operands[0]

    def test_multi_value_any_of(self):
        operands = SearchService._tag_filter_operands({"section_type": ["pricing", "security"]})
        assert "section_type=pricing" in operands[0]
        assert "section_type=security" in operands[0]

    def test_multiple_fields_produce_multiple_operands(self):
        operands = SearchService._tag_filter_operands(
            {"section_type": "pricing", "product": "Core"}
        )
        assert len(operands) == 2

    def test_none_or_empty_produces_no_operands(self):
        assert SearchService._tag_filter_operands(None) == []
        assert SearchService._tag_filter_operands({}) == []


class TestBuildGraphqlWithFilters:
    def test_filters_appear_in_where_clause(self, search_service):
        request = SearchRequest(query="test", limit=5, filters={"section_type": "pricing"})
        gql = search_service._build_graphql(
            "Workspace_TEST", "User_TEST", request, query_vector=[0.0] * 384
        )["query"]
        assert "where" in gql
        assert "section_type=pricing" in gql
        assert "operator: ContainsAny" in gql

    def test_filters_combined_with_document_ids_use_and(self, search_service):
        request = SearchRequest(
            query="test",
            limit=5,
            document_ids=["doc1"],
            filters={"section_type": "pricing"},
        )
        gql = search_service._build_graphql(
            "Workspace_TEST", "User_TEST", request, query_vector=[0.0] * 384
        )["query"]
        assert "operator: And" in gql
        assert "doc1" in gql
        assert "section_type=pricing" in gql

    def test_no_filters_no_document_ids_no_where(self, search_service):
        request = SearchRequest(query="test", limit=5)
        gql = search_service._build_graphql(
            "Workspace_TEST", "User_TEST", request, query_vector=[0.0] * 384
        )["query"]
        assert "where" not in gql


class TestSearchEndToEndValidation:
    @pytest.mark.asyncio
    async def test_search_raises_before_querying_weaviate_when_pack_missing(self, search_service):
        request = SearchRequest(query="test", limit=5, filters={"section_type": "pricing"})
        mock_client = AsyncMock()
        search_service._client = mock_client

        with patch("src.services.search.resolve_workspace_pack", return_value=None):
            with pytest.raises(TagFilterError):
                await search_service.search("ws1", "u1", request)

        mock_client.post.assert_not_called()

    @pytest.mark.asyncio
    async def test_search_result_tags_parsed_from_weaviate_property(self, search_service):
        vertical = _load_fixture_pack()
        request = SearchRequest(query="test", limit=5, filters={"section_type": "pricing"})

        collection_name = _get_workspace_collection_name("ws1")
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "data": {
                "Get": {
                    collection_name: [
                        {
                            "document_id": "d1",
                            "original_filename": "a.txt",
                            "content": "pricing text",
                            "chunk_index": 0,
                            "tags": ["section_type=pricing"],
                            "_additional": {"id": "c1-uuid", "score": "0.9"},
                        }
                    ]
                }
            }
        }
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response
        search_service._client = mock_client

        with patch("src.services.search.resolve_workspace_pack", return_value=vertical):
            response = await search_service.search("ws1", "u1", request)

        assert response.results[0].tags == {"section_type": "pricing"}
