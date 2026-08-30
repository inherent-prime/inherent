"""Unit tests for the chunk embedder (ENG-S083).

The embedder is now a thin HTTP client over the text-embeddings-inference
(TEI) sidecar. These tests mock httpx so they run offline and don't pull
the model.
"""

from __future__ import annotations

from typing import Any

import pytest


@pytest.fixture(autouse=True)
def cleanup_test_data():
    """No-op override of the package-level DB-dependent autouse fixture.

    These tests mock httpx; they must not skip when PostgreSQL is down.
    """
    yield


@pytest.fixture(autouse=True)
def _reset_embedder_client(monkeypatch):
    """Reset the module-level httpx client between tests so each test
    can install its own mock without state leaking across tests."""
    from src.services import embedder

    monkeypatch.setattr(embedder, "_CLIENT", None, raising=False)
    yield
    monkeypatch.setattr(embedder, "_CLIENT", None, raising=False)


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
    """Minimal httpx.Client stand-in that records requests and returns
    canned vectors of the configured dimension."""

    def __init__(self, dim: int = 384):
        self.dim = dim
        self.calls: list[dict] = []

    def post(self, url: str, json: dict) -> _StubResponse:
        self.calls.append({"url": url, "json": json})
        inputs = json["inputs"]
        # Generate distinct vectors per input so order/equality assertions
        # in tests can distinguish them.
        vecs = []
        for i, text in enumerate(inputs):
            seed = sum((pos + 1) * ord(char) for pos, char in enumerate(text)) + i + 1
            vecs.append([float(seed) / 1000.0] * self.dim)
        return _StubResponse(vecs)


def _install_stub(monkeypatch, dim: int = 384) -> _StubClient:
    from src.services import embedder

    stub = _StubClient(dim=dim)
    monkeypatch.setattr(embedder, "_client", lambda: stub, raising=True)
    return stub


def test_embed_text_returns_correct_dim(monkeypatch):
    from src.services.embedder import embed_text

    _install_stub(monkeypatch)
    vec = embed_text("How do I authenticate API requests?")
    assert len(vec) == 384
    assert all(isinstance(x, float) for x in vec)


def test_embed_text_empty_returns_zero_vector_no_http(monkeypatch):
    from src.services.embedder import embed_text

    stub = _install_stub(monkeypatch)
    vec = embed_text("")
    assert len(vec) == 384
    assert all(x == 0.0 for x in vec)
    assert stub.calls == [], "empty input must not hit the network"


def test_embed_text_whitespace_only_returns_zero_vector_no_http(monkeypatch):
    from src.services.embedder import embed_text

    stub = _install_stub(monkeypatch)
    vec = embed_text("   \n\t  ")
    assert len(vec) == 384
    assert all(x == 0.0 for x in vec)
    assert stub.calls == [], "whitespace-only input must not hit the network"


def test_embed_texts_batched_preserves_order_and_handles_empties(monkeypatch):
    from src.services.embedder import embed_texts

    stub = _install_stub(monkeypatch)
    out = embed_texts(["hello world", "", "another sentence"])
    assert len(out) == 3
    assert len(out[0]) == 384
    assert all(x == 0.0 for x in out[1])
    assert len(out[2]) == 384
    # Different inputs produce different stub vectors
    assert out[0] != out[2]
    # Only the non-empty positions go over the wire, in one batch
    assert len(stub.calls) == 1
    assert stub.calls[0]["json"]["inputs"] == ["hello world", "another sentence"]


def test_embed_texts_empty_list(monkeypatch):
    from src.services.embedder import embed_texts

    stub = _install_stub(monkeypatch)
    assert embed_texts([]) == []
    assert stub.calls == []


def test_embed_texts_all_empty(monkeypatch):
    from src.services.embedder import embed_texts

    stub = _install_stub(monkeypatch)
    out = embed_texts(["", "  ", "\n"])
    assert len(out) == 3
    for vec in out:
        assert len(vec) == 384
        assert all(x == 0.0 for x in vec)
    assert stub.calls == [], "all-empty input must not hit the network"


