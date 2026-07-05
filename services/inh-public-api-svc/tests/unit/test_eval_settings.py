"""Eval settings defaults + the per-workspace capture opt-out parser (evals v1).

Capture is ON by default (design decision: opt-out, not opt-in) with a
comma-separated env escape hatch until per-workspace settings exist."""

from src.config.settings import Settings


def test_eval_defaults():
    s = Settings()
    assert s.eval_capture_enabled is True
    assert s.eval_retention_days == 30
    assert s.eval_min_sample_size == 50
    assert s.eval_run_concurrency == 4
    assert s.eval_run_k == 5


def test_eval_optout_set_parses_csv():
    s = Settings(eval_capture_disabled_workspaces=" ws-a, ws-b ,,")
    assert s.eval_capture_optout_set() == {"ws-a", "ws-b"}


def test_eval_optout_set_empty():
    assert Settings().eval_capture_optout_set() == set()
