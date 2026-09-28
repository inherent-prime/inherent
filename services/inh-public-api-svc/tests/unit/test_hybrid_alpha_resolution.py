"""Per-workspace hybrid alpha resolution (inherent#391).

``SearchService.search`` resolves the effective ``alpha`` once, before
calling ``_search_weaviate``: an explicit request value always wins; when the
caller omits it (``alpha=None``, the model's own default), the workspace's
``WORKSPACE_HYBRID_ALPHA`` entry applies, falling back to the global default
(``DEFAULT_HYBRID_ALPHA``, 0.7 -- the same value the field used to default to
directly) when neither is set.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.models.search import SearchRequest
from src.services.search import DEFAULT_HYBRID_ALPHA, SearchService


def _service() -> SearchService:
    return SearchService(database=MagicMock(), weaviate_url="http://fake")


@pytest.mark.asyncio
async def test_no_workspace_override_falls_back_to_global_default():
    """Unchanged behavior: no WORKSPACE_HYBRID_ALPHA configured at all."""
    svc = _service()
    with patch.object(svc, "_search_weaviate", new_callable=AsyncMock) as mock_weaviate:
        mock_weaviate.return_value = []
        request = SearchRequest(query="q", search_mode="hybrid")

        await svc.search(workspace_id="ws1", user_id="u1", request=request)

        passed_request = mock_weaviate.call_args.args[2]
        assert passed_request.alpha == DEFAULT_HYBRID_ALPHA


@pytest.mark.asyncio
async def test_workspace_override_applies_when_request_omits_alpha():
    svc = _service()
    with (
        patch.object(svc, "_search_weaviate", new_callable=AsyncMock) as mock_weaviate,
        patch("src.services.search.settings") as mock_settings,
    ):
        mock_weaviate.return_value = []
        mock_settings.workspace_hybrid_alpha = {"ws_keyword_heavy": 0.3}
        mock_settings.enable_diversification = False
        request = SearchRequest(query="q", search_mode="hybrid")

        await svc.search(workspace_id="ws_keyword_heavy", user_id="u1", request=request)

        passed_request = mock_weaviate.call_args.args[2]
        assert passed_request.alpha == 0.3


@pytest.mark.asyncio
async def test_explicit_request_alpha_always_wins_over_workspace_override():
    """A request's own alpha overrides even a configured workspace default."""
    svc = _service()
    with (
        patch.object(svc, "_search_weaviate", new_callable=AsyncMock) as mock_weaviate,
        patch("src.services.search.settings") as mock_settings,
    ):
        mock_weaviate.return_value = []
        mock_settings.workspace_hybrid_alpha = {"ws_keyword_heavy": 0.3}
        mock_settings.enable_diversification = False
        request = SearchRequest(query="q", search_mode="hybrid", alpha=0.9)

        await svc.search(workspace_id="ws_keyword_heavy", user_id="u1", request=request)

        passed_request = mock_weaviate.call_args.args[2]
        assert passed_request.alpha == 0.9


@pytest.mark.asyncio
async def test_unmapped_workspace_with_other_overrides_configured_gets_global_default():
    """Only the exact workspace_id in the mapping is affected -- every other
    workspace keeps the global default, same as WORKSPACE_VERTICAL_PACKS."""
    svc = _service()
    with (
        patch.object(svc, "_search_weaviate", new_callable=AsyncMock) as mock_weaviate,
        patch("src.services.search.settings") as mock_settings,
    ):
        mock_weaviate.return_value = []
        mock_settings.workspace_hybrid_alpha = {"ws_other": 0.3}
        mock_settings.enable_diversification = False
        request = SearchRequest(query="q", search_mode="hybrid")

        await svc.search(workspace_id="ws_unmapped", user_id="u1", request=request)

        passed_request = mock_weaviate.call_args.args[2]
        assert passed_request.alpha == DEFAULT_HYBRID_ALPHA
