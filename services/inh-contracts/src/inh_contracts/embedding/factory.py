"""Embedding provider factory (#311) -- mirrors ``mq/__init__.py::create_mq_service``.

Selects the concrete ``EmbeddingProvider`` from a ``provider`` string (each
service's ``EMBEDDING_PROVIDER`` setting/env var, default ``"tei"`` --
non-negotiable, see ``defaults.DEFAULT_EMBEDDING_PROVIDER``). This is the ONE
place that maps a provider name to a class, so switching providers is a
config change, never a call-site change.
"""

from __future__ import annotations

import httpx

from inh_contracts.embedding.defaults import DEFAULT_EMBEDDING_PROVIDER, DEFAULT_TIMEOUT_S
from inh_contracts.embedding.openai_provider import OpenAICompatibleProvider
from inh_contracts.embedding.provider import EmbeddingProvider
from inh_contracts.embedding.tei_provider import TEIProvider

# Accepted spellings per provider -- kept liberal (hyphen/underscore) since
# this is an operator-typed env var, not an internal enum.
_TEI_ALIASES = {"tei", ""}
_OPENAI_ALIASES = {"openai_compatible", "openai-compatible", "openai"}


def create_embedding_provider(
    *,
    provider: str = DEFAULT_EMBEDDING_PROVIDER,
    base_url: str,
    model_id: str,
    dimension: int,
    timeout: float = DEFAULT_TIMEOUT_S,
    api_key: str | None = None,
    transport: httpx.BaseTransport | None = None,
) -> EmbeddingProvider:
    """Construct the ``EmbeddingProvider`` named by ``provider``.

    Args:
        provider: ``"tei"`` (default) or ``"openai_compatible"``. Unknown
            values raise -- a typo in ``EMBEDDING_PROVIDER`` must fail loudly
            at startup, not silently fall back to TEI.
        base_url: Base URL of the embedding endpoint.
        model_id: Model identity used for the OpenAI-compatible request body
            AND (for both providers) the persisted collection-identity guard
            (#311 item 4) -- see ``inh_contracts.embedding.identity``.
        dimension: Expected vector width -- also feeds the identity guard and
            the zero-vector shortcut for empty input.
        timeout: Per-request httpx timeout.
        api_key: Sent as ``Authorization: Bearer <key>`` when set. TEI
            accepts one but does not require it (zero-config local dev);
            OpenAI-compatible backends generally require one.
        transport: Test-only httpx transport override (e.g. MockTransport).
    """
    key = (provider or DEFAULT_EMBEDDING_PROVIDER).strip().lower()
    if key in _TEI_ALIASES:
        return TEIProvider(
            base_url=base_url,
            model_id=model_id,
            dimension=dimension,
            timeout=timeout,
            api_key=api_key,
            transport=transport,
        )
    if key in _OPENAI_ALIASES:
        return OpenAICompatibleProvider(
            base_url=base_url,
            model_id=model_id,
            dimension=dimension,
            timeout=timeout,
            api_key=api_key,
            transport=transport,
        )
    raise ValueError(
        f"Unknown EMBEDDING_PROVIDER {provider!r}; expected 'tei' (default) or "
        "'openai_compatible'"
    )
