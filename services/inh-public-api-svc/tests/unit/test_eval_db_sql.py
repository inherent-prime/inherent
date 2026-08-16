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
    "get_eval_case_ids",
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


def test_get_active_eval_cases_accepts_optional_scoping():
    # #250: run-replay scoping. Both params optional and default to None so
    # every existing unscoped caller keeps working unchanged.
    params = inspect.signature(DatabaseService.get_active_eval_cases).parameters
    assert "case_ids" in params
    assert params["case_ids"].default is None
    assert "since" in params
    assert params["since"].default is None


def test_get_eval_case_ids_is_workspace_scoped():
    params = inspect.signature(DatabaseService.get_eval_case_ids).parameters
    assert "workspace_id" in params
    assert "case_ids" in params


def test_delete_eval_events_accepts_include_cases_defaulting_false():
    # #250: purge of labeled cases is opt-in. Default False preserves the
    # documented contract (raw events ephemeral, labeled cases durable) that
    # tests/evals/test_evals_flywheel.py asserts against a live stack.
    params = inspect.signature(DatabaseService.delete_eval_events).parameters
    assert "include_cases" in params
    assert params["include_cases"].default is False
