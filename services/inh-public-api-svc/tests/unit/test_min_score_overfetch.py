"""min_score filtering must not under-fill the page (#31).

Weaviate returned exactly `limit` rows and min_score was then applied
client-side, so a page could come back short even when more above-threshold
matches existed. The query over-fetches when a min_score filter is active; the
service truncates back to `limit` after filtering.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from src.models.search import SearchRequest
from src.services.search import SearchService


def _svc() -> SearchService:
    return SearchService(database=MagicMock(), weaviate_url="http://fake")


def test_overfetches_when_min_score_set():
    req = SearchRequest(query="q", limit=10, min_score=0.5, search_mode="keyword")
    body = _svc()._build_graphql("Workspace_X", "User_Y", req, None)
    assert "limit: 30" in body["query"]  # 10 * 3 over-fetch


def test_no_overfetch_without_min_score():
    req = SearchRequest(query="q", limit=10, min_score=0.0, search_mode="keyword")
    body = _svc()._build_graphql("Workspace_X", "User_Y", req, None)
    assert "limit: 10" in body["query"]


def test_overfetch_capped_at_max_page_size():
    req = SearchRequest(query="q", limit=100, min_score=0.5, search_mode="keyword")
    body = _svc()._build_graphql("Workspace_X", "User_Y", req, None)
    assert "limit: 100" in body["query"]  # 100*3 capped at 100
