"""Zero-vector shortcuts, TEI-sized batching, and bounded concurrency (#311).

Ported verbatim (behaviorally) from ``inh-ingestion-svc``'s ``embedder.py``,
which is the side that originally had this logic; ``embed_query`` on the
public-api side gets it for the first time via this shared module.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed

from inh_contracts.embedding.defaults import (
    BATCH_RETRY_SLEEP_BUDGET_S,
    DEFAULT_BATCH_MAX_RETRIES,
    DEFAULT_BATCH_SIZE,
    DEFAULT_MAX_CONCURRENCY,
)
from inh_contracts.embedding.provider import EmbeddingProvider
from inh_contracts.embedding.retry import embed_batch_with_retry


def embed_single(
    provider: EmbeddingProvider,
    text: str,
    *,
    max_retries: int = DEFAULT_BATCH_MAX_RETRIES,
    retry_budget_s: float = BATCH_RETRY_SLEEP_BUDGET_S,
) -> list[float]:
    """Return a normalized embedding for one text.

    Empty / whitespace-only input returns a zero vector -- those chunks/
    queries shouldn't surface in semantic search results anyway, and we
    avoid a network round-trip.
    """
    if not text or not text.strip():
        return [0.0] * provider.dimension
    vecs = embed_batch_with_retry(
        provider, [text], max_retries=max_retries, retry_budget_s=retry_budget_s
    )
    return vecs[0]


def embed_texts_batched(
    provider: EmbeddingProvider,
    texts: list[str],
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
    max_retries: int = DEFAULT_BATCH_MAX_RETRIES,
    retry_budget_s: float = BATCH_RETRY_SLEEP_BUDGET_S,
) -> list[list[float]]:
    """Batched embedding with bounded parallel dispatch (#231 phase 1).

    Empty strings still get zero vectors (preserved per-position), and only
    the non-empty positions go over the wire. Batches run concurrently up to
    ``max_concurrency`` so a large document is
    ``ceil(n_batches / concurrency)`` round-trips instead of ``n_batches``
    serial ones.
    """
    dim = provider.dimension
    if not texts:
        return []
    keep_idx = [i for i, t in enumerate(texts) if t and t.strip()]
    if not keep_idx:
        return [[0.0] * dim for _ in texts]

    # Chunk into batches under the provider's max batch size to avoid a
    # payload-too-large response. A 535-chunk PDF was failing with one giant
    # POST against TEI; batching keeps every request comfortably sized.
    batch = max(1, batch_size)
    keep_texts = [texts[i] for i in keep_idx]
    batches: list[tuple[int, list[str]]] = []
    for offset in range(0, len(keep_texts), batch):
        batches.append((offset, keep_texts[offset : offset + batch]))

    # index-in-keep_texts -> vector
    results: dict[int, list[float]] = {}
    concurrency = min(max(1, max_concurrency), len(batches))

    def _run_batch(item: tuple[int, list[str]]) -> tuple[int, list[list[float]]]:
        offset, inputs = item
        return offset, embed_batch_with_retry(
            provider, inputs, max_retries=max_retries, retry_budget_s=retry_budget_s
        )

    if concurrency == 1:
        for item in batches:
            offset, vecs = _run_batch(item)
            for j, vec in enumerate(vecs):
                results[offset + j] = vec
    else:
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = [pool.submit(_run_batch, item) for item in batches]
            for fut in as_completed(futures):
                offset, vecs = fut.result()
                for j, vec in enumerate(vecs):
                    results[offset + j] = vec

    out: list[list[float]] = [[0.0] * dim for _ in texts]
    for j, i in enumerate(keep_idx):
        out[i] = results[j]
    return out
