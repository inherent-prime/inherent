"""Shared embedding defaults -- single source of truth for both services (#311).

Before #311 these constants lived only in ``inh-ingestion-svc/src/services/
embedding_defaults.py`` and were silently NOT applied to the public-api query
path (``embed_query`` had zero retry -- see the #311 issue). Moving them here
means both services' embedder modules -- and the shared retry/batching helpers
in this package -- read the exact same numbers, so the divergence the issue
called out cannot reopen. ``inh-ingestion-svc``'s ``embedding_defaults.py``
re-exports these names (plus its own Temporal-budget constants) so existing
imports (``weaviate_store_budget.py``) keep working unchanged.
"""

from __future__ import annotations

# Default provider when EMBEDDING_PROVIDER is unset. TEI stays the default --
# non-negotiable (#311 item 8): `make up` / docker-compose with no new env
# vars must behave exactly as before this change.
DEFAULT_EMBEDDING_PROVIDER = "tei"

# Chunks per embedding HTTP call (TEI's default max-client-batch-size is
# typically ~32; OpenAI-compatible endpoints are far more permissive, but
# using the same conservative default keeps behavior predictable across
# providers -- an operator can raise EMBEDDING_BATCH_SIZE per deployment).
DEFAULT_BATCH_SIZE = 32

# In-flight batch POSTs per embed_texts call.
DEFAULT_MAX_CONCURRENCY = 2

# Per-request httpx timeout toward the embedding provider.
DEFAULT_TIMEOUT_S = 30.0

# Attempts per batch (including the first try) on transient provider failures.
DEFAULT_BATCH_MAX_RETRIES = 3

# Sum of exponential backoff sleeps across failed attempts (capped ~8s each).
# This is an ENFORCED ceiling, not a rough estimate: embed_batch_with_retry
# clamps each planned sleep so the cumulative sleep time across every retry
# of a single batch/single-text call can never exceed this many seconds --
# see its docstring. Ingestion's weaviate_store_budget.py still reasons about
# this value when sizing the store_in_weaviate Temporal StartToClose budget.
BATCH_RETRY_SLEEP_BUDGET_S = 10.0
