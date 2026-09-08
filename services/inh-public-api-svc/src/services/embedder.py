"""Query embedder — thin wrapper over the shared ``inh_contracts.embedding`` provider (#311).

Before #311 this module was a second, DIVERGENT copy of inh-ingestion-svc's
TEI HTTP client — no auth, no retry at all (the exact gap #311 closes: a
transient TEI hiccup failed a search request instantly while the ingestion
write path already retried). The actual HTTP/retry/batching logic now lives
ONCE in ``inh_contracts.embedding``; this module resolves this service's
env/Settings into a concrete provider and keeps the same public
``embed_query`` function search.py already imports — no call site outside
this file changes.

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
                            (see src/services/search.py and
                            inh_contracts.embedding.identity).
    EMBEDDING_DIM         — vector dimension (default: 384, matches the
                            default model, BAAI/bge-small-en-v1.5)
    EMBEDDING_TIMEOUT_S   — per-request timeout in seconds (default: 5 —
                            see the wall-clock note below; NOT the same
                            default as inh-ingestion-svc's batch path)
    EMBEDDING_BATCH_MAX_RETRIES  — retry attempts for the query embed
                            (default: 2)
    EMBEDDING_QUERY_RETRY_BUDGET_S — cumulative retry SLEEP budget in
                            seconds (default: 2) — see the wall-clock note

Wall clock (PR #314 review finding 2): a query embed sits inside a
synchronous, user-facing search request with a real caller-side ceiling
(the #311 issue's own incident cites a 15s consumer timeout on interactive
chat search) — unlike inh-ingestion-svc's batch write path, which embeds in
a background Temporal activity with its own generously-sized StartToClose
budget (`weaviate_store_budget.py`). The two paths therefore do NOT share
one "the retry defaults" number: this module's defaults are deliberately
SMALLER than the batch defaults in `inh_contracts.embedding.defaults`, so
that `attempts * timeout + sleep_budget` (see that package's
`max_wall_clock_s`) fits under the 15s ceiling with margin to spare —
2 * 5 + 2 = 12s — rather than the ~91.5s worst case the pre-fix defaults
(3 attempts * 30s timeout) produced. Set the env vars above explicitly if a
deployment's actual consumer ceiling differs.
"""

from __future__ import annotations

import os
import threading
from functools import lru_cache

from inh_contracts.embedding import (
    DEFAULT_QUERY_MAX_RETRIES,
    DEFAULT_QUERY_TIMEOUT_S,
    QUERY_RETRY_SLEEP_BUDGET_S,
    EmbeddingIdentity,
    EmbeddingProvider,
    create_embedding_provider,
    embed_single,
    redact_url,
)

from src.config.settings import Settings
from src.utils import get_logger

logger = get_logger(__name__)


# Sourced from Settings' own field defaults (not re-hardcoded here) so the
# embedder's fallback can't drift from src/config/settings.py.
_DEFAULT_URL = Settings.model_fields["embedding_service_url"].default
_DEFAULT_DIM = Settings.model_fields["embedding_dim"].default
_DEFAULT_PROVIDER = Settings.model_fields["embedding_provider"].default
_DEFAULT_MODEL_ID = Settings.model_fields["embedding_model_id"].default
# #311 PR #314 review finding 2: these were 30.0 / 3 -- copied from the
# ingestion BATCH defaults for "retry parity" without noticing that parity
# in RETRY BEHAVIOR (backoff + jitter on transient failures) does not mean
# parity in the underlying NUMBERS is safe. attempts * timeout + sleep
# blew past the #311 issue's own 15s interactive-search ceiling (~91.5s
# worst case). Sourced from inh_contracts.embedding.defaults' query-path
# constants (see that module's docstring for the full worst-case math) so
# this module doesn't carry its own independently-typed copy.
_DEFAULT_TIMEOUT_S = DEFAULT_QUERY_TIMEOUT_S
_DEFAULT_BATCH_MAX_RETRIES = DEFAULT_QUERY_MAX_RETRIES
_DEFAULT_RETRY_BUDGET_S = QUERY_RETRY_SLEEP_BUDGET_S

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


def _batch_max_retries() -> int:
    raw = os.environ.get("EMBEDDING_BATCH_MAX_RETRIES", "").strip()
    return max(1, int(raw)) if raw else _DEFAULT_BATCH_MAX_RETRIES


def _retry_budget_s() -> float:
    """Cumulative retry SLEEP budget (PR #314 review finding 2) -- previously
    unconfigurable here, silently defaulting to the batch path's 10s budget
    via inh_contracts.embedding.batching's own default. Explicit now so the
    query path's honest worst case (attempts * timeout + this) is a number
    this module actually controls end to end."""
    raw = os.environ.get("EMBEDDING_QUERY_RETRY_BUDGET_S", "").strip()
    return float(raw) if raw else _DEFAULT_RETRY_BUDGET_S


def _provider() -> EmbeddingProvider:
    """Return the process-wide embedding provider, constructing it on first use."""
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
    ``src/services/search.py`` and ``inh_contracts.embedding.identity``).
    Cheap — the provider's identity is fixed at construction from Settings/
    env, no network call.
    """
    return _provider().identity()


@lru_cache(maxsize=1024)
def embed_query(text: str) -> tuple[float, ...]:
    """Return a tuple of floats (hashable for LRU caching).

    Empty / whitespace-only input returns a zero vector without a network
    call. Retries transient provider failures with the same backoff+jitter
    MECHANISM as the ingestion write path (#311 item 5) -- this used to have
    zero retry, so a single TEI queue blip failed a search request outright
    -- but tuned to this path's own, much smaller wall-clock budget (PR #314
    review finding 2; see the module docstring's "Wall clock" note): the
    ingestion defaults alone would let retries here blow past the caller's
    real timeout ceiling.
    """
    vec = embed_single(
        _provider(), text, max_retries=_batch_max_retries(), retry_budget_s=_retry_budget_s()
    )
    return tuple(vec)