def test_embed_text_idempotent_for_same_input(monkeypatch):
    from src.services.embedder import embed_text

    _install_stub(monkeypatch)
    a = embed_text("rotate an API key")
    b = embed_text("rotate an API key")
    assert a == b


def test_embed_texts_single_item(monkeypatch):
    from src.services.embedder import embed_texts

    stub = _install_stub(monkeypatch)
    out = embed_texts(["only one chunk here"])
    assert len(out) == 1
    assert len(out[0]) == 384
    assert len(stub.calls) == 1


def test_embed_dim_overridable_via_env(monkeypatch):
    """Allow upgrading to a different model with a different vector size."""
    monkeypatch.setenv("EMBEDDING_DIM", "768")
    from src.services.embedder import embed_text

    _install_stub(monkeypatch, dim=768)
    vec = embed_text("test")
    assert len(vec) == 768

    # Empty short-circuit also honors the configured dim
    zero = embed_text("")
    assert len(zero) == 768
    assert all(x == 0.0 for x in zero)


def test_embed_texts_chunks_into_batches(monkeypatch):
    """535-chunk PDFs were failing with HTTP 413; embed_texts must batch under EMBEDDING_BATCH_SIZE."""
    from src.services import embedder as emb

    monkeypatch.setenv("EMBEDDING_BATCH_SIZE", "10")
    # Serial path makes assertion order deterministic.
    monkeypatch.setenv("EMBEDDING_MAX_CONCURRENCY", "1")
    captured_batches: list[int] = []

    def fake_post(inputs):
        captured_batches.append(len(inputs))
        return [[0.0] * emb._embedding_dim() for _ in inputs]

    monkeypatch.setattr(emb, "_post_embed", fake_post)
    out = emb.embed_texts([f"chunk-{i}" for i in range(25)])
    assert len(out) == 25
    # 25 chunks at batch=10 -> [10, 10, 5]
    assert captured_batches == [10, 10, 5]


def test_embed_texts_parallel_batches_preserve_order(monkeypatch):
    """#231 phase 1 / #228: concurrent batches must reassemble in input order."""
    from src.services import embedder as emb

    monkeypatch.setenv("EMBEDDING_BATCH_SIZE", "5")
    monkeypatch.setenv("EMBEDDING_MAX_CONCURRENCY", "4")

    def fake_post(inputs):
        # Distinct vector per text (not per batch) so reassembly order is checkable.
        return [[float(ord(t[0]))] * emb._embedding_dim() for t in inputs]

    monkeypatch.setattr(emb, "_post_embed", fake_post)
    texts = [f"{chr(65 + i)}-chunk" for i in range(12)]  # A..L
    out = emb.embed_texts(texts)
    assert len(out) == 12
    # First char of each text is A, B, C, ... so first component tracks order.
    for i, vec in enumerate(out):
        assert vec[0] == float(ord(chr(65 + i)))


def test_embed_batch_retries_transient_failure(monkeypatch):
    """#229: per-batch retry absorbs a single TEI blip without failing the call."""
    import httpx

    from src.services import embedder as emb

    monkeypatch.setenv("EMBEDDING_BATCH_MAX_RETRIES", "3")
    monkeypatch.setenv("EMBEDDING_MAX_CONCURRENCY", "1")
    calls = {"n": 0}

    def flaky_post(inputs):
        calls["n"] += 1
        if calls["n"] < 2:
            raise httpx.ReadTimeout("queue saturated", request=httpx.Request("POST", "/embed"))
        return [[1.0] * emb._embedding_dim() for _ in inputs]

    # Avoid real sleep in unit tests.
    monkeypatch.setattr(emb.time, "sleep", lambda _s: None)
    monkeypatch.setattr(emb, "_post_embed", flaky_post)
    out = emb.embed_texts(["only one"])
    assert len(out) == 1
    assert out[0][0] == 1.0
    assert calls["n"] == 2


