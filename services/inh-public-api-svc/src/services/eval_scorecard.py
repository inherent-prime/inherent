"""Scorecard assembly (evals v1): the operator's day-one verdict surface.

Pure aggregation over eval tables — no LLM, no network beyond Postgres. The
summary field is deliberately plain English: the trial evaluator judging
Inherent is a human even though the API's end users are agents.
"""

from __future__ import annotations

from src.config.settings import settings
from src.models.evals import ModeMetrics, RunSummary, ScorecardResponse


def _summarize(sc: ScorecardResponse) -> str:
    """One-paragraph plain-English readout, honest about sample size."""
    if sc.captured_events == 0 and sc.eval_case_count == 0:
        return (
            "No search traffic captured yet. Run some searches (or the trial "
            "labeling script in docs/examples/eval_trial.py) to start building "
            "your eval set."
        )
    parts = [
        f"Captured {sc.captured_events} searches in the last {settings.eval_retention_days} days."
    ]
    if sc.answer_rate is not None:
        parts.append(
            f"Agents reported feedback on {sc.feedback_count} of them "
            f"({sc.feedback_rate:.0%}); {sc.answer_rate:.0%} were answered fully or partially."
        )
    parts.append(f"{sc.eval_case_count} labeled eval cases accumulated.")
    if sc.low_confidence:
        parts.append(
            f"Fewer than {sc.min_sample_size} labeled cases — treat all rates as "
            "low-confidence until more feedback accumulates."
        )
    if sc.corpus_gaps:
        parts.append(
            f"Possible corpus gaps (queries judged not relevant): {', '.join(sc.corpus_gaps)}."
        )
    return " ".join(parts)


async def build_scorecard(db, *, workspace_id: str) -> ScorecardResponse:
    """Assemble the scorecard from aggregate counts + the latest run summary."""
    counts = await db.eval_scorecard_counts(
        workspace_id=workspace_id, window_days=settings.eval_retention_days
    )
    feedback_count = sum(counts["feedback_distribution"].values())
    captured = counts["captured_events"]
    positive = counts["feedback_distribution"].get("answered", 0) + counts[
        "feedback_distribution"
    ].get("partial", 0)

    last_run_row = await db.get_last_eval_run(workspace_id=workspace_id)
    last_run = None
    if last_run_row:
        last_run = RunSummary(
            run_id=last_run_row["run_id"],
            status=last_run_row["status"],
            case_count=last_run_row["case_count"],
            k=last_run_row["k"],
            aggregates={m: ModeMetrics(**v) for m, v in (last_run_row["aggregates"] or {}).items()},
            created_at=last_run_row["created_at"],
            finished_at=last_run_row["finished_at"],
            error=last_run_row["error"],
        )

    sc = ScorecardResponse(
        workspace_id=workspace_id,
        captured_events=captured,
        feedback_count=feedback_count,
        feedback_rate=(feedback_count / captured) if captured else 0.0,
        answer_rate=(positive / feedback_count) if feedback_count else None,
        verdict_distribution=counts["verdict_distribution"],
        feedback_distribution=counts["feedback_distribution"],
        corpus_gaps=counts["corpus_gaps"],
        eval_case_count=counts["eval_case_count"],
        low_confidence=counts["eval_case_count"] < settings.eval_min_sample_size,
        min_sample_size=settings.eval_min_sample_size,
        last_run=last_run,
        summary="",
    )
    sc.summary = _summarize(sc)
    return sc
