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
"""

from __future__ import annotations

import os
import threading

import httpx
import structlog

from src.config.settings import Settings

logger = structlog.get_logger(__name__)


# Sourced from Settings' own field defaults (not re-hardcoded here) so the
# embedder's fallback can't drift from src/config/settings.py.
_DEFAULT_URL = Settings.model_fields["embedding_service_url"].default
_DEFAULT_DIM = Settings.model_fields["embedding_dim"].default
_DEFAULT_TIMEOUT_S = 30.0
_DEFAULT_BATCH_SIZE = 32

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


def embed_text(text: str) -> list[float]:
    """Return a normalized embedding for the given text.

    Empty / whitespace-only input returns a zero vector — those chunks
    shouldn't surface in semantic search results anyway, and we avoid
    a network round-trip.
    """
    dim = _embedding_dim()
    if not text or not text.strip():
        return [0.0] * dim
    vecs = _post_embed([text])
    return vecs[0]


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Batched embedding — one HTTP call regardless of batch size.

    Empty strings still get zero vectors (preserved per-position),
    and only the non-empty positions go over the wire.
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
    vecs: list[list[float]] = []
    for offset in range(0, len(keep_texts), batch):
        vecs.extend(_post_embed(keep_texts[offset : offset + batch]))

    out: list[list[float]] = [[0.0] * dim for _ in texts]
    for j, i in enumerate(keep_idx):
        out[i] = vecs[j]
    return out
