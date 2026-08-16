"""Unit tests for store_in_weaviate StartToClose budget (#228)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from src.services.embedding_defaults import (
    DEFAULT_BATCH_MAX_RETRIES,
    DEFAULT_BATCH_SIZE,
    DEFAULT_TIMEOUT_S,
)
from src.temporal.weaviate_store_budget import (
    _FIXED_OVERHEAD_SECONDS,
    _SECONDS_PER_BATCH,
    weaviate_store_timeout,
    weaviate_store_timeout_seconds,
)


@pytest.fixture(autouse=True)
def cleanup_test_data():
    """No-op override of the package-level DB-dependent autouse fixture."""
    yield


def test_budget_constants_match_embedding_defaults():
    """Budget and embedder must share one source of truth (Copilot #228 drift)."""
    expected = int(DEFAULT_BATCH_MAX_RETRIES * DEFAULT_TIMEOUT_S) + 10  # sleep budget
    assert _SECONDS_PER_BATCH == expected
    assert DEFAULT_BATCH_SIZE == 32


def test_one_batch_covers_full_retry_worst_case():
    # 1 batch: 100 + 30 = 130 — must exceed 3×30s TEI timeouts.
    assert weaviate_store_timeout_seconds(1) == _SECONDS_PER_BATCH + _FIXED_OVERHEAD_SECONDS
    assert weaviate_store_timeout_seconds(1) >= 90 + _FIXED_OVERHEAD_SECONDS
    assert weaviate_store_timeout_seconds(0) == weaviate_store_timeout_seconds(1)


def test_scales_with_serial_batch_count():
    # Budget is always serial worst-case so lowering EMBEDDING_MAX_CONCURRENCY
    # cannot under-budget. 44 chunks → 2 batches → 2*100 + 30 = 230.
    assert weaviate_store_timeout_seconds(44) == 2 * _SECONDS_PER_BATCH + _FIXED_OVERHEAD_SECONDS


def test_many_batches_scale_then_cap():
    # 535 chunks → 17 batches: 17*100+30=1730 → cap 900
    assert weaviate_store_timeout_seconds(535) == 900
    assert weaviate_store_timeout_seconds(10_000) == 900


def test_never_below_one_batch_budget():
    assert weaviate_store_timeout_seconds(1, batch_size=10_000) == (
        _SECONDS_PER_BATCH + _FIXED_OVERHEAD_SECONDS
    )


def test_timedelta_wrapper():
    assert weaviate_store_timeout(1) == timedelta(
        seconds=_SECONDS_PER_BATCH + _FIXED_OVERHEAD_SECONDS
    )
