"""Chunk embedder — HTTP client for the text-embeddings-inference (TEI) sidecar.

The model itself runs in a separate container (HuggingFace TEI), so this
service stays slim — no torch, no sentence-transformers, no ~2GB CUDA stack
in our image. To upgrade the embedding model, change MODEL_ID on the TEI
sidecar and restart it; no code change here.

Both inh-ingestion-svc (chunks) and inh-public-api-svc (queries) call the
same sidecar so the vectors are guaranteed comparable.

Config:
    EMBEDDING_SERVICE_URL — base URL of the TEI sidecar
                            (default: http://text-embeddings-inference:80)
    EMBEDDING_DIM         — vector dimension (default: 384, matches MiniLM-L6-v2)
    EMBEDDING_TIMEOUT_S   — per-request timeout in seconds (default: 30)
    EMBEDDING_BATCH_SIZE  — chunks per HTTP call (default: 32). TEI's default
                            max-client-batch-size is small (~32); larger batches
                            return HTTP 413 Payload Too Large. We chunk
                            internally and concatenate, so callers can pass any
                            number of texts.
    EMBEDDING_MAX_CONCURRENCY — max in-flight batch POSTs per embed_texts call
                            (default: 2). Serial dispatch made a 535-chunk PDF
                            17 round-trips end-to-end (#228 / #231 phase 1).
                            Keep this low under bulk upload: the product of
                            this and TEMPORAL_MAX_CONCURRENT_ACTIVITIES is the
                            TEI in-flight cap (default 2×10=20, not 4×10=40).
    EMBEDDING_BATCH_MAX_RETRIES — retries per batch on *transient* failure
                            (default: 3), with exponential backoff + jitter
                            so a single queue spike does not burn a whole
                            Temporal activity attempt (#229). 4xx (except 429)
                            fail fast. Worst-case batch wall clock is baked
                            into weaviate_store_budget via embedding_defaults.
"""

from __future__ import annotations

import os
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import httpx
import structlog

from src.config.settings import Settings
from src.services.embedding_defaults import (
    DEFAULT_BATCH_MAX_RETRIES,
    DEFAULT_BATCH_SIZE,
    DEFAULT_MAX_CONCURRENCY,
    DEFAULT_TIMEOUT_S,
)

logger = structlog.get_logger(__name__)


# Sourced from Settings / embedding_defaults so fallbacks cannot drift from
# weaviate_store_budget or settings.py independently.
_DEFAULT_URL = Settings.model_fields["embedding_service_url"].default
_DEFAULT_DIM = Settings.model_fields["embedding_dim"].default
_DEFAULT_TIMEOUT_S = DEFAULT_TIMEOUT_S
_DEFAULT_BATCH_SIZE = DEFAULT_BATCH_SIZE
_DEFAULT_MAX_CONCURRENCY = DEFAULT_MAX_CONCURRENCY
_DEFAULT_BATCH_MAX_RETRIES = DEFAULT_BATCH_MAX_RETRIES

_CLIENT_LOCK = threading.Lock()
_CLIENT: httpx.Client | None = None


def _embedding_dim() -> int:
    raw = os.environ.get("EMBEDDING_DIM", "").strip()
    return int(raw) if raw else _DEFAULT_DIM


def _service_url() -> str:
    return os.environ.get("EMBEDDING_SERVICE_URL", _DEFAULT_URL).rstrip("/")


def _timeout() -> float:
    raw = os.environ.get("EMBEDDING_TIMEOUT_S", "").strip()
    return float(raw) if raw else _DEFAULT_TIMEOUT_S


def _batch_size() -> int:
    raw = os.environ.get("EMBEDDING_BATCH_SIZE", "").strip()
    return max(1, int(raw)) if raw else _DEFAULT_BATCH_SIZE


def _max_concurrency() -> int:
    raw = os.environ.get("EMBEDDING_MAX_CONCURRENCY", "").strip()
    return max(1, int(raw)) if raw else _DEFAULT_MAX_CONCURRENCY


def _batch_max_retries() -> int:
    raw = os.environ.get("EMBEDDING_BATCH_MAX_RETRIES", "").strip()
    return max(1, int(raw)) if raw else _DEFAULT_BATCH_MAX_RETRIES


