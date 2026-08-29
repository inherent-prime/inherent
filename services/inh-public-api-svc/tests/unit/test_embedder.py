"""Unit tests for the query embedder (#311).

Mirrors inh-ingestion-svc's ``tests/test_embedder.py``: the actual HTTP/
retry/batching logic lives once in ``inh_contracts.embedding`` (see that
package's own tests for wire-format-fixture and retry-wall-clock coverage).
These tests exercise this module's own job: env plumbing (dimension,
provider selection, retry count), the ``embed_query`` behavior contract
(zero-vector shortcut, lru_cache, retry pass-through -- this used to have
ZERO retry, the exact ingestion/query divergence #311 closes), and that no
embedding API key ever reaches a log line.
"""

from __future__ import annotations

import httpx
import pytest
from inh_contracts.embedding import EmbeddingProvider


@pytest.fixture(autouse=True)
def _reset_embedder_provider(monkeypatch):
    """Reset the module-level provider AND the embed_query lru_cache between
    tests so each test can install its own fake without state leaking."""
    from src.services import embedder

    monkeypatch.setattr(embedder, "_PROVIDER", None, raising=False)
    embedder.embed_query.cache_clear()
    yield
    monkeypatch.setattr(embedder, "_PROVIDER", None, raising=False)
    embedder.embed_query.cache_clear()


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
            seed = sum((pos + 1) * ord(char) for pos, char in enumerate(text)) + 1
            vecs.append([float(seed) / 1000.0] * self.dim)
        return vecs


def _install_fake(monkeypatch, dim: int = 384) -> _FakeProvider:
    from src.services import embedder

    fake = _FakeProvider(dim=dim)
    monkeypatch.setattr(embedder, "_PROVIDER", fake, raising=False)
    return fake


# --- basic behavior contracts ------------------------------------------------------------------


def test_embed_query_returns_correct_dim(monkeypatch):
    from src.services.embedder import embed_query

    _install_fake(monkeypatch)
    vec = embed_query("how do I authenticate?")
    assert len(vec) == 384
    assert isinstance(vec, tuple)


def test_embed_query_empty_returns_zero_vector_no_http(monkeypatch):
    from src.services.embedder import embed_query

    fake = _install_fake(monkeypatch)
    vec = embed_query("")
    assert vec == tuple(0.0 for _ in range(384))
    assert fake.calls == [], "empty input must not hit the network"


def test_embed_query_whitespace_only_returns_zero_vector(monkeypatch):
    from src.services.embedder import embed_query

    fake = _install_fake(monkeypatch)
    vec = embed_query("   \t\n ")
    assert vec == tuple(0.0 for _ in range(384))
    assert fake.calls == []


def test_embed_query_is_lru_cached(monkeypatch):
    """Same query text must not re-hit the network."""
    from src.services.embedder import embed_query

    fake = _install_fake(monkeypatch)
    a = embed_query("rotate an API key")
    b = embed_query("rotate an API key")
    assert a == b
    assert len(fake.calls) == 1


def test_embed_dim_overridable_via_env(monkeypatch):
    monkeypatch.setenv("EMBEDDING_DIM", "768")
    from src.services.embedder import embed_query

    _install_fake(monkeypatch, dim=768)
    vec = embed_query("test")
    assert len(vec) == 768


# --- retry parity with the ingestion write path (#311 item 5) -----------------------------------


def test_embed_query_retries_transient_failure(monkeypatch):
    """Before #311, embed_query had ZERO retry -- a single TEI blip failed
    the whole search request. Now it retries transient errors just like the
    ingestion write path."""
    from src.services.embedder import embed_query

    monkeypatch.setenv("EMBEDDING_BATCH_MAX_RETRIES", "3")
    fake = _install_fake(monkeypatch)
    calls = {"n": 0}
    real_embed_batch = fake.embed_batch

    def flaky(texts):
        calls["n"] += 1
        if calls["n"] < 2:
            raise httpx.ReadTimeout("queue saturated", request=httpx.Request("POST", "/embed"))
        return real_embed_batch(texts)

    monkeypatch.setattr(fake, "embed_batch", flaky)
    vec = embed_query("only one")
    assert len(vec) == 384
    assert calls["n"] == 2


def test_embed_query_does_not_retry_client_4xx(monkeypatch):
    """Deterministic 4xx must fail fast."""
    from src.services.embedder import embed_query

    monkeypatch.setenv("EMBEDDING_BATCH_MAX_RETRIES", "5")
    fake = _install_fake(monkeypatch)
    calls = {"n": 0}

    def bad_request(texts):
        calls["n"] += 1
        req = httpx.Request("POST", "/embed")
        resp = httpx.Response(400, request=req)
        raise httpx.HTTPStatusError("bad request", request=req, response=resp)

    monkeypatch.setattr(fake, "embed_batch", bad_request)
    with pytest.raises(httpx.HTTPStatusError):
        embed_query("x")
    assert calls["n"] == 1


