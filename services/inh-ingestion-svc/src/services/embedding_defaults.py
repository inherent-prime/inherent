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
