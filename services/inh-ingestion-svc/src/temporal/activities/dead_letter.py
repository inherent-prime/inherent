"""Activities for the dead-letter table: recording terminal failures (#8) and
resolving jobs once a retried document succeeds (#249).

With non-blocking (async) workflow starts (#18), the MQ consumer no longer
observes the workflow's execution outcome, so terminal failures are recorded
from the WORKFLOW's failure path instead of the trigger. ``record_dead_letter``
writes a ``dead_letter_jobs`` row (via DatabaseService.add_dead_letter_job)
including the reconstructed original MQ message, so the dead-letter retry API
can re-publish it faithfully.

Both activities here are best-effort observability/recovery signals: a
failure to record OR resolve a dead-letter row must NEVER mask the original
workflow outcome. The caller in the workflow wraps each activity call so a
failure here is logged and swallowed.
"""

import structlog
from temporalio import activity

from src.temporal.models import RecordDeadLetterInput, ResolveDeadLetterJobsInput

logger = structlog.get_logger(__name__)


@activity.defn
async def record_dead_letter(input: RecordDeadLetterInput) -> bool:
    """Insert a dead-letter row for a terminally-failed ingestion job.

    Delegates to ``DatabaseService.add_dead_letter_job`` using the shared,
    already-connected database pool (same pool used by the other ingestion
    activities). Returns True on success, False if recording was skipped or
    failed (it never raises, so it cannot mask the workflow's real error).

    Args:
        input: document/workspace/user IDs, workflow run ID, the original MQ
            message dict, the error message, and the classified error type.

    Returns:
        True if a dead-letter row was written, False otherwise.
    """
    from src.temporal.shared_services import get_db_service

    db_service = get_db_service()

    job_id = await db_service.add_dead_letter_job(
        document_id=input.document_id,
        workspace_id=input.workspace_id,
        user_id=input.user_id,
        workflow_run_id=input.workflow_run_id,
        original_message=input.original_message,
        error_message=input.error_message,
        error_type=input.error_type,
    )

    logger.info(
        "Recorded dead-letter job",
        document_id=input.document_id,
        workspace_id=input.workspace_id,
        error_type=input.error_type,
        dead_letter_job_id=job_id,
    )

    return True


@activity.defn
async def resolve_dead_letter_jobs(input: ResolveDeadLetterJobsInput) -> int:
    """Mark a document's outstanding 'retrying' dead-letter rows resolved (#249).

    Delegates to ``DatabaseService.resolve_dead_letter_jobs_for_document``
    using the shared, already-connected database pool. Called from the
    workflow's SUCCESS path once the document has genuinely finished
    processing -- see
    ``DocumentIngestionWorkflow._resolve_dead_letter_best_effort`` for why
    this closes the #249 gap (dead-letter rows previously never left
    status='retrying' after a successful retry).

    Only rows currently in status='retrying' are touched (rows still
    'pending' or already 'abandoned' are left alone) -- see the DB method's
    docstring for the full reasoning.

    Args:
        input: the document_id whose dead-letter rows to resolve.

    Returns:
        Number of dead-letter rows resolved (0 if none were 'retrying').
    """
    from src.temporal.shared_services import get_db_service

    db_service = get_db_service()

    # int(...): get_db_service() is untyped (returns Any), so mypy would
    # otherwise flag this as "Returning Any from function declared to
    # return int" -- the underlying DB call already returns a real int
    # (DatabaseService.resolve_dead_letter_jobs_for_document), this just
    # makes that concrete for the type checker at the activity boundary.
    resolved_count = int(await db_service.resolve_dead_letter_jobs_for_document(input.document_id))

    if resolved_count:
        logger.info(
            "Resolved dead-letter jobs for document",
            document_id=input.document_id,
            resolved_count=resolved_count,
        )

    return resolved_count
