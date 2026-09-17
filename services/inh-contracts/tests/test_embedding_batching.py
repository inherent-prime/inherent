"""embed_single / embed_texts_batched (#311 item 1): behavior preserved from ingestion's embedder.py."""

from __future__ import annotations

import threading
import time

from inh_contracts.embedding.batching import embed_single, embed_texts_batched


class _FakeProvider:
    """Records every embed_batch call; returns a deterministic vector per text."""

    def __init__(self, dimension: int = 3) -> None:
        self.dimension = dimension
        self.calls: list[list[str]] = []
        self._lock = threading.Lock()
        self.max_concurrent = 0
        self._in_flight = 0

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        with self._lock:
            self._in_flight += 1
            self.max_concurrent = max(self.max_concurrent, self._in_flight)
            self.calls.append(list(texts))
        try:
            time.sleep(0.02)  # widen the overlap window so concurrency is observable
            return [[float(len(t))] * self.dimension for t in texts]
        finally:
            with self._lock:
                self._in_flight -= 1


# --- embed_single -----------------------------------------------------------------------


def test_embed_single_empty_text_returns_zero_vector_without_network_call() -> None:
    provider = _FakeProvider(dimension=4)
    assert embed_single(provider, "") == [0.0, 0.0, 0.0, 0.0]
    assert embed_single(provider, "   ") == [0.0, 0.0, 0.0, 0.0]
    assert provider.calls == []


def test_embed_single_non_empty_text_calls_provider() -> None:
    provider = _FakeProvider(dimension=2)
    vec = embed_single(provider, "hello")
    assert vec == [5.0, 5.0]
    assert provider.calls == [["hello"]]


# --- embed_texts_batched: zero-vector / positional preservation -------------------------


def test_empty_list_returns_empty_list() -> None:
    provider = _FakeProvider()
    assert embed_texts_batched(provider, []) == []
    assert provider.calls == []


def test_all_blank_texts_return_zero_vectors_without_network_call() -> None:
    provider = _FakeProvider(dimension=3)
    result = embed_texts_batched(provider, ["", "  ", "\t"])
    assert result == [[0.0, 0.0, 0.0]] * 3
    assert provider.calls == []


def test_zero_vectors_preserved_at_their_original_position() -> None:
    provider = _FakeProvider(dimension=1)
    texts = ["ab", "", "abcd", "   ", "a"]
    result = embed_texts_batched(provider, texts, max_concurrency=1)

    # Non-empty entries get a real (non-zero-vector) embedding; blanks stay zero,
    # each at its ORIGINAL index.
    assert result[1] == [0.0]
    assert result[3] == [0.0]
    assert result[0] == [2.0]
    assert result[2] == [4.0]
    assert result[4] == [1.0]
    # Only the 3 non-empty texts went over the wire.
    sent = [t for batch in provider.calls for t in batch]
    assert sorted(sent) == sorted(["ab", "abcd", "a"])


# --- batching ------------------------------------------------------------------------------


def test_texts_split_into_batch_size_chunks() -> None:
    provider = _FakeProvider()
    texts = [f"t{i}" for i in range(10)]
    embed_texts_batched(provider, texts, batch_size=3, max_concurrency=1)

    sizes = sorted(len(c) for c in provider.calls)
    assert sizes == [1, 3, 3, 3]


def test_batch_size_of_one_still_embeds_everything() -> None:
    provider = _FakeProvider()
    texts = ["a", "b", "c"]
    result = embed_texts_batched(provider, texts, batch_size=1, max_concurrency=1)
    assert len(result) == 3
    assert len(provider.calls) == 3


# --- concurrency ---------------------------------------------------------------------------


def test_concurrency_is_bounded_by_max_concurrency() -> None:
    provider = _FakeProvider()
    texts = [f"t{i}" for i in range(20)]
    embed_texts_batched(provider, texts, batch_size=2, max_concurrency=3)
    assert provider.max_concurrent <= 3
    # With 10 batches and a sleep per call, concurrency > 1 must actually
    # have been exercised, not just permitted.
    assert provider.max_concurrent > 1


def test_single_concurrency_runs_serially() -> None:
    provider = _FakeProvider()
    texts = [f"t{i}" for i in range(6)]
    embed_texts_batched(provider, texts, batch_size=2, max_concurrency=1)
    assert provider.max_concurrent == 1
