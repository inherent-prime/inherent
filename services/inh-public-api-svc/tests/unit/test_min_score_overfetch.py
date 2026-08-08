"""min_score filtering must not under-fill the page (#31).

Weaviate returned exactly `limit` rows and min_score was then applied
client-side, so a page could come back short even when more above-threshold
matches existed. The query over-fetches when a min_score filter is active; the
service truncates back to `limit` after filtering.

Every test here pins ``enable_diversification`` to ``False`` even though that
is no longer the production default (#146, on by default since 2026-08-06):
this file is scoped to the min_score-only over-fetch behavior (#31) in
isolation, and diversification's own over-fetch widening (which composes with
this one via ``max()``) has its own dedicated coverage in
``test_search_diversification.py::TestFetchLimitComposition`` -- including the
composition case where both are active together. Without pinning it here, a
production-default change to either flag would silently change what these
`limit: N` assertions mean.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.config import settings
from src.models.search import SearchRequest
from src.services.search import SearchService


def _svc() -> SearchService:
    return SearchService(database=MagicMock(), weaviate_url="http://fake")


@pytest.fixture(autouse=True)
def _diversification_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate min_score's own over-fetch from diversification's (#146)."""
    monkeypatch.setattr(settings, "enable_diversification", False)


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