def _client() -> httpx.Client:
    global _CLIENT
    if _CLIENT is None:
        with _CLIENT_LOCK:
            if _CLIENT is None:
                _CLIENT = httpx.Client(
                    base_url=_service_url(),
                    timeout=_timeout(),
                )
                logger.info("embedder_client_initialized", url=_service_url())
    return _CLIENT


def _post_embed(inputs: list[str]) -> list[list[float]]:
    # truncate=true tells TEI to silently truncate inputs longer than the model's
    # max_input_length (256 tokens for all-MiniLM-L6-v2) instead of returning 413.
    # Without this, any chunk over ~190 words crashes the entire batch with
    # "Input validation error: inputs must have less than 256 tokens".
    resp = _client().post("/embed", json={"inputs": inputs, "truncate": True})
    resp.raise_for_status()
    data = resp.json()
    # TEI returns a list of vectors (already normalized for cosine-similarity models)
    return [[float(x) for x in vec] for vec in data]


def _is_transient_embed_error(exc: BaseException) -> bool:
    """True only for failures that may succeed on a short retry.

    Deterministic client/config errors (4xx except 429) fail immediately so
    we do not pad latency or mask bad input/auth under the batch retry loop.
    """
    if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return code == 429 or code >= 500
    return False


def _post_embed_with_retry(inputs: list[str]) -> list[list[float]]:
    """POST one batch; retry *transient* failures with exponential backoff + jitter.

    Activity-level Temporal retries re-embed the *whole document* (#229). A
    cheap per-batch retry absorbs a single TEI queue spike so the activity
    attempt can still finish inside its budget. Non-transient errors raise
    on the first failure.
    """
    attempts = _batch_max_retries()
    last_exc: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            return _post_embed(inputs)
        except Exception as exc:
            last_exc = exc
            if not _is_transient_embed_error(exc) or attempt >= attempts:
                break
            # Base 0.5s * 2^(attempt-1), plus 0–50% jitter; cap at 8s.
            # The only thing this randomness protects is retry *timing*: it
            # de-synchronises concurrent batches so they do not re-hit TEI in
            # lockstep. Nothing here is a secret, a token or a key, so a
            # predictable sequence costs an attacker nothing and a CSPRNG buys
            # nothing -- hence the B311 waiver on the delay line below.
            base = min(8.0, 0.5 * (2 ** (attempt - 1)))
            delay = base * (0.5 + random.random() * 0.5)  # nosec B311 -- see above
            logger.warning(
                "embed_batch_retry",
                attempt=attempt,
                max_attempts=attempts,
                delay_s=round(delay, 3),
                error=str(exc),
            )
            time.sleep(delay)
    assert last_exc is not None
    raise last_exc


def embed_text(text: str) -> list[float]:
    """Return a normalized embedding for the given text.

    Empty / whitespace-only input returns a zero vector — those chunks
    shouldn't surface in semantic search results anyway, and we avoid
    a network round-trip.
    """
    dim = _embedding_dim()
    if not text or not text.strip():
        return [0.0] * dim
    vecs = _post_embed_with_retry([text])
    return vecs[0]


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Batched embedding with bounded parallel dispatch (#231 phase 1).

    Empty strings still get zero vectors (preserved per-position),
    and only the non-empty positions go over the wire. Batches run
    concurrently up to EMBEDDING_MAX_CONCURRENCY so a large document
    is ceil(n_batches / concurrency) round-trips instead of n_batches
    serial ones — the difference that made a 535-chunk PDF miss a 60s
    activity budget under TEI queue load (#228).
    """
    dim = _embedding_dim()
    if not texts:
        return []
    keep_idx = [i for i, t in enumerate(texts) if t and t.strip()]
    if not keep_idx:
        return [[0.0] * dim for _ in texts]

    # Chunk into batches under TEI's max-client-batch-size to avoid HTTP 413.
    # A 535-chunk PDF was failing with one giant POST; batching of 32 gets us
    # comfortably under any reasonable TEI default.
    batch = _batch_size()
    keep_texts = [texts[i] for i in keep_idx]
    batches: list[tuple[int, list[str]]] = []
    for offset in range(0, len(keep_texts), batch):
        batches.append((offset, keep_texts[offset : offset + batch]))

    # index-in-keep_texts -> vector
    results: dict[int, list[float]] = {}
    concurrency = min(_max_concurrency(), len(batches))

    def _run_batch(item: tuple[int, list[str]]) -> tuple[int, list[list[float]]]:
        offset, inputs = item
        return offset, _post_embed_with_retry(inputs)

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
