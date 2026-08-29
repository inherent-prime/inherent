"""Shared embedding-provider abstraction (#311).

Single source of truth for both services' embedder modules: the
``EmbeddingProvider`` interface, the TEI (default) and OpenAI-compatible wire
adapters, the factory that selects one from ``EMBEDDING_PROVIDER``, the
retry/batching helpers that give both the ingestion write path and the
public-api query path identical behavior, and the Weaviate collection
model-identity guard. See ``inh_contracts.embedding.identity`` for the
identity-guard policy write-up.
"""

from inh_contracts.embedding.batching import embed_single, embed_texts_batched
from inh_contracts.embedding.defaults import (
    BATCH_RETRY_SLEEP_BUDGET_S,
    DEFAULT_BATCH_MAX_RETRIES,
    DEFAULT_BATCH_SIZE,
    DEFAULT_EMBEDDING_PROVIDER,
    DEFAULT_MAX_CONCURRENCY,
    DEFAULT_TIMEOUT_S,
)
from inh_contracts.embedding.factory import create_embedding_provider
from inh_contracts.embedding.identity import (
    EmbeddingIdentityMismatchError,
    decode_identity,
    encode_identity,
    resolve_identity,
)
from inh_contracts.embedding.openai_provider import OpenAICompatibleProvider
from inh_contracts.embedding.provider import EmbeddingIdentity, EmbeddingProvider, redact_url
from inh_contracts.embedding.retry import embed_batch_with_retry, is_transient_embed_error
from inh_contracts.embedding.tei_provider import TEIProvider

__all__ = [
    "EmbeddingProvider",
    "EmbeddingIdentity",
    "EmbeddingIdentityMismatchError",
    "TEIProvider",
    "OpenAICompatibleProvider",
    "create_embedding_provider",
    "redact_url",
    "encode_identity",
    "decode_identity",
    "resolve_identity",
    "embed_single",
    "embed_texts_batched",
    "embed_batch_with_retry",
    "is_transient_embed_error",
    "DEFAULT_EMBEDDING_PROVIDER",
    "DEFAULT_BATCH_SIZE",
    "DEFAULT_MAX_CONCURRENCY",
    "DEFAULT_TIMEOUT_S",
    "DEFAULT_BATCH_MAX_RETRIES",
    "BATCH_RETRY_SLEEP_BUDGET_S",
]
