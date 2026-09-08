"""Failure-injection: embedder (embedding provider) HTTP errors must propagate.

If the embedding provider (TEI sidecar, or an OpenAI-compatible backend)
returns a 5xx, rejects the batch, or times out, the embed call must raise so
the chunk-storage step fails and the work is retried — silently returning
empty/zero vectors would poison the index.

Mocking is at the provider boundary (module-level ``_PROVIDER``, the seam
#311 moved the HTTP client behind) — no live TEI.
"""

from __future__ import annotations

import httpx
import pytest
from inh_contracts.embedding import EmbeddingProvider

from src.services import embedder

pytestmark = pytest.mark.failure_injection


class _FailingProvider(EmbeddingProvider):
    """Provider whose embed_batch always raises the given error."""

    def __init__(self, error: BaseException, dim: int = 384) -> None:
        self._error = error
        self._dim = dim

    @property
    def name(self) -> str:
        return "failing"

    @property
    def model_id(self) -> str:
        return "failing-model"

    @property
    def dimension(self) -> int:
        return self._dim

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        raise self._error


@pytest.fixture(autouse=True)
def _reset_embedder_provider(monkeypatch):
    """Reset the cached module-level provider around each test.

    Also collapse to a single attempt so permanent-failure cases stay fast
    after #229 added per-batch retries (default 3 attempts with exponential
    sleep would otherwise stall CI).
    """
    monkeypatch.setattr(embedder, "_PROVIDER", None, raising=False)
    monkeypatch.setenv("EMBEDDING_BATCH_MAX_RETRIES", "1")
    yield
    monkeypatch.setattr(embedder, "_PROVIDER", None, raising=False)


def _http_status_error(status: int = 503) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "http://tei/embed")
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError(f"injected {status}", request=request, response=response)


def _install_failing_provider(monkeypatch, *, error: BaseException) -> None:
    monkeypatch.setattr(embedder, "_PROVIDER", _FailingProvider(error), raising=False)


def test_embed_text_propagates_http_status_error(monkeypatch):
    """A non-2xx from the provider must propagate from embed_text."""
    _install_failing_provider(monkeypatch, error=_http_status_error(503))

    with pytest.raises(httpx.HTTPStatusError):
        embedder.embed_text("some chunk text")


def test_embed_texts_propagates_http_status_error(monkeypatch):
    """Batched embedding must also surface the HTTP error, not swallow it."""
    _install_failing_provider(monkeypatch, error=_http_status_error(413))

    with pytest.raises(httpx.HTTPStatusError):
        embedder.embed_texts(["chunk one", "chunk two"])


def test_embed_text_propagates_timeout(monkeypatch):
    """A request timeout (sidecar overloaded) must propagate from embed_text."""
    _install_failing_provider(
        monkeypatch,
        error=httpx.ReadTimeout("timed out", request=httpx.Request("POST", "/embed")),
    )

    with pytest.raises(httpx.TimeoutException):
        embedder.embed_text("some chunk text")


def test_embed_texts_propagates_connect_error(monkeypatch):
    """A connection error (sidecar down) must propagate from embed_texts."""
    _install_failing_provider(monkeypatch, error=httpx.ConnectError("connection refused"))

    with pytest.raises(httpx.ConnectError):
        embedder.embed_texts(["chunk one", "chunk two"])
