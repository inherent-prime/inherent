"""Query embedder — HTTP client for the text-embeddings-inference (TEI) sidecar.

The model itself runs in a separate container (HuggingFace TEI), so this
service stays slim. The same sidecar is used by inh-ingestion-svc for chunk
embeddings, guaranteeing that query vectors are directly comparable to
stored chunk vectors.

To upgrade the embedding model, change MODEL_ID on the TEI sidecar and
restart it; no code change here.

Config:
    EMBEDDING_SERVICE_URL — base URL of the TEI sidecar
                            (default: http://text-embeddings-inference:80)
    EMBEDDING_DIM         — vector dimension (default: 384, matches MiniLM-L6-v2)
    EMBEDDING_TIMEOUT_S   — per-request timeout in seconds (default: 30)
"""

from __future__ import annotations

import os
import threading
from functools import lru_cache

import httpx

from src.config.settings import Settings
from src.utils import get_logger

logger = get_logger(__name__)


# Sourced from Settings' own field defaults (not re-hardcoded here) so the
# embedder's fallback can't drift from src/config/settings.py.
_DEFAULT_URL = Settings.model_fields["embedding_service_url"].default
_DEFAULT_DIM = Settings.model_fields["embedding_dim"].default
_DEFAULT_TIMEOUT_S = 30.0

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


@lru_cache(maxsize=1024)
def embed_query(text: str) -> tuple[float, ...]:
    """Return a tuple of floats (hashable for LRU caching).

    Empty / whitespace-only input returns a zero vector without a network call.
    """
    dim = _embedding_dim()
    if not text or not text.strip():
        return tuple(0.0 for _ in range(dim))
    resp = _client().post("/embed", json={"inputs": [text]})
    resp.raise_for_status()
    vec = resp.json()[0]
    return tuple(float(x) for x in vec)
