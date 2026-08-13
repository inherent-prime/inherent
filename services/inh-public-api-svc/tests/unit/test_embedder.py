"""Unit tests for public-api TEI embedder — query vs passage (#133 review)."""

from __future__ import annotations

from typing import Any

import pytest


@pytest.fixture(autouse=True)
def _reset_embedder_client(monkeypatch):
    from src.services import embedder

    monkeypatch.setattr(embedder, "_CLIENT", None, raising=False)
    embedder.embed_query.cache_clear()
    yield
    monkeypatch.setattr(embedder, "_CLIENT", None, raising=False)
    embedder.embed_query.cache_clear()


class _StubResponse:
    def __init__(self, payload: Any, status: int = 200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class _StubClient:
    def __init__(self, dim: int = 384):
        self.dim = dim
        self.calls: list[dict] = []

    def post(self, url: str, json: dict) -> _StubResponse:
        self.calls.append({"url": url, "json": json})
        inputs = json["inputs"]
        return _StubResponse([[0.1] * self.dim for _ in inputs])


def _install_stub(monkeypatch, dim: int = 384) -> _StubClient:
    from src.services import embedder

    stub = _StubClient(dim=dim)
    monkeypatch.setattr(embedder, "_client", lambda: stub, raising=True)
    return stub


def test_embed_passage_sends_truncate_true(monkeypatch):
    from src.services.embedder import embed_passage

    stub = _install_stub(monkeypatch)
    vec = embed_passage("A real paragraph that would exceed MiniLM's 256-token cap " * 20)
    assert len(vec) == 384
    assert len(stub.calls) == 1
    assert stub.calls[0]["json"]["inputs"]
    assert stub.calls[0]["json"]["truncate"] is True


def test_embed_query_does_not_send_truncate(monkeypatch):
    from src.services.embedder import embed_query

    stub = _install_stub(monkeypatch)
    embed_query("short search query")
    assert "truncate" not in stub.calls[0]["json"]


def test_embed_passage_is_not_lru_cached(monkeypatch):
    from src.services.embedder import embed_passage

    stub = _install_stub(monkeypatch)
    embed_passage("same chunk body")
    embed_passage("same chunk body")
    assert len(stub.calls) == 2


def test_embed_passage_empty_is_zero_vector_no_http(monkeypatch):
    from src.services.embedder import embed_passage

    stub = _install_stub(monkeypatch)
    vec = embed_passage("")
    assert all(x == 0.0 for x in vec)
    assert stub.calls == []
