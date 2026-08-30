"""Unit tests for the chunk embedder (ENG-S083, #311).

#311 moved the HTTP client, wire format, batching, and retry logic into the
shared ``inh_contracts.embedding`` package (see that package's own tests for
wire-format-fixture and retry-wall-clock coverage). This module now only
resolves env/Settings into a provider and wires it into the shared batching/
retry helpers -- these tests exercise exactly that: env plumbing (dimension,
batch size, concurrency, retry count, provider selection), the public
``embed_text``/``embed_texts`` behavior contracts (zero-vector shortcuts,
positional preservation, batching, concurrency, retry pass-through), and that
no embedding API key ever reaches a log line.
"""

from __future__ import annotations

import httpx
import pytest
from inh_contracts.embedding import EmbeddingProvider


@pytest.fixture(autouse=True)
def cleanup_test_data():
    """No-op override of the package-level DB-dependent autouse fixture.

    These tests mock the provider; they must not skip when PostgreSQL is down.
    """
    yield


@pytest.fixture(autouse=True)
def _reset_embedder_provider(monkeypatch):
    """Reset the module-level provider between tests so each test can
    install its own fake without state leaking across tests."""
    from src.services import embedder

    monkeypatch.setattr(embedder, "_PROVIDER", None, raising=False)
    yield
    monkeypatch.setattr(embedder, "_PROVIDER", None, raising=False)


class _FakeProvider(EmbeddingProvider):
    """Stand-in that records every embed_batch call and returns deterministic vectors."""

    def __init__(self, dim: int = 384):
        self.dim = dim
        self.calls: list[list[str]] = []

    @property
    def name(self) -> str:
        return "fake"

    @property
    def model_id(self) -> str:
        return "fake-model"

    @property
    def dimension(self) -> int:
        return self.dim

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        vecs = []
        for text in texts:
            # Depends only on the text's own content (not its position in
            # this particular batch) so a vector for the same text is equal
            # regardless of how callers happen to have batched it.
            seed = sum((pos + 1) * ord(char) for pos, char in enumerate(text)) + 1
            vecs.append([float(seed) / 1000.0] * self.dim)
        return vecs


def _install_fake(monkeypatch, dim: int = 384) -> _FakeProvider:
    from src.services import embedder

    fake = _FakeProvider(dim=dim)
    monkeypatch.setattr(embedder, "_PROVIDER", fake, raising=False)
    return fake


# --- basic behavior contracts (unchanged by #311) --------------------------------------------


def test_embed_text_returns_correct_dim(monkeypatch):
    from src.services.embedder import embed_text

    _install_fake(monkeypatch)
    vec = embed_text("How do I authenticate API requests?")
    assert len(vec) == 384
    assert all(isinstance(x, float) for x in vec)


def test_embed_text_empty_returns_zero_vector_no_http(monkeypatch):
    from src.services.embedder import embed_text

    fake = _install_fake(monkeypatch)
    vec = embed_text("")
    assert len(vec) == 384
    assert all(x == 0.0 for x in vec)
    assert fake.calls == [], "empty input must not hit the network"


def test_embed_text_whitespace_only_returns_zero_vector_no_http(monkeypatch):
    from src.services.embedder import embed_text

    fake = _install_fake(monkeypatch)
    vec = embed_text("   \n\t  ")
    assert len(vec) == 384
    assert all(x == 0.0 for x in vec)
    assert fake.calls == [], "whitespace-only input must not hit the network"


def test_embed_texts_batched_preserves_order_and_handles_empties(monkeypatch):
    from src.services.embedder import embed_texts

    fake = _install_fake(monkeypatch)
    out = embed_texts(["hello world", "", "another sentence"])
    assert len(out) == 3
    assert len(out[0]) == 384
    assert all(x == 0.0 for x in out[1])
    assert len(out[2]) == 384
    # Different inputs produce different fake vectors
    assert out[0] != out[2]
    # Only the non-empty positions go over the wire, in one batch
    assert len(fake.calls) == 1
    assert fake.calls[0] == ["hello world", "another sentence"]


