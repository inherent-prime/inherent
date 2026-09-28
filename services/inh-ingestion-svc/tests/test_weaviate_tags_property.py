"""Weaviate `tags` property tests (inherent#390 item 4).

Unit-level, mocked client -- no live Weaviate needed. Verifies the pack-tags
TEXT_ARRAY property exists on the standard chunk schema (so new collections
get it) and that the existing backward-compatible reconciliation path (added
for #41/#42/#44/#129) would add it to an already-existing collection that
predates this feature -- both required by "existing collections must keep
working" (inherent#390's legacy-support requirement).
"""

from __future__ import annotations

from unittest.mock import MagicMock

from weaviate.classes.config import DataType

from src.config.settings import Settings
from src.services.weaviate import WeaviateService


def _settings() -> Settings:
    return Settings(
        DATABASE_URL="postgresql://x/y",
        WEAVIATE_URL="http://localhost:8080",
    )


def test_tags_property_is_a_text_array():
    service = WeaviateService(_settings())
    props = {p.name: p for p in service._get_chunk_properties()}
    assert "tags" in props
    assert props["tags"].dataType == DataType.TEXT_ARRAY


def test_tags_property_not_index_searchable():
    """Exact-match filter tokens, not BM25 prose -- same reasoning as
    chunking_strategy (#129)."""
    service = WeaviateService(_settings())
    props = {p.name: p for p in service._get_chunk_properties()}
    assert props["tags"].indexSearchable is False


def test_reconcile_adds_tags_property_to_existing_collection_missing_it():
    """A collection created before this feature existed has every OTHER
    property but not `tags` -- reconcile must add just that one, additively,
    without touching anything else (existing collections keep working)."""
    service = WeaviateService(_settings())

    existing_props = [p for p in service._get_chunk_properties() if p.name != "tags"]
    mock_collection = MagicMock()
    mock_collection.config.get.return_value.properties = existing_props

    mock_client = MagicMock()
    mock_client.collections.get.return_value = mock_collection
    service.client = mock_client

    service._reconcile_collection_properties("Workspace_TEST")

    added_names = [call.args[0].name for call in mock_collection.config.add_property.call_args_list]
    assert added_names == ["tags"]


def test_reconcile_is_noop_when_tags_already_present():
    service = WeaviateService(_settings())

    mock_collection = MagicMock()
    mock_collection.config.get.return_value.properties = service._get_chunk_properties()

    mock_client = MagicMock()
    mock_client.collections.get.return_value = mock_collection
    service.client = mock_client

    service._reconcile_collection_properties("Workspace_TEST")

    mock_collection.config.add_property.assert_not_called()
