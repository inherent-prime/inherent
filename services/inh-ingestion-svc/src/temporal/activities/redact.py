"""redact_turns: non-retryable credential redaction before conversation
chunking (#307).

Runs BEFORE `chunk_conversation` in the conversation-ingestion flush
pipeline (#306): conversations captured from an assistant contain
credentials by default, not by accident (API keys pasted for debugging,
connection strings, bearer tokens, private keys). Once embedded, that
material is in the vector store, in search results, and in every agent
context that retrieves it -- there is no remedy after the fact. This
activity is the one and only place that raw pre-redaction turn text is
allowed to exist in the pipeline.

Per-turn granularity (#307 design constraint)
-----------------------------------------------
A turn whose redaction pass raises is DROPPED, not the whole batch: the
batch input is a list of independent turns, so one turn's failure can never
prevent the other turns in the same flush from being redacted and returned.
The failing turn's id is both returned (`RedactTurnsOutput.dropped_turn_ids`)
and written to the `redaction_audit` table via
`DatabaseService.record_redaction_failure` -- see that method's docstring
and migration 019 for why the audit row itself can never carry raw text.

Non-retryable -- TWO independent guards
-----------------------------------------
Retrying a redaction failure risks storing the raw turn on a later attempt,
so this activity must never be retried by Temporal. Two independent guards
enforce that, deliberately redundant so a future refactor dropping ONE of
them doesn't silently reopen the retry path:

1. Any exception that escapes this activity's own control flow (a bug, not
   a per-turn detector failure -- those are caught below and never escape)
   is converted to `ApplicationError(..., non_retryable=True)` before it
   reaches Temporal.
2. The CALLER must still set `RetryPolicy(maximum_attempts=1)` on the
   `execute_activity` call. `non_retryable=True` marks this failure as one
   Temporal will not retry regardless of the caller's policy, but an
   explicit `maximum_attempts=1` is the belt to that suspenders -- it keeps
   the activity's own retry budget at exactly one attempt even if a future
   Temporal SDK change, a bug in this file, or a copy-pasted call site ever
   loosens (1).

THE SHARPEST EDGE -- read this before wiring this activity into any workflow
------------------------------------------------------------------------------
`non_retryable=True` only prevents RETRYING `redact_turns` itself. It does
**not** prevent the real bug class this issue exists to close: a downstream
activity (e.g. #306's `chunk_conversation`) re-deriving turn text from the
WORKFLOW's raw pre-redaction buffer instead of consuming THIS activity's
output (`RedactTurnsOutput.redacted_turns`). If any later step reads the
original, unredacted turns from workflow state -- because it's convenient,
because a refactor forgot, because a retry replayed the workflow and the
buffer was still sitting there -- every credential this activity redacted is
back in the pipeline as if `redact_turns` had never run. Whoever wires this
activity into #306's `ConversationMemoryWorkflow` must ensure
`chunk_conversation` reads ONLY `redact_turns`'s result -- never the
workflow's own turn buffer -- for any turn that passed through here.

Honest limits
---------------
This is best-effort pattern matching (see
src/services/redaction_patterns.py's module docstring). It will not catch
every credential shape. Do not represent this to users as a guarantee.
"""

from __future__ import annotations

import structlog
from temporalio import activity
from temporalio.exceptions import ApplicationError

from src.services.metrics import REDACTED_TURNS_DROPPED_TOTAL, REDACTIONS_TOTAL
from src.services.redaction_patterns import RedactionDetectorError, redact_text
from src.temporal.models import RedactedTurn, RedactTurnsInput, RedactTurnsOutput

logger = structlog.get_logger(__name__)


