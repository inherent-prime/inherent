"""Retry-with-backoff for embedding batch calls (#311 item 5).

Ported from ``inh-ingestion-svc``'s ``embedder.py`` (the only side that had
retry before #311 -- ``embed_query`` on the public-api query path had zero
retry, the exact divergence this issue calls out) so BOTH the write and query
paths now share one retry policy.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable

import httpx

from inh_contracts.embedding.defaults import BATCH_RETRY_SLEEP_BUDGET_S, DEFAULT_BATCH_MAX_RETRIES
from inh_contracts.embedding.provider import EmbeddingProvider, redact_url

logger = logging.getLogger(__name__)


def is_transient_embed_error(exc: BaseException) -> bool:
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


def embed_batch_with_retry(
    provider: EmbeddingProvider,
    texts: list[str],
    *,
    max_retries: int = DEFAULT_BATCH_MAX_RETRIES,
    retry_budget_s: float = BATCH_RETRY_SLEEP_BUDGET_S,
    sleep: Callable[[float], None] = time.sleep,
) -> list[list[float]]:
    """Call ``provider.embed_batch(texts)``; retry *transient* failures.

    Exponential backoff (base 0.5s * 2^(attempt-1), capped at 8s) plus 0-50%
    jitter, same as the pre-#311 ingestion-only implementation. Non-transient
    errors (see ``is_transient_embed_error``) raise on the first failure --
    4xx (except 429) never retries.

    Wall-clock bound (#311 item 5): ``retry_budget_s`` is an ENFORCED ceiling
    on the total time this call spends sleeping across every attempt, not
    just an accounting estimate. Each planned delay is clamped to whatever
    budget remains before sleeping; once the budget is exhausted the loop
    stops retrying and raises the last error immediately, even if
    ``max_retries`` attempts remain. This is what lets a caller's own request
    timeout stay the real ceiling -- retries can add AT MOST
    ``retry_budget_s`` seconds on top of the calls themselves, regardless of
    how ``max_retries`` is configured.
    """
    attempts = max(1, max_retries)
    last_exc: BaseException | None = None
    slept_total = 0.0
    for attempt in range(1, attempts + 1):
        try:
            return provider.embed_batch(texts)
        except Exception as exc:
            last_exc = exc
            if not is_transient_embed_error(exc) or attempt >= attempts:
                break
            remaining_budget = retry_budget_s - slept_total
            if remaining_budget <= 0:
                # Sleep budget already spent -- stop retrying now rather than
                # silently exceeding the caller's timeout budget.
                break
            base = min(8.0, 0.5 * (2 ** (attempt - 1)))
            # The only thing this randomness protects is retry *timing*: it
            # de-synchronises concurrent batches so they do not re-hit the
            # provider in lockstep. Nothing here is a secret, a token or a
            # key, so a predictable sequence costs an attacker nothing and a
            # CSPRNG buys nothing -- hence the B311 waiver on the next line.
            delay = min(base * (0.5 + random.random() * 0.5), remaining_budget)  # nosec B311
            slept_total += delay
            # redact_url in case the exception message echoes back a URL with
            # embedded credentials (an operator misconfiguration -- the
            # supported key path, EMBEDDING_API_KEY, never reaches this
            # string at all since it travels as a header, not the URL).
            logger.warning(
                "embed_batch_retry attempt=%d max_attempts=%d delay_s=%.3f error=%s",
                attempt,
                attempts,
                round(delay, 3),
                redact_url(str(exc)),
            )
            sleep(delay)
    assert last_exc is not None
    raise last_exc