def test_embed_texts_empty_list(monkeypatch):
    from src.services.embedder import embed_texts

    fake = _install_fake(monkeypatch)
    assert embed_texts([]) == []
    assert fake.calls == []


def test_embed_texts_all_empty(monkeypatch):
    from src.services.embedder import embed_texts

    fake = _install_fake(monkeypatch)
    out = embed_texts(["", "  ", "\n"])
    assert len(out) == 3
    for vec in out:
        assert len(vec) == 384
        assert all(x == 0.0 for x in vec)
    assert fake.calls == [], "all-empty input must not hit the network"


def test_embed_text_idempotent_for_same_input(monkeypatch):
    from src.services.embedder import embed_text

    _install_fake(monkeypatch)
    a = embed_text("rotate an API key")
    b = embed_text("rotate an API key")
    assert a == b


def test_embed_texts_single_item(monkeypatch):
    from src.services.embedder import embed_texts

    fake = _install_fake(monkeypatch)
    out = embed_texts(["only one chunk here"])
    assert len(out) == 1
    assert len(out[0]) == 384
    assert len(fake.calls) == 1


def test_embed_dim_overridable_via_env(monkeypatch):
    """Allow upgrading to a different model with a different vector size."""
    monkeypatch.setenv("EMBEDDING_DIM", "768")
    from src.services.embedder import embed_text

    _install_fake(monkeypatch, dim=768)
    vec = embed_text("test")
    assert len(vec) == 768

    # Empty short-circuit also honors the configured dim
    zero = embed_text("")
    assert len(zero) == 768
    assert all(x == 0.0 for x in zero)


def test_embed_texts_chunks_into_batches(monkeypatch):
    """535-chunk PDFs were failing with HTTP 413; embed_texts must batch under EMBEDDING_BATCH_SIZE."""
    from src.services.embedder import embed_texts

    monkeypatch.setenv("EMBEDDING_BATCH_SIZE", "10")
    # Serial path makes assertion order deterministic.
    monkeypatch.setenv("EMBEDDING_MAX_CONCURRENCY", "1")
    fake = _install_fake(monkeypatch)

    out = embed_texts([f"chunk-{i}" for i in range(25)])
    assert len(out) == 25
    # 25 chunks at batch=10 -> [10, 10, 5]
    assert [len(c) for c in fake.calls] == [10, 10, 5]


def test_embed_texts_parallel_batches_preserve_order(monkeypatch):
    """#231 phase 1 / #228: concurrent batches must reassemble in input order."""
    from src.services.embedder import embed_texts

    monkeypatch.setenv("EMBEDDING_BATCH_SIZE", "5")
    monkeypatch.setenv("EMBEDDING_MAX_CONCURRENCY", "4")
    _install_fake(monkeypatch)

    texts = [f"{chr(65 + i)}-chunk" for i in range(12)]  # A..L
    out = embed_texts(texts)
    assert len(out) == 12
    # First char of each text determines the fake vector's seed magnitude
    # in a way that's monotonic with position -- reuse embed_text on each
    # single input as the ground truth instead of re-deriving the formula.
    for i, vec in enumerate(out):
        single = _FakeProvider(dim=384)
        expected = single.embed_batch([texts[i]])[0]
        assert vec == expected


# --- retry pass-through (env plumbing; the algorithm itself is tested in inh-contracts) -------


def test_embed_batch_retries_transient_failure(monkeypatch):
    """#229: per-batch retry absorbs a single TEI blip without failing the call."""
    from src.services.embedder import embed_texts

    monkeypatch.setenv("EMBEDDING_BATCH_MAX_RETRIES", "3")
    monkeypatch.setenv("EMBEDDING_MAX_CONCURRENCY", "1")
    fake = _install_fake(monkeypatch)
    calls = {"n": 0}
    real_embed_batch = fake.embed_batch

    def flaky(texts):
        calls["n"] += 1
        if calls["n"] < 2:
            raise httpx.ReadTimeout("queue saturated", request=httpx.Request("POST", "/embed"))
        return real_embed_batch(texts)

    monkeypatch.setattr(fake, "embed_batch", flaky)
    out = embed_texts(["only one"])
    assert len(out) == 1
    assert calls["n"] == 2


