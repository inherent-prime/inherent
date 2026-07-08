"""Feedback ingestion + auto-promotion into eval cases (evals v1).

Ground truth for retrieval evals comes from the consuming agent: only it knows
whether the returned evidence answered its question. This module is the single
place the promotion rules live; REST (api/v1/evals.py) and MCP (mcp_server)
both call submit_feedback.
"""

from __future__ import annotations

import uuid

from src.models.evals import FeedbackRequest, FeedbackResponse
from src.utils.logger import get_logger

logger = get_logger(__name__)

# Relevance grades for promoted cases (nDCG uses the grade; recall/MRR are binary).
_GRADE_BY_VERDICT = {"answered": 2, "partial": 1}


class EventNotFoundError(Exception):
    """The event id is unknown, expired (past retention), or in a foreign workspace."""


async def submit_feedback(
    db, *, workspace_ids: list[str], req: FeedbackRequest
) -> FeedbackResponse:
    """Record a verdict for one captured event; promote positives to a case.

    Promotion rules:
    - "answered"                      → promote, grade 2.
    - "partial" WITH useful_chunk_ids → promote, grade 1.
    - "partial" without evidence      → record only (nothing to label).
    - "not_relevant"                  → record only (feeds gap stats).
    Expected docs = docs of the named useful chunks; when "answered" names no
    chunks, all returned docs count as evidence.
    Duplicate feedback on one event is last-write-wins (PK upsert), and case
    upserts merge evidence, so re-submission never double-promotes.
    """
    event = await db.get_eval_event(event_id=req.event_id, workspace_ids=workspace_ids)
    if event is None:
        raise EventNotFoundError(req.event_id)

    await db.upsert_eval_feedback(
        event_id=req.event_id,
        workspace_id=event["workspace_id"],
        verdict=req.verdict,
        useful_chunk_ids=req.useful_chunk_ids or [],
        query_text=event["query_text"],
        note=req.note,
    )

    grade = _GRADE_BY_VERDICT.get(req.verdict)
    should_promote = grade == 2 or (grade == 1 and bool(req.useful_chunk_ids))
    if grade is None or not should_promote:
        return FeedbackResponse(event_id=req.event_id, promoted=False)

    if req.useful_chunk_ids:
        chunk_set = set(req.useful_chunk_ids)
        expected = [
            doc
            for doc, chunk in zip(event["result_doc_ids"], event["result_chunk_ids"])
            if chunk in chunk_set
        ]
        # De-dupe while preserving rank order.
        expected = list(dict.fromkeys(expected))
    else:
        expected = list(dict.fromkeys(event["result_doc_ids"]))

    if not expected:
        # Named chunks matched nothing we returned — record-only, don't promote garbage.
        logger.warning("eval_feedback_chunks_unmatched", event_id=req.event_id)
        return FeedbackResponse(event_id=req.event_id, promoted=False)

    case_id = await db.upsert_eval_case(
        case_id="case_" + uuid.uuid4().hex,
        workspace_id=event["workspace_id"],
        query_text=event["query_text"],
        expected_doc_ids=expected,
        relevance_grade=grade,
        source_event_id=req.event_id,
    )
    return FeedbackResponse(event_id=req.event_id, promoted=True, case_id=case_id)
