"""Mode-comparison eval runs (evals v1): replay cases across all three modes,
score with the promoted ranking metrics, aggregate per mode, survive failures."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.services.eval_runner import MODES, execute_run, start_run

CASES = [
    {
        "case_id": "case_1",
        "query_text": "refund policy",
        "expected_doc_ids": ["d1"],
        "relevance_grade": 2,
    },
    {
        "case_id": "case_2",
        "query_text": "sso setup",
        "expected_doc_ids": ["d9"],
        "relevance_grade": 1,
    },
]


def _search_service():
    # Every mode returns d1 then d2: case_1 hits rank 1, case_2 misses entirely.
    svc = AsyncMock()
    resp = MagicMock()
    resp.results = [MagicMock(document_id="d1"), MagicMock(document_id="d2")]
    svc.search.return_value = resp
    return svc


@pytest.mark.asyncio
async def test_start_run_returns_none_without_cases():
    db = AsyncMock()
    db.get_active_eval_cases.return_value = []
    assert await start_run(db, workspace_id="ws-1") is None
    db.insert_eval_run.assert_not_awaited()


@pytest.mark.asyncio
async def test_execute_run_scores_all_modes_and_completes():
    db = AsyncMock()
    db.get_active_eval_cases.return_value = CASES
    await execute_run(db, _search_service(), run_id="run_1", workspace_id="ws-1", user_id="u1")

    rows = db.insert_eval_run_results.call_args.kwargs["rows"]
    assert len(rows) == len(CASES) * len(MODES)  # 2 cases x 3 modes
    case1_rows = [r for r in rows if r["case_id"] == "case_1"]
    assert all(r["recall_at_k"] == 1.0 and r["mrr"] == 1.0 for r in case1_rows)
    finish = db.finish_eval_run.call_args.kwargs
    assert finish["status"] == "completed"
    # Aggregate = mean over cases: case_1 recall 1.0, case_2 recall 0.0 -> 0.5.
    assert finish["aggregates"]["hybrid"]["recall_at_k"] == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_execute_run_marks_failed_on_error():
    db = AsyncMock()
    db.get_active_eval_cases.return_value = CASES
    svc = AsyncMock()
    svc.search.side_effect = RuntimeError("weaviate down")
    await execute_run(db, svc, run_id="run_1", workspace_id="ws-1", user_id="u1")
    assert db.finish_eval_run.call_args.kwargs["status"] == "failed"


@pytest.mark.asyncio
async def test_execute_run_never_raises_even_if_finish_fails():
    # Both finish_eval_run calls raise (DB down for both writes): the background
    # task must still swallow. The test completing without raising IS the assertion.
    db = AsyncMock()
    db.get_active_eval_cases.return_value = CASES
    db.finish_eval_run.side_effect = RuntimeError("db down")
    await execute_run(db, _search_service(), run_id="run_1", workspace_id="ws-1", user_id="u1")
