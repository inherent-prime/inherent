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

# Ceiling on store_in_weaviate's StartToClose so one document cannot pin a
# worker slot indefinitely (#228, raised for #298 -- see below).
#
# Originally 900s (15m), sized off the same worst-case-serial-retries model
# as the rest of this budget. That model breaks down at real-world scale: a
# 60,215-chunk document (#298's repro) needs well over 15 minutes of wall
# clock to embed on CPU TEI even with *zero* retries, so every attempt hit
# this ceiling and was cancelled deterministically -- retries changed
# nothing, because a flat StartToClose has no way to tell "still making
# progress, just slow" from "hung".
#
# store_in_weaviate now heartbeats real per-batch progress (#298:
# weaviate.py's store_chunks_with_tenant), and its execute_activity call in
# document_ingestion.py pairs that with a heartbeat_timeout (weaviate_store_
# budget.weaviate_store_heartbeat_timeout). That is what makes raising this ceiling safe
# instead of just moving the same failure further out: a worker that stops
# advancing (crash, deadlock, network partition) is now caught by
# heartbeat_timeout in roughly one worst-case batch, independent of how
# large this ceiling is. This constant's job shrinks to "how long may a
# document that IS legitimately progressing hold a worker slot" -- a much
# larger number is fine because a stalled one no longer waits it out.
#
# 7200s (2h) comfortably covers #298's 60,215-chunk document at realistic
# (non-worst-case-retry-storm) CPU TEI throughput, while still bounding
# worst-case slot occupancy to a fixed, operationally-sane window rather
# than removing the ceiling outright.
STORE_MAX_TIMEOUT_SECONDS = 7200
