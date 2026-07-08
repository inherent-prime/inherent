"""Scorecard assembly (evals v1): rates, sample-size honesty, plain-English summary."""

from unittest.mock import AsyncMock, patch

import pytest

from src.services.eval_scorecard import build_scorecard

COUNTS = {
    "captured_events": 100,
    "verdict_distribution": {"sufficient": 80, "insufficient_evidence": 15, "low_confidence": 5},
    "feedback_distribution": {"answered": 18, "partial": 4, "not_relevant": 3},
    "eval_case_count": 20,
    "corpus_gaps": ["how do i cancel", "sso setup"],
}


def _db(counts=COUNTS, last_run=None):
    db = AsyncMock()
    db.eval_scorecard_counts.return_value = counts
    db.get_last_eval_run.return_value = last_run
    return db


@pytest.mark.asyncio
async def test_scorecard_rates_and_low_confidence_flag():
    with patch("src.services.eval_scorecard.settings") as s:
        s.eval_min_sample_size = 50
        s.eval_retention_days = 30
        sc = await build_scorecard(_db(), workspace_id="ws-1")
    assert sc.feedback_count == 25
    assert sc.feedback_rate == pytest.approx(0.25)
    assert sc.answer_rate == pytest.approx((18 + 4) / 25)
    assert sc.low_confidence is True  # 20 cases < 50 threshold
    assert sc.corpus_gaps == ["how do i cancel", "sso setup"]
    assert "20" in sc.summary  # summary mentions the sample size


@pytest.mark.asyncio
async def test_scorecard_empty_workspace():
    empty = {
        "captured_events": 0,
        "verdict_distribution": {},
        "feedback_distribution": {},
        "eval_case_count": 0,
        "corpus_gaps": [],
    }
    with patch("src.services.eval_scorecard.settings") as s:
        s.eval_min_sample_size = 50
        s.eval_retention_days = 30
        sc = await build_scorecard(_db(counts=empty), workspace_id="ws-1")
    assert sc.answer_rate is None and sc.feedback_rate == 0.0
    assert sc.low_confidence is True
