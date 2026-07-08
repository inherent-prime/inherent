"""Offline smoke tests for the eval SQL methods on DatabaseService.

Deep behavior is covered by the compose E2E (tests/evals/test_evals_flywheel.py);
here we pin the method surface so service-layer mocks (AsyncMock) stay honest,
and check tenancy: every eval method must take/filter workspace scope."""

import inspect

from src.services.database import DatabaseService

EVAL_METHODS = [
    "insert_eval_event",
    "purge_expired_eval_events",
    "delete_eval_events",
    "get_eval_event",
    "upsert_eval_feedback",
    "upsert_eval_case",
    "list_eval_cases",
    "set_eval_case_active",
    "get_active_eval_cases",
    "eval_scorecard_counts",
    "insert_eval_run",
    "finish_eval_run",
    "insert_eval_run_results",
    "get_eval_run",
    "get_eval_run_results",
    "get_last_eval_run",
]


def test_eval_methods_exist_and_are_async():
    for name in EVAL_METHODS:
        fn = getattr(DatabaseService, name, None)
        assert fn is not None, f"DatabaseService.{name} missing"
        assert inspect.iscoroutinefunction(fn), f"DatabaseService.{name} must be async"


def test_eval_methods_are_workspace_scoped():
    # Tenancy guard: every eval method except pure run-child lookups must take
    # workspace scope explicitly (workspace_id or workspace_ids).
    exempt = {"insert_eval_run_results", "get_eval_run_results", "finish_eval_run"}
    for name in set(EVAL_METHODS) - exempt:
        params = inspect.signature(getattr(DatabaseService, name)).parameters
        assert "workspace_id" in params or "workspace_ids" in params, name
