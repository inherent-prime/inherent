"""Mode-comparison eval runs (evals v1).

Replays every active eval case through SearchService in each retrieval mode
(keyword / semantic / hybrid) and scores the ranked doc ids with the promoted
ranking metrics. Runs execute as a FastAPI background task with bounded
concurrency so replay can never starve live serving. Produces the trial
artifact: "recall@5 0.91 hybrid vs 0.78 keyword — on YOUR corpus".

Optional replay scoping (#250): both ``start_run`` (which decides case_count
and the 409) and ``execute_run`` (which does the actual replay) accept the
same ``case_ids``/``since`` filters and must be called with the same values --
they query ``get_active_eval_cases`` independently, so scoping only one of
the two call sites reintroduces the order-fragility the issue is about.
Omitting both keeps the unscoped default (every active case for the
workspace), unchanged from before #250.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime

from src.config.settings import settings
from src.models.evals import CaseModeResult, EvalRunReport, ModeMetrics, RunSummary
from src.models.search import SearchRequest
from src.services.ranking_metrics import mrr, ndcg_at_k, recall_at_k
from src.utils.logger import get_logger

logger = get_logger(__name__)

MODES = ("keyword", "semantic", "hybrid")


async def start_run(
    db,
    *,
    workspace_id: str,
    case_ids: list[str] | None = None,
    since: datetime | None = None,
) -> str | None:
    """Create the run row; None when there is nothing to replay (caller 409s).

    ``case_ids``/``since`` optionally scope the replay set (#250); the caller
    must pass the identical values to ``execute_run`` so both independent
    ``get_active_eval_cases`` fetches agree.
    """
    cases = await db.get_active_eval_cases(
        workspace_id=workspace_id, case_ids=case_ids, since=since
    )
    if not cases:
        return None
    run_id = "run_" + uuid.uuid4().hex
    await db.insert_eval_run(
        run_id=run_id,
        workspace_id=workspace_id,
        case_count=len(cases),
        k=settings.eval_run_k,
    )
    return run_id


async def _score_case_mode(semaphore, search_service, *, workspace_id, user_id, case, mode, k):
    """One replay: search this case's query in one mode, compute the metrics."""
    async with semaphore:
        response = await search_service.search(
            workspace_id=workspace_id,
            user_id=user_id,
            request=SearchRequest(query=case["query_text"], limit=max(k, 10), search_mode=mode),
        )
    ranked = [r.document_id for r in response.results]
    expected = list(case["expected_doc_ids"])
    grades = {doc: float(case["relevance_grade"]) for doc in expected}
    return {
        "case_id": case["case_id"],
        "query_text": case["query_text"],
        "mode": mode,
        "recall_at_k": recall_at_k(ranked, expected, k),
        "mrr": mrr(ranked, expected),
        "ndcg_at_k": ndcg_at_k(ranked, grades, k),
    }


async def execute_run(
    db,
    search_service,
    *,
    run_id: str,
    workspace_id: str,
    user_id: str,
    case_ids: list[str] | None = None,
    since: datetime | None = None,
) -> None:
    """Background body: replay cases x modes, store rows + per-mode means.

    All-or-nothing by design (spec): any replay failure marks the run failed
    with the error; partial results are never reported.

    ``case_ids``/``since`` optionally scope the replay set (#250) -- must be
    the SAME values passed to the ``start_run`` call that created this run,
    otherwise the run's reported ``case_count`` and its actual replayed rows
    disagree.
    """
    k = settings.eval_run_k
    try:
        cases = await db.get_active_eval_cases(
            workspace_id=workspace_id, case_ids=case_ids, since=since
        )
        semaphore = asyncio.Semaphore(settings.eval_run_concurrency)
        rows = await asyncio.gather(
            *[
                _score_case_mode(
                    semaphore,
                    search_service,
                    workspace_id=workspace_id,
                    user_id=user_id,
                    case=case,
                    mode=mode,
                    k=k,
                )
                for case in cases
                for mode in MODES
            ]
        )
        await db.insert_eval_run_results(run_id=run_id, rows=list(rows))

        aggregates = {}
        for mode in MODES:
            mode_rows = [r for r in rows if r["mode"] == mode]
            n = len(mode_rows) or 1
            aggregates[mode] = {
                "recall_at_k": sum(r["recall_at_k"] for r in mode_rows) / n,
                "mrr": sum(r["mrr"] for r in mode_rows) / n,
                "ndcg_at_k": sum(r["ndcg_at_k"] for r in mode_rows) / n,
            }
        await db.finish_eval_run(
            run_id=run_id, status="completed", aggregates=aggregates, error=None
        )
    except Exception as exc:  # noqa: BLE001 — background task: record, never raise
        logger.error("eval_run_failed", run_id=run_id, error=str(exc))
        # The failure-path write itself can fail (DB down for both writes); it
        # must not propagate out of the background task either.
        try:
            await db.finish_eval_run(run_id=run_id, status="failed", aggregates={}, error=str(exc))
        except Exception as inner:  # noqa: BLE001 — failure path must not raise either
            logger.error("eval_run_finish_failed", run_id=run_id, error=str(inner))


async def get_run_report(db, *, workspace_id: str, run_id: str) -> EvalRunReport | None:
    """Fetch a run + its per-case rows; None when unknown/foreign run."""
    run_row = await db.get_eval_run(workspace_id=workspace_id, run_id=run_id)
    if run_row is None:
        return None
    result_rows = await db.get_eval_run_results(run_id=run_id)
    run = RunSummary(
        run_id=run_row["run_id"],
        status=run_row["status"],
        case_count=run_row["case_count"],
        k=run_row["k"],
        aggregates={m: ModeMetrics(**v) for m, v in (run_row["aggregates"] or {}).items()},
        created_at=run_row["created_at"],
        finished_at=run_row["finished_at"],
        error=run_row["error"],
    )
    return EvalRunReport(run=run, per_case=[CaseModeResult(**r) for r in result_rows])
