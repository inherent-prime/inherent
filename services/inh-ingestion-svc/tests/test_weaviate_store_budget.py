"""Unit tests for store_in_weaviate StartToClose budget (#228)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from src.services.embedding_defaults import (
    DEFAULT_BATCH_MAX_RETRIES,
    DEFAULT_BATCH_SIZE,
    DEFAULT_TIMEOUT_S,
    STORE_MAX_TIMEOUT_SECONDS,
)
from src.temporal.weaviate_store_budget import (
    _FIXED_OVERHEAD_SECONDS,
    _HEARTBEAT_TIMEOUT_SECONDS,
    _SECONDS_PER_BATCH,
    weaviate_store_heartbeat_timeout,
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
    # #298: cap raised 900 -> 7200 (see embedding_defaults.STORE_MAX_TIMEOUT_
    # SECONDS docstring) now that heartbeat_timeout, not StartToClose, is
    # what catches a genuinely wedged worker. 2300 chunks -> 72 batches:
    # 72*100+30=7230 -> capped at the new ceiling.
    assert STORE_MAX_TIMEOUT_SECONDS == 7200
    assert weaviate_store_timeout_seconds(2300) == STORE_MAX_TIMEOUT_SECONDS
    assert weaviate_store_timeout_seconds(10_000) == STORE_MAX_TIMEOUT_SECONDS


def test_298_repro_document_gets_the_raised_cap_not_the_old_one():
    """#298's repro (CUAD_v1.json, 60,215 chunks) hit the OLD 900s cap on
    every attempt and never completed. It must now get the raised ceiling,
    with real headroom over the old one -- not just a formula output that
    still happens to be small."""
    seconds = weaviate_store_timeout_seconds(60_215)
    assert seconds == STORE_MAX_TIMEOUT_SECONDS
    assert seconds > 900  # strictly more room than the old, insufficient cap


def test_heartbeat_timeout_catches_a_wedge_far_before_the_full_budget():
    """The whole point of #298: a worker that stops advancing must be
    detected in a small fraction of the document's StartToClose budget, not
    by waiting out the full (now much larger) ceiling."""
    hb = weaviate_store_heartbeat_timeout()
    full_budget = weaviate_store_timeout(60_215)
    assert hb.total_seconds() == _HEARTBEAT_TIMEOUT_SECONDS
    assert hb < full_budget
    # At least an order of magnitude sooner for a large, capped document.
    assert full_budget.total_seconds() / hb.total_seconds() >= 10


def test_heartbeat_timeout_exceeds_worst_case_single_batch():
    """heartbeat_timeout must clear one batch's own worst-case retry+backoff
    wall clock with margin, or Temporal would kill a healthy batch that is
    still legitimately retrying."""
    hb = weaviate_store_heartbeat_timeout()
    assert hb.total_seconds() > _SECONDS_PER_BATCH


def test_never_below_one_batch_budget():
    assert weaviate_store_timeout_seconds(1, batch_size=10_000) == (
        _SECONDS_PER_BATCH + _FIXED_OVERHEAD_SECONDS
    )


def test_timedelta_wrapper():
    assert weaviate_store_timeout(1) == timedelta(
        seconds=_SECONDS_PER_BATCH + _FIXED_OVERHEAD_SECONDS
    )
