"""``EmbeddingProvider`` -- the shared abstraction over embedding backends (#311).

Both inh-ingestion-svc (chunk embeddings) and inh-public-api-svc (query
embeddings) used to each carry their own copy of the TEI HTTP client, with
divergent behavior (only ingestion had retry). This module is the ONE
interface both services program against; ``factory.py`` picks the concrete
implementation from ``EMBEDDING_PROVIDER`` so switching providers is an env
change only -- no call site in either service's business logic changes.

Mirrors the shape of ``inh-ingestion-svc``'s ``BaseMQService`` / ``mq/base.py``
+ ``create_mq_service()`` pattern: an ABC here, concrete backends in sibling
modules, a factory that selects on a setting.
"""

from __future__ import annotations

import abc
import re
import threading
from dataclasses import dataclass

import httpx

# Matches a userinfo segment (``user:pass@`` or ``token@``) directly embedded
# in a URL, e.g. ``https://sk-abc123@api.example.com``. An operator who puts
# an API key in EMBEDDING_SERVICE_URL instead of EMBEDDING_API_KEY must not
# have it show up verbatim the first time we log that URL (#311 item 2).
_URL_CREDENTIALS_RE = re.compile(r"://([^/@\s]+)@")


def redact_url(url: str) -> str:
    """Strip any embedded ``user:pass@`` / ``token@`` credentials from a URL.

    Used everywhere an embedding-provider URL (or an httpx exception message
    that echoes one back) is about to be logged, so a key embedded in the URL
    itself can never leak into logs even though ``EMBEDDING_API_KEY`` -- the
    supported, header-based path -- never does.
    """
    return _URL_CREDENTIALS_RE.sub("://***@", url)


@dataclass(frozen=True)
class EmbeddingIdentity:
    """The (model_id, dimension) pair that pins a provider's vector space.

    Weaviate collections are created with ``Configure.Vectorizer.none()`` and
    never declare a dimension -- Weaviate just pins vector width at first
    insert. Querying with model A against a collection built with model B
    returns plausible-looking noise with no error anywhere (#311 item 4).
    This tuple is what gets persisted as collection metadata and compared on
    every write/query -- see ``inh_contracts.embedding.identity``.
    """

    model_id: str
    dimension: int


class EmbeddingProvider(abc.ABC):
    """Abstract embedding backend: identity + a single batch HTTP call.

    Deliberately minimal. Everything callers actually reason about --
    zero-vectors for blank input, positional preservation, TEI-sized
    batching, bounded concurrency, retry-with-backoff, the total retry wall
    clock -- lives ONCE in ``batching.py``/``retry.py`` and is shared by every
    provider. A concrete provider only knows how to (a) name itself, (b)
    report the identity of the model it talks to, and (c) POST one batch of
    *non-empty* texts and return vectors in REQUEST order.
    """

    @property
    @abc.abstractmethod
    def name(self) -> str:
        """Short provider identifier for logs/metrics (e.g. ``"tei"``)."""

    @property
    @abc.abstractmethod
    def model_id(self) -> str:
        """The embedding model this provider is configured to talk to."""

    @property
    @abc.abstractmethod
    def dimension(self) -> int:
        """The vector width this provider's model produces."""

    def identity(self) -> EmbeddingIdentity:
        """Convenience: this provider's (model_id, dimension) as one value."""
        return EmbeddingIdentity(model_id=self.model_id, dimension=self.dimension)

    @abc.abstractmethod
    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """POST one batch of non-empty texts; return vectors in REQUEST order.

        Callers (``batching.py``) own chunking a large request into
        provider-sized batches, skipping empty/whitespace-only entries, and
        retrying transient failures -- this method does exactly one HTTP
        round trip and either returns len(texts) vectors or raises.
        """


class HTTPEmbeddingProvider(EmbeddingProvider):
    """Shared httpx client plumbing for HTTP-backed providers.

    Both concrete providers (TEI, OpenAI-compatible) are plain HTTP clients
    that differ only in wire format (``tei_provider.py`` / ``openai_
    provider.py`` override ``embed_batch``). This base class owns the one
    piece that must never regress: the API key reaches the client as a
    header and is never logged (#311 item 2 -- "the blocker"). TEI accepts
    the header if present but does not require one, so ``api_key=None``
    (the zero-config local-dev default) is a fully valid, supported state.
    """

    def __init__(
        self,
        *,
        base_url: str,
        model_id: str,
        dimension: int,
        timeout: float = 30.0,
        api_key: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model_id = model_id
        self._dimension = dimension
        self._timeout = timeout
        self._api_key = api_key
        # Injected only by tests (httpx.MockTransport) -- production callers
        # never pass this, so httpx opens its normal network transport.
        self._transport = transport
        self._client_lock = threading.Lock()
        self._client: httpx.Client | None = None

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def dimension(self) -> int:
        return self._dimension

    def _headers(self) -> dict[str, str]:
        # Authorization: Bearer <key> -- never logged (see redact_url above
        # for the one place a *URL* -- not this header -- could carry one).
        return {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}

    def _get_client(self) -> httpx.Client:
        if self._client is None:
            with self._client_lock:
                if self._client is None:
                    self._client = httpx.Client(
                        base_url=self._base_url,
                        timeout=self._timeout,
                        headers=self._headers(),
                        transport=self._transport,
                    )
        return self._client