def test_embed_batch_retries_exhausted_raise(monkeypatch):
    from src.services.embedder import embed_texts

    monkeypatch.setenv("EMBEDDING_BATCH_MAX_RETRIES", "2")
    monkeypatch.setenv("EMBEDDING_MAX_CONCURRENCY", "1")
    fake = _install_fake(monkeypatch)

    def always_fail(texts):
        raise httpx.ConnectError("tei down")

    monkeypatch.setattr(fake, "embed_batch", always_fail)
    with pytest.raises(httpx.ConnectError, match="tei down"):
        embed_texts(["x"])


def test_embed_batch_does_not_retry_client_4xx(monkeypatch):
    """Deterministic 4xx must fail fast -- not pad load under the retry loop."""
    from src.services.embedder import embed_texts

    monkeypatch.setenv("EMBEDDING_BATCH_MAX_RETRIES", "5")
    monkeypatch.setenv("EMBEDDING_MAX_CONCURRENCY", "1")
    fake = _install_fake(monkeypatch)
    calls = {"n": 0}

    def bad_request(texts):
        calls["n"] += 1
        req = httpx.Request("POST", "/embed")
        resp = httpx.Response(400, request=req)
        raise httpx.HTTPStatusError("bad request", request=req, response=resp)

    monkeypatch.setattr(fake, "embed_batch", bad_request)
    with pytest.raises(httpx.HTTPStatusError):
        embed_texts(["x"])
    assert calls["n"] == 1


def test_embed_query_path_uses_configured_batch_max_retries(monkeypatch):
    """embed_text's retry count also honors EMBEDDING_BATCH_MAX_RETRIES."""
    from src.services.embedder import embed_text

    monkeypatch.setenv("EMBEDDING_BATCH_MAX_RETRIES", "1")
    fake = _install_fake(monkeypatch)
    calls = {"n": 0}

    def always_fail(texts):
        calls["n"] += 1
        raise httpx.ConnectError("down")

    monkeypatch.setattr(fake, "embed_batch", always_fail)
    with pytest.raises(httpx.ConnectError):
        embed_text("x")
    # max_retries=1 -> exactly one attempt, no retry.
    assert calls["n"] == 1


# --- provider selection ------------------------------------------------------------------------


def test_default_provider_is_tei(monkeypatch):
    """TEI stays the default (#311 item 8) -- no env vars set at all."""
    from inh_contracts.embedding import TEIProvider

    from src.services import embedder

    provider = embedder._provider()
    assert isinstance(provider, TEIProvider)


def test_embedding_provider_env_selects_openai_compatible(monkeypatch):
    from inh_contracts.embedding import OpenAICompatibleProvider

    from src.services import embedder

    monkeypatch.setenv("EMBEDDING_PROVIDER", "openai_compatible")
    provider = embedder._provider()
    assert isinstance(provider, OpenAICompatibleProvider)


def test_get_active_embedding_identity_reflects_configured_model_and_dim(monkeypatch):
    from src.services import embedder

    monkeypatch.setenv("EMBEDDING_MODEL_ID", "some/other-model")
    monkeypatch.setenv("EMBEDDING_DIM", "111")
    identity = embedder.get_active_embedding_identity()
    assert identity.model_id == "some/other-model"
    assert identity.dimension == 111


# --- API key never leaks into logs (#311 item 2) ------------------------------------------------
#
# structlog in this service is configured with PrintLoggerFactory (see
# src/utils/logger.py), which writes straight to stdout rather than through
# stdlib `logging` -- so pytest's `caplog` fixture would silently capture
# nothing and the test would pass vacuously. Instead, replace the module's
# `logger` with a spy that records every call verbatim, and assert the
# secret never appears in ANY argument passed to it -- this exercises the
# actual log call sites directly, independent of how structlog renders them.