def test_embed_batch_retries_exhausted_raise(monkeypatch):
    import httpx

    from src.services import embedder as emb

    monkeypatch.setenv("EMBEDDING_BATCH_MAX_RETRIES", "2")
    monkeypatch.setenv("EMBEDDING_MAX_CONCURRENCY", "1")
    monkeypatch.setattr(emb.time, "sleep", lambda _s: None)

    def always_fail(inputs):
        raise httpx.ConnectError("tei down")

    monkeypatch.setattr(emb, "_post_embed", always_fail)
    with pytest.raises(httpx.ConnectError, match="tei down"):
        emb.embed_texts(["x"])


def test_embed_batch_does_not_retry_client_4xx(monkeypatch):
    """Deterministic 4xx must fail fast — not pad load under the retry loop."""
    import httpx

    from src.services import embedder as emb

    monkeypatch.setenv("EMBEDDING_BATCH_MAX_RETRIES", "5")
    monkeypatch.setenv("EMBEDDING_MAX_CONCURRENCY", "1")
    monkeypatch.setattr(emb.time, "sleep", lambda _s: None)
    calls = {"n": 0}

    def bad_request(inputs):
        calls["n"] += 1
        req = httpx.Request("POST", "/embed")
        resp = httpx.Response(400, request=req)
        raise httpx.HTTPStatusError("bad request", request=req, response=resp)

    monkeypatch.setattr(emb, "_post_embed", bad_request)
    with pytest.raises(httpx.HTTPStatusError):
        emb.embed_texts(["x"])
    assert calls["n"] == 1


def test_embed_batch_retries_http_503(monkeypatch):
    import httpx

    from src.services import embedder as emb

    monkeypatch.setenv("EMBEDDING_BATCH_MAX_RETRIES", "3")
    monkeypatch.setenv("EMBEDDING_MAX_CONCURRENCY", "1")
    monkeypatch.setattr(emb.time, "sleep", lambda _s: None)
    calls = {"n": 0}

    def flaky_503(inputs):
        calls["n"] += 1
        if calls["n"] < 2:
            req = httpx.Request("POST", "/embed")
            resp = httpx.Response(503, request=req)
            raise httpx.HTTPStatusError("unavailable", request=req, response=resp)
        return [[0.5] * emb._embedding_dim() for _ in inputs]

    monkeypatch.setattr(emb, "_post_embed", flaky_503)
    out = emb.embed_texts(["x"])
    assert out[0][0] == 0.5
    assert calls["n"] == 2


# --- embed_texts_with_progress (#298) ---------------------------------------
#
# Async sibling of embed_texts used by store_chunks_with_tenant so
# store_in_weaviate can heartbeat real per-batch progress instead of going
# dark for a whole document's embed. Same batching/retry/ordering contract
# as embed_texts -- these tests focus on what's new: the on_batch_done
# callback and offload-per-batch (rather than offload-the-whole-call).


async def test_embed_texts_with_progress_matches_embed_texts_contract(monkeypatch):
    """Same order/dim/empty-handling contract as the sync embed_texts."""
    from src.services.embedder import embed_texts_with_progress

    stub = _install_stub(monkeypatch)
    out = await embed_texts_with_progress(["hello world", "", "another sentence"])
    assert len(out) == 3
    assert len(out[0]) == 384
    assert all(x == 0.0 for x in out[1])
    assert out[0] != out[2]
    assert len(stub.calls) == 1
    assert stub.calls[0]["json"]["inputs"] == ["hello world", "another sentence"]


async def test_embed_texts_with_progress_empty_list_no_callback(monkeypatch):
    from src.services.embedder import embed_texts_with_progress

    _install_stub(monkeypatch)
    calls: list[tuple[int, int]] = []
    out = await embed_texts_with_progress([], on_batch_done=calls.append)
    assert out == []
    assert calls == []


