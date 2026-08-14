"""StartToClose budget for ``store_in_weaviate`` (#228).

The activity embeds every chunk of a document in one shot. A flat 60s
budget is enough for small docs at idle load and not enough once TEI
queue time climbs (2026-08-10 bulk-upload incident: queue_time p50
18.3s, 70/83 documents timed out after five attempts).

Scale the budget with chunk count so large documents get enough wall
clock without giving every tiny doc a 15-minute hang. Pure function so
workflow code and unit tests share one formula (Temporal sandbox-safe:
stdlib + embedding_defaults only).

The per-batch component covers the embedder's **worst-case** batch wall
clock under ``DEFAULT_BATCH_MAX_RETRIES`` × ``DEFAULT_TIMEOUT_S`` plus
jitter sleeps (#229 review): otherwise StartToClose cancels mid per-batch
retry and Temporal re-embeds the whole document.

Wall-clock is always computed for **serial** batch completion so an
operator lowering ``EMBEDDING_MAX_CONCURRENCY`` cannot under-budget the
activity (see ``embedding_defaults`` module docstring).
"""

from __future__ import annotations

from datetime import timedelta

from src.services.embedding_defaults import (
    BATCH_RETRY_SLEEP_BUDGET_S,
    DEFAULT_BATCH_MAX_RETRIES,
    DEFAULT_BATCH_SIZE,
    DEFAULT_TIMEOUT_S,
    STORE_FIXED_OVERHEAD_SECONDS,
    STORE_MAX_TIMEOUT_SECONDS,
)

# Worst-case wall clock for one batch (retries run serially per batch).
_SECONDS_PER_BATCH = int(
    DEFAULT_BATCH_MAX_RETRIES * DEFAULT_TIMEOUT_S + BATCH_RETRY_SLEEP_BUDGET_S
)  # 100 with defaults
# Re-export for tests that pin the formula.
_SECONDS_PER_WAVE = _SECONDS_PER_BATCH
_FIXED_OVERHEAD_SECONDS = STORE_FIXED_OVERHEAD_SECONDS
_MAX_SECONDS = STORE_MAX_TIMEOUT_SECONDS


def weaviate_store_timeout_seconds(
    chunk_count: int,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> int:
    """Return StartToClose seconds for embedding + Weaviate store.

    ``chunk_count`` is the number of staged chunks for the document.
    Empty/zero counts still get one synthetic batch so a no-op store
    cannot hang on a misconfigured zero timeout.

    Wall-clock model (serial worst case)::

        batches = ceil(chunk_count / batch_size)
        seconds = batches * (attempts * http_timeout + sleep_budget) + overhead
    """
    n = max(0, int(chunk_count))
    size = max(1, int(batch_size))
    batches = max(1, (n + size - 1) // size) if n > 0 else 1
    raw = batches * _SECONDS_PER_BATCH + STORE_FIXED_OVERHEAD_SECONDS
    return min(STORE_MAX_TIMEOUT_SECONDS, raw)


def weaviate_store_timeout(chunk_count: int) -> timedelta:
    """Timedelta form used at ``workflow.execute_activity`` call sites."""
    return timedelta(seconds=weaviate_store_timeout_seconds(chunk_count))
