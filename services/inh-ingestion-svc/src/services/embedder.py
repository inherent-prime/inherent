"""Chunk embedder — thin wrapper over the shared ``inh_contracts.embedding`` provider (#311).

Before #311 this module WAS the HTTP client (TEI-only, no auth, no provider
choice) and inh-public-api-svc's ``embedder.py`` carried a second, divergent
copy (no retry at all on the query path). The actual HTTP/retry/batching
logic now lives ONCE in ``inh_contracts.embedding`` — see that package's
docstrings for the provider interface, the TEI/OpenAI-compatible wire
adapters, and the retry/batching helpers. This module's job is narrower:
resolve this service's env/Settings into a concrete provider, and expose the
same public functions (``embed_text``, ``embed_texts``) other modules already
import, so **no call site outside this file changes** (weaviate.py's
``embed_texts``/``embed_text`` imports are untouched — the #311 acceptance
bar).

Config:
    EMBEDDING_PROVIDER    — "tei" (default, non-negotiable — #311 item 8) or
                            "openai_compatible". Switching is an env change
                            only.
    EMBEDDING_SERVICE_URL — base URL of the embedding endpoint
                            (default: http://text-embeddings-inference:80)
    EMBEDDING_API_KEY     — sent as `Authorization: Bearer <key>`. TEI
                            accepts one but does not require it; NEVER
                            logged (#311 item 2).
    EMBEDDING_MODEL_ID    — model identity, also used for the Weaviate
                            collection model-identity guard
                            (see src/services/weaviate.py and
                            inh_contracts.embedding.identity).
    EMBEDDING_DIM         — vector dimension (default: 384, matches the
                            default model, BAAI/bge-small-en-v1.5)
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
import threading

import structlog
from inh_contracts.embedding import (
    EmbeddingIdentity,
    EmbeddingProvider,
    create_embedding_provider,
    embed_single,
    embed_texts_batched,
    redact_url,
)

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
_DEFAULT_PROVIDER = Settings.model_fields["embedding_provider"].default
_DEFAULT_MODEL_ID = Settings.model_fields["embedding_model_id"].default
_DEFAULT_TIMEOUT_S = DEFAULT_TIMEOUT_S
_DEFAULT_BATCH_SIZE = DEFAULT_BATCH_SIZE
_DEFAULT_MAX_CONCURRENCY = DEFAULT_MAX_CONCURRENCY
_DEFAULT_BATCH_MAX_RETRIES = DEFAULT_BATCH_MAX_RETRIES

_PROVIDER_LOCK = threading.Lock()
_PROVIDER: EmbeddingProvider | None = None


def _provider_name() -> str:
    raw = os.environ.get("EMBEDDING_PROVIDER", "").strip()
    return raw or _DEFAULT_PROVIDER


def _embedding_dim() -> int:
    raw = os.environ.get("EMBEDDING_DIM", "").strip()
    return int(raw) if raw else _DEFAULT_DIM


def _service_url() -> str:
    return os.environ.get("EMBEDDING_SERVICE_URL", _DEFAULT_URL).rstrip("/")


def _model_id() -> str:
    return os.environ.get("EMBEDDING_MODEL_ID", "").strip() or _DEFAULT_MODEL_ID


def _api_key() -> str | None:
    # Never logged (#311 item 2) -- only ever handed to the provider, which
    # sends it as a header, never as part of a logged URL/message.
    return os.environ.get("EMBEDDING_API_KEY", "").strip() or None


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


def _provider() -> EmbeddingProvider:
    """Return the process-wide embedding provider, constructing it on first use.

    Reads env directly (not ``Settings``) so this stays consistent with
    every other knob in this module — see the module docstring.
    """
    global _PROVIDER
    if _PROVIDER is None:
        with _PROVIDER_LOCK:
            if _PROVIDER is None:
                url = _service_url()
                _PROVIDER = create_embedding_provider(
                    provider=_provider_name(),
                    base_url=url,
                    model_id=_model_id(),
                    dimension=_embedding_dim(),
                    timeout=_timeout(),
                    api_key=_api_key(),
                )
                # redact_url: defense in depth in case an operator embeds a
                # key directly in EMBEDDING_SERVICE_URL instead of using
                # EMBEDDING_API_KEY (#311 item 2) -- the supported key path
                # never reaches this log line at all, it travels as a header.
                logger.info(
                    "embedder_client_initialized",
                    url=redact_url(url),
                    provider=_PROVIDER.name,
                    model_id=_PROVIDER.model_id,
                )
    return _PROVIDER


def get_active_embedding_identity() -> EmbeddingIdentity:
    """Return (model_id, dimension) of the currently configured provider.

    Used by the Weaviate collection model-identity guard (#311 item 4, see
    ``src/services/weaviate.py`` and ``inh_contracts.embedding.identity``).
    Cheap — the provider's identity is fixed at construction from Settings/
    env, no network call.
    """
    return _provider().identity()


def embed_text(text: str) -> list[float]:
    """Return a normalized embedding for the given text.

    Empty / whitespace-only input returns a zero vector — those chunks
    shouldn't surface in semantic search results anyway, and we avoid
    a network round-trip.
    """
    return embed_single(
        _provider(),
        text,
        max_retries=_batch_max_retries(),
    )


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Batched embedding with bounded parallel dispatch (#231 phase 1).

    Empty strings still get zero vectors (preserved per-position),
    and only the non-empty positions go over the wire. Batches run
    concurrently up to EMBEDDING_MAX_CONCURRENCY so a large document
    is ceil(n_batches / concurrency) round-trips instead of n_batches
    serial ones — the difference that made a 535-chunk PDF miss a 60s
    activity budget under TEI queue load (#228).
    """
    return embed_texts_batched(
        _provider(),
        texts,
        batch_size=_batch_size(),
        max_concurrency=_max_concurrency(),
        max_retries=_batch_max_retries(),
    )
