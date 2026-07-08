"""Evals v1 models — traffic-mined retrieval evals (design spec: evals-v1).

The flywheel: every search is captured as an event (event_id on the search
response) → a consuming agent (or the trial script in docs/examples) reports
feedback on that event → positive feedback auto-promotes into an eval case →
eval runs replay cases across the three retrieval modes and score them with
the ranking metrics in src/services/ranking_metrics.py.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

FeedbackVerdict = Literal["answered", "partial", "not_relevant"]


class FeedbackRequest(BaseModel):
    """An agent's verdict on one captured search event."""

    event_id: str = Field(..., min_length=1, max_length=100)
    verdict: FeedbackVerdict
    useful_chunk_ids: list[str] | None = Field(
        default=None,
        description="Chunk ids from the search results that actually answered the query",
    )
    note: str | None = Field(default=None, max_length=2000)


class FeedbackResponse(BaseModel):
    """Outcome of feedback submission; promoted=True when an eval case was created/updated."""

    event_id: str
    promoted: bool
    case_id: str | None = None


class EvalCase(BaseModel):
    """A labeled eval case (query + expected evidence) promoted from feedback."""

    case_id: str
    workspace_id: str
    query_text: str
    expected_doc_ids: list[str]
    relevance_grade: int
    active: bool
    created_at: datetime
    updated_at: datetime


class ModeMetrics(BaseModel):
    """Aggregate ranking metrics for one retrieval mode."""

    recall_at_k: float
    mrr: float
    ndcg_at_k: float


class RunSummary(BaseModel):
    """Eval run metadata + aggregate metrics per mode."""

    run_id: str
    status: Literal["running", "completed", "failed"]
    case_count: int
    k: int
    aggregates: dict[str, ModeMetrics]
    created_at: datetime
    finished_at: datetime | None = None
    error: str | None = None


class CaseModeResult(BaseModel):
    """Per-case, per-mode metrics inside a run report."""

    case_id: str
    query_text: str
    mode: str
    recall_at_k: float
    mrr: float
    ndcg_at_k: float


class EvalRunReport(BaseModel):
    """Full run report: summary + per-case detail."""

    run: RunSummary
    per_case: list[CaseModeResult]


class ScorecardResponse(BaseModel):
    """The operator's day-one verdict surface (human-legible by design).

    - answer_rate is None until any feedback exists.
    - low_confidence flags labeled-case counts below min_sample_size, so
      numbers are never presented as more certain than they are.
    - summary is plain English: the one field a human evaluator reads first.
    """

    workspace_id: str
    captured_events: int
    feedback_count: int
    feedback_rate: float
    answer_rate: float | None
    verdict_distribution: dict[str, int]
    feedback_distribution: dict[str, int]
    corpus_gaps: list[str]
    eval_case_count: int
    low_confidence: bool
    min_sample_size: int
    last_run: RunSummary | None
    summary: str
