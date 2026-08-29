"""Shared defaults for TEI embedder and store_in_weaviate budget (#228).

Embedder env overrides (``EMBEDDING_*``) and the workflow StartToClose formula
must not invent independent magic numbers. This module is the single source
of truth for fallbacks used by ``embedder.py`` and for the budget constants
in ``weaviate_store_budget.py``.

Why the budget does not read env at workflow time: Temporal StartToClose is
chosen *before* the activity runs, inside the deterministic workflow sandbox,
so it cannot observe live env without an extra activity hop. The formula
therefore uses these defaults and always budgets for **serial** batch
completion (one batch at a time). That way lowering ``EMBEDDING_MAX_CONCURRENCY``
cannot reintroduce #228 underestimation; raising concurrency only finishes
earlier inside the same budget.

#311: the batching/retry constants below (everything except the two STORE_*
budget constants) now have a twin copy in ``inh_contracts.embedding.defaults``
-- that package is the actual shared source of truth ``embedder.py`` builds
its provider from. The values here are deliberately NOT imported from there:
``weaviate_store_budget.py`` (the consumer of these constants) is imported
inside the Temporal *workflow sandbox*, and ``inh_contracts.embedding``'s
package ``__init__`` transitively imports httpx/threading -- exactly what
"stdlib + embedding_defaults only" above is protecting against. Instead,
``tests/test_embedding_defaults_contract.py`` pins the two copies equal, the
same anti-drift pattern already used for the URL/dim defaults in
``tests/test_settings_config_dedup_contract.py``.
"""

from __future__ import annotations

# Chunks per TEI POST (TEI max-client-batch-size is typically ~32).
DEFAULT_BATCH_SIZE = 32

# In-flight TEI POSTs per embed_texts call. Product of this and
# TEMPORAL_MAX_CONCURRENT_ACTIVITIES is the TEI in-flight cap under bulk.
DEFAULT_MAX_CONCURRENCY = 2

# Per-request httpx timeout toward the TEI sidecar.
DEFAULT_TIMEOUT_S = 30.0

# Attempts per batch (including the first try) on transient TEI failures.
DEFAULT_BATCH_MAX_RETRIES = 3

# Sum of exponential backoff sleeps across failed attempts (capped ~8s each).
BATCH_RETRY_SLEEP_BUDGET_S = 10

# Weaviate write + fencing + lineage after embeddings finish.
STORE_FIXED_OVERHEAD_SECONDS = 30

# Hard cap so a pathological multi-thousand-chunk doc cannot pin a worker slot.
STORE_MAX_TIMEOUT_SECONDS = 900