@activity.defn
async def redact_turns(input: RedactTurnsInput) -> RedactTurnsOutput:
    """Redact credentials from every turn in `input.turns`.

    Per-turn: a turn that redacts successfully is added to
    `redacted_turns`; a turn whose redaction pass raises is dropped
    (added to `dropped_turn_ids` instead) and audited via
    `DatabaseService.record_redaction_failure`. The activity itself never
    raises for a per-turn failure -- see the module docstring for why the
    batch must keep succeeding.

    Any exception that escapes the per-turn handling below (an actual bug in
    this activity's own control flow, not a detector failure) is converted
    into a non-retryable `ApplicationError` -- see the module docstring's
    "Non-retryable -- TWO independent guards" section.
    """
    try:
        return await _redact_turns_inner(input)
    except ApplicationError:
        raise  # already the right shape -- don't double-wrap
    except Exception as exc:
        # Catastrophic, unexpected failure OUTSIDE the per-turn try/except in
        # _redact_turns_inner (e.g. a bug reached here despite that guard).
        # Deliberately logs only the exception's TYPE, not str(exc) -- a bug
        # triggered by pathological turn content could otherwise leak that
        # content into this message. Non-retryable: see the module docstring
        # -- retrying redact_turns after any failure is never safe.
        logger.error(
            "redact_turns: unhandled failure, activity-level abort",
            error_type=type(exc).__name__,
        )
        raise ApplicationError(
            f"redact_turns failed: {type(exc).__name__}",
            type="RedactionCatastrophicFailure",
            non_retryable=True,
        ) from exc


async def _redact_turns_inner(input: RedactTurnsInput) -> RedactTurnsOutput:
    """Inner implementation, wrapped by `redact_turns`'s catastrophic-failure guard."""
    from src.config.settings import get_settings

    settings = get_settings()
    extra_patterns = settings.redaction_patterns_extra

    redacted_turns: list[RedactedTurn] = []
    dropped_turn_ids: list[str] = []
    batch_counts: dict[str, int] = {}

    for turn in input.turns:
        try:
            redacted_text, counts = redact_text(turn.text, extra_patterns)
        except RedactionDetectorError as exc:
            # Per-turn failure: drop THIS turn only, audit it, and keep
            # processing the rest of the batch (#307's core design
            # constraint -- see module docstring).
            dropped_turn_ids.append(turn.turn_id)
            REDACTED_TURNS_DROPPED_TOTAL.labels(detector=exc.detector).inc()
            logger.warning(
                "redact_turns: dropped turn after redaction failure",
                turn_id=turn.turn_id,
                detector=exc.detector,
                error_type=type(exc.cause).__name__,
                # No `text=turn.text` and no `error=str(exc.cause)` here --
                # see the module docstring and record_redaction_failure's
                # docstring: this is exactly the log line a leak would slip
                # through if written carelessly.
            )
            await _audit_failure(turn.turn_id, exc, input)
            continue

        redacted_turns.append(
            RedactedTurn(
                turn_id=turn.turn_id,
                text=redacted_text,
                role=turn.role,
                redaction_counts=counts,
            )
        )
        for redaction_type, n in counts.items():
            batch_counts[redaction_type] = batch_counts.get(redaction_type, 0) + n
            REDACTIONS_TOTAL.labels(redaction_type=redaction_type).inc(n)

    logger.info(
        "redact_turns: batch complete",
        turns_in=len(input.turns),
        turns_redacted=len(redacted_turns),
        turns_dropped=len(dropped_turn_ids),
        redaction_counts=batch_counts,
    )

    return RedactTurnsOutput(
        redacted_turns=redacted_turns,
        dropped_turn_ids=dropped_turn_ids,
        redaction_counts=batch_counts,
    )


async def _audit_failure(
    turn_id: str, exc: RedactionDetectorError, input: RedactTurnsInput
) -> None:
    """Write a redaction_audit row for one dropped turn -- best-effort.

    A failure to WRITE the audit row must never mask or escalate the
    original per-turn redaction failure (the turn is already correctly
    dropped and logged by the caller regardless of whether this succeeds) --
    same "best-effort, never mask the real outcome" contract as
    dead_letter.py's record_dead_letter. Swallows and logs rather than
    re-raising, so a Postgres hiccup here cannot turn a clean per-turn drop
    into an activity-level (non-retryable) abort.
    """
    from src.temporal.shared_services import get_db_service

    try:
        db = get_db_service()
        await db.record_redaction_failure(
            turn_id=turn_id,
            detector=exc.detector,
            error_type=type(exc.cause).__name__,
            error_message=str(exc.cause),
            workflow_run_id=input.workflow_run_id,
            workspace_id=input.workspace_id,
            document_id=input.document_id,
        )
    except Exception as audit_err:
        logger.warning(
            "redact_turns: failed to write redaction_audit row",
            turn_id=turn_id,
            audit_error_type=type(audit_err).__name__,
        )
