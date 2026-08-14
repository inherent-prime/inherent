"""Failure-injection: embedder (TEI sidecar) HTTP errors must propagate.

If the text-embeddings-inference sidecar returns a 5xx, rejects the batch,
or times out, the embed call must raise so the chunk-storage step fails and
the work is retried — silently returning empty/zero vectors would poison the
index.

Mocking is at the httpx boundary (module-level ``_CLIENT``); no live TEI.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest

from src.services import embedder

pytestmark = pytest.mark.failure_injection


@pytest.fixture(autouse=True)
def _reset_embedder_client(monkeypatch):
    """Reset the cached module-level httpx client around each test.

    Also disable real retry sleeps and collapse to a single attempt so
    permanent-failure cases stay fast after #229 added per-batch retries
    (default 3 attempts with exponential sleep would otherwise stall CI).
    """
    monkeypatch.setattr(embedder, "_CLIENT", None, raising=False)
    monkeypatch.setenv("EMBEDDING_BATCH_MAX_RETRIES", "1")
    monkeypatch.setattr(embedder.time, "sleep", lambda _s: None)
    yield
    monkeypatch.setattr(embedder, "_CLIENT", None, raising=False)


def _http_status_error(status: int = 503) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "http://tei/embed")
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError(f"injected {status}", request=request, response=response)


def _install_client(monkeypatch, *, post_side_effect):
    """Install a stub httpx client whose .post raises the given error."""
    client = MagicMock()
    client.post.side_effect = post_side_effect
    monkeypatch.setattr(embedder, "_CLIENT", client, raising=False)
    return client


def test_embed_text_propagates_http_status_error(monkeypatch):
    """A non-2xx from TEI (raise_for_status) must propagate from embed_text."""
    response = MagicMock()
    response.raise_for_status.side_effect = _http_status_error(503)
    _install_client(monkeypatch, post_side_effect=None)
    embedder._CLIENT.post.return_value = response

    with pytest.raises(httpx.HTTPStatusError):
        embedder.embed_text("some chunk text")


def test_embed_texts_propagates_http_status_error(monkeypatch):
    """Batched embedding must also surface the HTTP error, not swallow it."""
    response = MagicMock()
    response.raise_for_status.side_effect = _http_status_error(413)
    _install_client(monkeypatch, post_side_effect=None)
    embedder._CLIENT.post.return_value = response

    with pytest.raises(httpx.HTTPStatusError):
        embedder.embed_texts(["chunk one", "chunk two"])


def test_embed_text_propagates_timeout(monkeypatch):
    """A request timeout (sidecar overloaded) must propagate from embed_text."""
    _install_client(
        monkeypatch,
        post_side_effect=httpx.ReadTimeout("timed out", request=httpx.Request("POST", "/embed")),
    )

    with pytest.raises(httpx.TimeoutException):
        embedder.embed_text("some chunk text")


def test_embed_texts_propagates_connect_error(monkeypatch):
    """A connection error (sidecar down) must propagate from embed_texts."""
    _install_client(
        monkeypatch,
        post_side_effect=httpx.ConnectError("connection refused"),
    )

    with pytest.raises(httpx.ConnectError):
        embedder.embed_texts(["chunk one", "chunk two"])
