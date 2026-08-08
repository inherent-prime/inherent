"""Prometheus metrics must not use unbounded label values (#20).

Labeling a series by ``workspace_id`` / ``key_id`` in a multi-tenant system
creates one time-series per tenant/key that the client never frees — an
unbounded-memory leak. Tenant identity belongs in logs/exemplars, not labels.
"""

from __future__ import annotations

from src.services import metrics


def test_search_requests_total_not_labeled_by_workspace():
    assert metrics.search_requests_total._labelnames == ("mode",)


def test_rate_limit_metric_not_labeled_by_key_id():
    assert "key_id" not in metrics.RATE_LIMIT_EXCEEDED._labelnames


def test_record_search_request_still_increments():
    # Must remain callable (best-effort instrumentation) after dropping the label.
    metrics.record_search_request("hybrid")


def test_workspace_ownership_lookup_degraded_metric_labeled_by_source_only():
    # #184: only a small bounded `source` set (mongo / postgres_fallback /
    # mongo_ownership_check) -- never user_id/workspace_id, which are
    # unbounded (#20).
    assert metrics.workspace_ownership_lookup_degraded_total._labelnames == ("source",)


def test_record_workspace_ownership_lookup_degraded_still_increments():
    # Must remain callable (best-effort instrumentation).
    metrics.record_workspace_ownership_lookup_degraded("mongo")