class _LogSpy:
    def __init__(self) -> None:
        self.records: list[tuple[str, tuple, dict]] = []

    def _record(self, event: str, *args: object, **kwargs: object) -> None:
        self.records.append((event, args, kwargs))

    def info(self, event: str, *args: object, **kwargs: object) -> None:
        self._record(event, *args, **kwargs)

    def warning(self, event: str, *args: object, **kwargs: object) -> None:
        self._record(event, *args, **kwargs)

    def error(self, event: str, *args: object, **kwargs: object) -> None:
        self._record(event, *args, **kwargs)

    def rendered(self) -> str:
        return "\n".join(f"{event} {args} {kwargs}" for event, args, kwargs in self.records)


def test_embedding_api_key_never_appears_in_logs(monkeypatch):
    """The blocker: an API key must never reach a log line, in the URL or headers."""
    from src.services import embedder

    spy = _LogSpy()
    monkeypatch.setattr(embedder, "logger", spy, raising=True)
    secret = "sk-super-secret-embedding-key-12345"  # noqa: S105 -- test fixture value
    monkeypatch.setenv("EMBEDDING_API_KEY", secret)
    monkeypatch.setenv("EMBEDDING_SERVICE_URL", "http://tei.local:80")

    embedder._provider()  # constructs + logs "embedder_client_initialized"

    assert spy.records, "expected the init log call to have fired"
    assert secret not in spy.rendered()


def test_embedding_api_key_embedded_in_url_is_redacted_from_logs(monkeypatch):
    """Defense in depth: a key an operator mistakenly puts IN the URL must
    also never reach a log line (#311 item 2 -- 'redact any key embedded in
    a URL')."""
    from src.services import embedder

    spy = _LogSpy()
    monkeypatch.setattr(embedder, "logger", spy, raising=True)
    secret = "sk-in-the-url-secret"  # noqa: S105 -- test fixture value
    monkeypatch.setenv("EMBEDDING_SERVICE_URL", f"http://{secret}@tei.local:80")

    embedder._provider()

    assert spy.records, "expected the init log call to have fired"
    assert secret not in spy.rendered()


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

    fake = _install_fake(monkeypatch)
    out = await embed_texts_with_progress(["hello world", "", "another sentence"])
    assert len(out) == 3
    assert len(out[0]) == 384
    assert all(x == 0.0 for x in out[1])
    assert out[0] != out[2]
    assert len(fake.calls) == 1
    assert fake.calls[0] == ["hello world", "another sentence"]


async def test_embed_texts_with_progress_empty_list_no_callback(monkeypatch):
    from src.services.embedder import embed_texts_with_progress

    _install_fake(monkeypatch)
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

    monkeypatch.setattr(
        emb, "embed_batch_with_retry", lambda _p, inputs, **_kw: fake_post(inputs)
    )

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
        # args = (provider, inputs) post-#311 -- the batch is args[1], not
        # args[0], because embed_batch_with_retry takes the provider first.
        offloaded.append(len(args[1]))  # batch size
        return await real_to_thread(func, *args, **kwargs)

    def fake_post(inputs):
        return [[0.0] * emb._embedding_dim() for _ in inputs]

    monkeypatch.setattr(
        emb, "embed_batch_with_retry", lambda _p, inputs, **_kw: fake_post(inputs)
    )
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

    monkeypatch.setattr(
        emb, "embed_batch_with_retry", lambda _p, inputs, **_kw: blocking_post(inputs)
    )

    task = asyncio.ensure_future(emb.embed_texts_with_progress([f"chunk-{i}" for i in range(5)]))
    await asyncio.sleep(0.05)  # let the first batch start and block
    task.cancel()
    release.set()  # let the blocked thread finish so the test can exit cleanly

    with pytest.raises(asyncio.CancelledError):
        await task

    # Only the in-flight batch was dispatched -- batches 2-5 never started.
    assert dispatched == ["chunk-0"]
