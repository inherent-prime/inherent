"""Evals v1 REST surface (design spec: evals-v1).

Read endpoints (feedback, scorecard, run reports, case list) need `search`
permission; mutating operator endpoints (start run, edit cases, purge events)
need `write`. Evals v1 is single-workspace: a resolved workspace is required.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel

from src.models.evals import (
    EvalCase,
    EvalRunReport,
    FeedbackRequest,
    FeedbackResponse,
    ScorecardResponse,
)
from src.services.auth import ResolvedAuth, resolve_workspace_search, resolve_workspace_write
from src.services.database import DatabaseService, get_database
from src.services.eval_feedback import EventNotFoundError, submit_feedback
from src.services.eval_runner import execute_run, get_run_report, start_run
from src.services.eval_scorecard import build_scorecard
from src.services.search import SearchService, get_search_service

router = APIRouter(prefix="/evals")


def _require_workspace(auth: ResolvedAuth) -> str:
    """Every evals endpoint is single-workspace; 400 without a resolved one."""
    if not auth.workspace_id:
        raise HTTPException(
            status_code=400,
            detail="Evals require a single workspace. Pass X-Workspace-Id.",
        )
    return auth.workspace_id


class CaseActiveUpdate(BaseModel):
    """PATCH body for enabling/disabling an eval case."""

    active: bool


@router.post("/feedback", response_model=FeedbackResponse)
async def report_feedback(
    request: FeedbackRequest,
    auth: Annotated[ResolvedAuth, Depends(resolve_workspace_search)],
    database: Annotated[DatabaseService, Depends(get_database)],
) -> FeedbackResponse:
    """Record a verdict on a captured search event; promotes to an eval case.

    Requires an API key with 'search' permission.
    """
    workspace_id = _require_workspace(auth)
    try:
        return await submit_feedback(database, workspace_ids=[workspace_id], req=request)
    except EventNotFoundError:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Unknown event_id '{request.event_id}'. Capture events expire "
                "after the retention window; report feedback promptly after searching."
            ),
        )


@router.get("/scorecard", response_model=ScorecardResponse)
async def get_scorecard(
    auth: Annotated[ResolvedAuth, Depends(resolve_workspace_search)],
    database: Annotated[DatabaseService, Depends(get_database)],
) -> ScorecardResponse:
    """Return the operator's day-one verdict surface for the workspace.

    Requires an API key with 'search' permission.
    """
    workspace_id = _require_workspace(auth)
    return await build_scorecard(database, workspace_id=workspace_id)


@router.get("/cases", response_model=list[EvalCase])
async def list_cases(
    auth: Annotated[ResolvedAuth, Depends(resolve_workspace_search)],
    database: Annotated[DatabaseService, Depends(get_database)],
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[EvalCase]:
    """Page through labeled eval cases for the workspace.

    Requires an API key with 'search' permission.
    """
    workspace_id = _require_workspace(auth)
    rows = await database.list_eval_cases(workspace_id=workspace_id, limit=limit, offset=offset)
    return [EvalCase(**row) for row in rows]


@router.patch("/cases/{case_id}")
async def patch_case(
    case_id: str,
    update: CaseActiveUpdate,
    auth: Annotated[ResolvedAuth, Depends(resolve_workspace_write)],
    database: Annotated[DatabaseService, Depends(get_database)],
) -> dict:
    """Enable or disable an eval case (soft delete from the replay set).

    Requires an API key with 'write' permission.
    """
    workspace_id = _require_workspace(auth)
    updated = await database.set_eval_case_active(
        workspace_id=workspace_id, case_id=case_id, active=update.active
    )
    if not updated:
        raise HTTPException(status_code=404, detail=f"Eval case '{case_id}' not found")
    return {"case_id": case_id, "active": update.active}


@router.post("/runs", status_code=202)
async def start_eval_run(
    auth: Annotated[ResolvedAuth, Depends(resolve_workspace_write)],
    database: Annotated[DatabaseService, Depends(get_database)],
    search_service: Annotated[SearchService, Depends(get_search_service)],
    background_tasks: BackgroundTasks,
) -> dict:
    """Start a mode-comparison eval run over the workspace's active cases.

    Replay executes as a background task; poll GET /v1/evals/runs/{run_id} for
    the report. Requires an API key with 'write' permission.
    """
    workspace_id = _require_workspace(auth)
    run_id = await start_run(database, workspace_id=workspace_id)
    if run_id is None:
        raise HTTPException(
            status_code=409,
            detail=(
                "No active eval cases yet. Label some queries first — see the "
                "eval quickstart in docs/getting-started/local.md."
            ),
        )
    background_tasks.add_task(
        execute_run,
        database,
        search_service,
        run_id=run_id,
        workspace_id=workspace_id,
        user_id=auth.key_info.user_id,
    )
    return {"run_id": run_id, "status": "running"}


@router.get("/runs/{run_id}", response_model=EvalRunReport)
async def get_run(
    run_id: str,
    auth: Annotated[ResolvedAuth, Depends(resolve_workspace_search)],
    database: Annotated[DatabaseService, Depends(get_database)],
) -> EvalRunReport:
    """Fetch an eval run's summary + per-case metrics.

    Requires an API key with 'search' permission.
    """
    workspace_id = _require_workspace(auth)
    report = await get_run_report(database, workspace_id=workspace_id, run_id=run_id)
    if report is None:
        raise HTTPException(status_code=404, detail=f"Eval run '{run_id}' not found")
    return report


@router.delete("/events")
async def delete_events(
    auth: Annotated[ResolvedAuth, Depends(resolve_workspace_write)],
    database: Annotated[DatabaseService, Depends(get_database)],
) -> dict:
    """Purge all captured search events for the workspace.

    Requires an API key with 'write' permission.
    """
    workspace_id = _require_workspace(auth)
    deleted = await database.delete_eval_events(workspace_id=workspace_id)
    return {"deleted": deleted}