async def test_embed_texts_with_progress_reports_progress_per_batch(monkeypatch):
    """Progress is real, per-batch signal: on_batch_done must fire once per
    batch with a monotonically increasing completed count that reaches
    (total, total) -- this is what lets store_in_weaviate heartbeat with
    genuine progress during a long store rather than a fixed timer."""
    from src.services import embedder as emb

    monkeypatch.setenv("EMBEDDING_BATCH_SIZE", "10")
    monkeypatch.setenv("EMBEDDING_MAX_CONCURRENCY", "1")  # deterministic order

    def fake_post(inputs):
        return [[0.0] * emb._embedding_dim() for _ in inputs]

    monkeypatch.setattr(emb, "_post_embed", fake_post)

    progress: list[tuple[int, int]] = []
    out = await emb.embed_texts_with_progress(
        [f"chunk-{i}" for i in range(25)],  # -> 3 batches of [10, 10, 5]
        on_batch_done=lambda done, total: progress.append((done, total)),
    )
    assert len(out) == 25
    assert progress == [(1, 3), (2, 3), (3, 3)]


async def test_embed_texts_with_progress_offloads_each_batch_to_a_thread(monkeypatch):
    """The blocking per-batch HTTP call must be offloaded (#19) -- same
    reasoning as embed_texts, now per batch instead of per whole call."""
    from src.services import embedder as emb

    monkeypatch.setenv("EMBEDDING_BATCH_SIZE", "10")
    monkeypatch.setenv("EMBEDDING_MAX_CONCURRENCY", "1")

    offloaded: list[int] = []
    real_to_thread = emb.asyncio.to_thread

    async def spying_to_thread(func, *args, **kwargs):
        offloaded.append(len(args[0]))  # batch size
        return await real_to_thread(func, *args, **kwargs)

    def fake_post(inputs):
        return [[0.0] * emb._embedding_dim() for _ in inputs]

    monkeypatch.setattr(emb, "_post_embed", fake_post)
    monkeypatch.setattr(emb.asyncio, "to_thread", spying_to_thread)

    out = await emb.embed_texts_with_progress([f"chunk-{i}" for i in range(25)])
    assert len(out) == 25
    assert offloaded == [10, 10, 5]


async def test_embed_texts_with_progress_stops_dispatch_when_cancelled(monkeypatch):
    """#298: unlike the old single asyncio.to_thread(embed_texts, ...) call
    (which could not be interrupted once started), a cancellation of the
    enclosing task must stop further, not-yet-started batches from being
    dispatched -- this is the "stop wasting CPU after Temporal has given up"
    half of the issue."""
    import asyncio

    from src.services import embedder as emb

    monkeypatch.setenv("EMBEDDING_BATCH_SIZE", "1")
    monkeypatch.setenv("EMBEDDING_MAX_CONCURRENCY", "1")  # strictly serial

    dispatched: list[str] = []
    release = asyncio.Event()

    def blocking_post(inputs):
        dispatched.append(inputs[0])
        if inputs[0] == "chunk-0":
            # Simulate the first batch being the one that's slow/wedged.
            import time as _time

            while not release.is_set():
                _time.sleep(0.01)
        return [[0.0] * emb._embedding_dim() for _ in inputs]

    monkeypatch.setattr(emb, "_post_embed", blocking_post)

    task = asyncio.ensure_future(emb.embed_texts_with_progress([f"chunk-{i}" for i in range(5)]))
    await asyncio.sleep(0.05)  # let the first batch start and block
    task.cancel()
    release.set()  # let the blocked thread finish so the test can exit cleanly

    with pytest.raises(asyncio.CancelledError):
        await task

    # Only the in-flight batch was dispatched -- batches 2-5 never started.
    assert dispatched == ["chunk-0"]
