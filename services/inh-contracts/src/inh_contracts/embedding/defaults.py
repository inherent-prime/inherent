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
# This is an ENFORCED ceiling on SLEEP time only, not a rough estimate and
# NOT a wall-clock bound (PR #314 review finding 2 -- see retry.py's
# docstring and `max_wall_clock_s` below for the distinction):
# embed_batch_with_retry clamps each planned sleep so the cumulative sleep
# time across every retry of a single batch/single-text call can never
# exceed this many seconds -- but each ATTEMPT can still independently take
# up to DEFAULT_TIMEOUT_S before this budget is even consulted. Ingestion's
# weaviate_store_budget.py reasons about the FULL formula (attempts *
# timeout + this budget), not this constant alone, when sizing the
# store_in_weaviate Temporal StartToClose budget.
BATCH_RETRY_SLEEP_BUDGET_S = 10.0

# --- Query-path (interactive) retry defaults ------------------------------
#
# The constants above size a Temporal activity budget for a BACKGROUND
# batch write -- the wrong shape for an embed call inside a synchronous,
# user-facing search request. PR #314 review finding 2: with the batch
# defaults, embed_query's worst case (DEFAULT_BATCH_MAX_RETRIES *
# DEFAULT_TIMEOUT_S + BATCH_RETRY_SLEEP_BUDGET_S = 3*30 + 10 = 100s, ~91.5s
# in the typical case where the jittered sleeps don't hit the full budget)
# blew right past the #311 issue's own cited 15s consumer-side ceiling on
# interactive chat search -- retrying more than compounded the problem.
#
# These are deliberately smaller and used ONLY by inh-public-api-svc's query
# embedder (`embed_query`), which has a real caller-side deadline. Worst
# case: DEFAULT_QUERY_MAX_RETRIES * DEFAULT_QUERY_TIMEOUT_S +
# QUERY_RETRY_SLEEP_BUDGET_S = 2*5 + 2 = 12s -- 3s of headroom under the 15s
# ceiling for the Weaviate query and response assembly that follow the
# embed call in the same request. See retry.py's `max_wall_clock_s`, which
# computes this formula, and
# inh-public-api-svc/tests/unit/test_embedder.py for the test that pins it.
DEFAULT_QUERY_TIMEOUT_S = 5.0
DEFAULT_QUERY_MAX_RETRIES = 2
QUERY_RETRY_SLEEP_BUDGET_S = 2.0