def test_embed_query_raises_after_exhausting_retries(monkeypatch):
    from src.services.embedder import embed_query

    monkeypatch.setenv("EMBEDDING_BATCH_MAX_RETRIES", "1")
    fake = _install_fake(monkeypatch)

    def always_fail(texts):
        raise httpx.ConnectError("tei down")

    monkeypatch.setattr(fake, "embed_batch", always_fail)
    with pytest.raises(httpx.ConnectError, match="tei down"):
        embed_query("x")


# --- wall-clock budget is honest (PR #314 review finding 2) --------------------------------------


def test_query_defaults_are_smaller_than_ingestion_batch_defaults():
    """The pre-fix bug: this module copied the BATCH defaults (30s timeout,
    3 retries) for "retry parity", producing a ~91.5s worst case against the
    #311 issue's 15s interactive-search ceiling. The query path needs its
    own, smaller numbers -- not the ingestion write path's."""
    from inh_contracts.embedding.defaults import DEFAULT_BATCH_MAX_RETRIES, DEFAULT_TIMEOUT_S

    from src.services import embedder

    assert embedder._DEFAULT_TIMEOUT_S < DEFAULT_TIMEOUT_S
    assert embedder._DEFAULT_BATCH_MAX_RETRIES <= DEFAULT_BATCH_MAX_RETRIES


def test_query_worst_case_wall_clock_fits_under_15s_consumer_ceiling():
    """attempts * timeout + sleep_budget, using this module's OWN resolved
    defaults -- not just the shared inh_contracts constants directly -- so a
    future accidental override in this file is caught too."""
    from inh_contracts.embedding.retry import max_wall_clock_s

    from src.services import embedder

    worst_case = max_wall_clock_s(
        attempts=embedder._batch_max_retries(),
        timeout_s=embedder._timeout(),
        retry_budget_s=embedder._retry_budget_s(),
    )
    assert worst_case < 15.0


def test_retry_budget_s_overridable_via_env(monkeypatch):
    from src.services import embedder

    assert embedder._retry_budget_s() == embedder._DEFAULT_RETRY_BUDGET_S
    monkeypatch.setenv("EMBEDDING_QUERY_RETRY_BUDGET_S", "0.5")
    assert embedder._retry_budget_s() == 0.5


def test_embed_query_retry_budget_env_is_actually_wired_through(monkeypatch):
    """Not just readable -- embed_query must actually pass it to the shared
    retry helper, otherwise the env var is a lie."""
    from src.services.embedder import embed_query

    monkeypatch.setenv("EMBEDDING_BATCH_MAX_RETRIES", "1000")
    monkeypatch.setenv("EMBEDDING_QUERY_RETRY_BUDGET_S", "0")
    fake = _install_fake(monkeypatch)
    calls = {"n": 0}

    def always_transient(texts):
        calls["n"] += 1
        raise httpx.ConnectError("tei down")

    monkeypatch.setattr(fake, "embed_batch", always_transient)
    with pytest.raises(httpx.ConnectError):
        embed_query("x")
    # A zero sleep budget must stop retrying after the first failure,
    # regardless of the (huge) max_retries -- proves retry_budget_s reached
    # embed_batch_with_retry rather than silently falling back to the
    # shared package's own 10s default.
    assert calls["n"] == 1


# --- provider selection --------------------------------------------------------------------------


def test_default_provider_is_tei(monkeypatch):
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


# --- API key never leaks into logs (#311 item 2) -------------------------------------------------
#
# See inh-ingestion-svc/tests/test_embedder.py for why this replaces the
# `logger` object with a spy instead of using pytest's `caplog`.


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
    from src.services import embedder

    spy = _LogSpy()
    monkeypatch.setattr(embedder, "logger", spy, raising=True)
    secret = "sk-super-secret-embedding-key-12345"  # noqa: S105 -- test fixture value
    monkeypatch.setenv("EMBEDDING_API_KEY", secret)
    monkeypatch.setenv("EMBEDDING_SERVICE_URL", "http://tei.local:80")

    embedder._provider()

    assert spy.records, "expected the init log call to have fired"
    assert secret not in spy.rendered()


def test_embedding_api_key_embedded_in_url_is_redacted_from_logs(monkeypatch):
    from src.services import embedder

    spy = _LogSpy()
    monkeypatch.setattr(embedder, "logger", spy, raising=True)
    secret = "sk-in-the-url-secret"  # noqa: S105 -- test fixture value
    monkeypatch.setenv("EMBEDDING_SERVICE_URL", f"http://{secret}@tei.local:80")

    embedder._provider()

    assert spy.records, "expected the init log call to have fired"
    assert secret not in spy.rendered()
