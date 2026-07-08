"""Feedback + auto-promotion rules (evals v1). The promotion rules here are the
single source of truth: REST and MCP both funnel into submit_feedback."""

from unittest.mock import AsyncMock

import pytest

from src.models.evals import FeedbackRequest
from src.services.eval_feedback import EventNotFoundError, submit_feedback

EVENT = {
    "event_id": "ev_1",
    "workspace_id": "ws-1",
    "query_text": "refund policy",
    "search_mode": "hybrid",
    "result_doc_ids": ["d1", "d2"],
    "result_chunk_ids": ["c1", "c2"],
}


def _db(event=EVENT):
    db = AsyncMock()
    db.get_eval_event.return_value = event
    db.upsert_eval_case.return_value = "case_1"
    return db


@pytest.mark.asyncio
async def test_unknown_event_raises():
    db = _db(event=None)
    with pytest.raises(EventNotFoundError):
        await submit_feedback(
            db, workspace_ids=["ws-1"], req=FeedbackRequest(event_id="ev_x", verdict="answered")
        )


@pytest.mark.asyncio
async def test_answered_with_chunks_promotes_matching_docs():
    db = _db()
    resp = await submit_feedback(
        db,
        workspace_ids=["ws-1"],
        req=FeedbackRequest(event_id="ev_1", verdict="answered", useful_chunk_ids=["c2"]),
    )
    assert resp.promoted and resp.case_id == "case_1"
    kwargs = db.upsert_eval_case.call_args.kwargs
    assert kwargs["expected_doc_ids"] == ["d2"]  # only the doc of chunk c2
    assert kwargs["relevance_grade"] == 2


@pytest.mark.asyncio
async def test_answered_without_chunks_promotes_all_result_docs():
    db = _db()
    resp = await submit_feedback(
        db, workspace_ids=["ws-1"], req=FeedbackRequest(event_id="ev_1", verdict="answered")
    )
    assert resp.promoted
    assert db.upsert_eval_case.call_args.kwargs["expected_doc_ids"] == ["d1", "d2"]


@pytest.mark.asyncio
async def test_partial_without_chunks_does_not_promote():
    db = _db()
    resp = await submit_feedback(
        db, workspace_ids=["ws-1"], req=FeedbackRequest(event_id="ev_1", verdict="partial")
    )
    assert not resp.promoted and resp.case_id is None
    db.upsert_eval_case.assert_not_awaited()


@pytest.mark.asyncio
async def test_partial_with_chunks_promotes_grade_one():
    db = _db()
    resp = await submit_feedback(
        db,
        workspace_ids=["ws-1"],
        req=FeedbackRequest(event_id="ev_1", verdict="partial", useful_chunk_ids=["c1"]),
    )
    assert resp.promoted and resp.case_id == "case_1"
    kwargs = db.upsert_eval_case.call_args.kwargs
    assert kwargs["relevance_grade"] == 1  # partial promotes at grade 1, not 2
    assert kwargs["expected_doc_ids"] == ["d1"]  # only the doc of chunk c1


@pytest.mark.asyncio
async def test_unmatched_chunk_ids_record_only():
    db = _db()
    resp = await submit_feedback(
        db,
        workspace_ids=["ws-1"],
        req=FeedbackRequest(
            event_id="ev_1", verdict="answered", useful_chunk_ids=["c_nonexistent"]
        ),
    )
    assert not resp.promoted and resp.case_id is None
    db.upsert_eval_feedback.assert_awaited_once()  # feedback still recorded
    db.upsert_eval_case.assert_not_awaited()  # but no garbage case promoted


@pytest.mark.asyncio
async def test_not_relevant_records_but_never_promotes():
    db = _db()
    resp = await submit_feedback(
        db, workspace_ids=["ws-1"], req=FeedbackRequest(event_id="ev_1", verdict="not_relevant")
    )
    assert not resp.promoted
    db.upsert_eval_feedback.assert_awaited_once()  # still recorded for gap stats
    db.upsert_eval_case.assert_not_awaited()
