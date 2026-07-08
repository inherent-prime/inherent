"""Shape tests for the evals v1 Pydantic models and the new SearchResponse
event_id field (the feedback handle + external-eval join key)."""

import pytest
from pydantic import ValidationError

from src.models.evals import FeedbackRequest, ModeMetrics, ScorecardResponse
from src.models.search import SearchResponse


def test_feedback_request_validates_verdict():
    ok = FeedbackRequest(event_id="ev_1", verdict="answered")
    assert ok.useful_chunk_ids is None
    with pytest.raises(ValidationError):
        FeedbackRequest(event_id="ev_1", verdict="great")  # not a valid verdict


def test_search_response_has_optional_event_id():
    resp = SearchResponse(
        results=[], query="q", total_results=0, processing_time_ms=1.0, search_mode="semantic"
    )
    assert resp.event_id is None  # backward-compatible default


def test_scorecard_minimal():
    sc = ScorecardResponse(
        workspace_id="ws-1",
        captured_events=0,
        feedback_count=0,
        feedback_rate=0.0,
        answer_rate=None,
        verdict_distribution={},
        feedback_distribution={},
        corpus_gaps=[],
        eval_case_count=0,
        low_confidence=True,
        min_sample_size=50,
        last_run=None,
        summary="No data yet.",
    )
    assert sc.low_confidence is True
    assert ModeMetrics(recall_at_k=1.0, mrr=1.0, ndcg_at_k=1.0).recall_at_k == 1.0
