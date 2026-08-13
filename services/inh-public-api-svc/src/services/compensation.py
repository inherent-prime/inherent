"""Compensating-write retry for document and chunk state (#99 / #133).

A state write followed by a publish (or any second fallible step) needs a
compensating mark-failed path (CLAUDE.md defect prevention). #99: the
compensation itself can fail — a DB blip while MQ is already down — and the
old log-and-swallow left the row orphaned as 'pending' while the caller was
told 'failed', with nothing to find or reconcile the divergence.

Every compensation site (upload intake, REST refresh, MCP refresh) must route
through :func:`mark_document_failed_with_retry` instead of calling
``database.mark_document_failed`` bare inside an ``except`` block. Chunk
create rollback (PG insert succeeded, vector write failed) must route through
:func:`delete_chunk_with_retry` instead of a bare ``delete_document_chunk``.

The helpers retry transient failures with exponential backoff; when retries
are exhausted they make the orphan loud — a CRITICAL log plus a bump of
``document_compensation_exhausted_total`` — and report the outcome to the
caller instead of raising (the caller is already on a failure path and must
still return its error response).
"""

import asyncio

from src.services.database import DatabaseService
from src.services.metrics import record_compensation_exhausted
from src.utils import get_logger

logger = get_logger(__name__)

# 3 attempts with 0.2s/0.4s backoff rides out a transient DB blip without
# holding the already-failing request open for more than ~1s extra.
MARK_FAILED_ATTEMPTS = 3
MARK_FAILED_BACKOFF_SECONDS = 0.2


async def mark_document_failed_with_retry(
    database: DatabaseService,
    document_id: str,
    workspace_id: str,
    error_message: str,
    *,
    operation: str,
    attempts: int = MARK_FAILED_ATTEMPTS,
    backoff_seconds: float = MARK_FAILED_BACKOFF_SECONDS,
) -> bool:
    """Mark a document 'failed', retrying transient failures with backoff.

    Args:
        database: Database service that owns ``mark_document_failed``.
        document_id: Document whose row must leave 'pending'.
        workspace_id: Workspace scoping the row.
        error_message: Error recorded on the document row.
        operation: Bounded metric label naming the compensation site
            (e.g. ``upload_enqueue``, ``refresh_enqueue``).
        attempts: Total attempts before declaring exhaustion.
        backoff_seconds: Base delay, doubled after each failed attempt.

    Returns:
        True when the mark landed; False when every attempt failed. On False
        the document is orphaned as 'pending' — the CRITICAL log and the
        ``document_compensation_exhausted_total`` metric emitted here are the
        reconciliation signal operators alert on.
    """
    for attempt in range(1, attempts + 1):
        try:
            await database.mark_document_failed(document_id, workspace_id, error_message)
            return True
        except Exception as exc:
            if attempt < attempts:
                logger.warning(
                    "Compensating mark-failed write failed; retrying",
                    error=str(exc),
                    document_id=document_id,
                    operation=operation,
                    attempt=attempt,
                    attempts=attempts,
                )
                await asyncio.sleep(backoff_seconds * 2 ** (attempt - 1))
            else:
                # Exhausted: the row stays 'pending' while the caller reports
                # failure. This divergence must never be silent (#99).
                logger.critical(
                    "Compensation exhausted — document orphaned as 'pending'; "
                    "manual reconciliation required",
                    error=str(exc),
                    document_id=document_id,
                    workspace_id=workspace_id,
                    operation=operation,
                    attempts=attempts,
                )
                record_compensation_exhausted(operation)
    return False


async def delete_chunk_with_retry(
    database: DatabaseService,
    document_id: str,
    workspace_id: str,
    chunk_index: int,
    *,
    operation: str,
    attempts: int = MARK_FAILED_ATTEMPTS,
    backoff_seconds: float = MARK_FAILED_BACKOFF_SECONDS,
) -> bool:
    """Delete a single chunk row, retrying transient failures with backoff (#133).

    Used when a chunk Create's PG insert succeeded but the Weaviate upsert
    failed: compensate by removing the orphan PG row so stores do not diverge.
    ``None`` from ``delete_document_chunk`` (row already gone) counts as
    success — compensation is idempotent.

    Args:
        database: Database service that owns ``delete_document_chunk``.
        document_id: Parent document id.
        workspace_id: Workspace scoping the row.
        chunk_index: Stable index of the chunk to remove (Option A gaps OK).
        operation: Metric label (e.g. ``chunk_create_vector_rollback``).
        attempts: Total attempts before declaring exhaustion.
        backoff_seconds: Base delay, doubled after each failed attempt.

    Returns:
        True when the delete landed or the row was already absent; False when
        every attempt raised. On False the PG row may still exist without a
        vector — CRITICAL log + exhaustion metric are the operator signal.
    """
    for attempt in range(1, attempts + 1):
        try:
            await database.delete_document_chunk(document_id, workspace_id, chunk_index)
            return True
        except Exception as exc:
            if attempt < attempts:
                logger.warning(
                    "Compensating chunk delete failed; retrying",
                    error=str(exc),
                    document_id=document_id,
                    chunk_index=chunk_index,
                    operation=operation,
                    attempt=attempt,
                    attempts=attempts,
                )
                await asyncio.sleep(backoff_seconds * 2 ** (attempt - 1))
            else:
                logger.critical(
                    "Compensation exhausted — chunk PG row may lack a vector; "
                    "manual reconciliation required",
                    error=str(exc),
                    document_id=document_id,
                    workspace_id=workspace_id,
                    chunk_index=chunk_index,
                    operation=operation,
                    attempts=attempts,
                )
                record_compensation_exhausted(operation)
    return False


async def restore_chunk_content_with_retry(
    database: DatabaseService,
    document_id: str,
    workspace_id: str,
    chunk_index: int,
    content: str,
    *,
    operation: str,
    only_if_content_hash: str | None = None,
    attempts: int = MARK_FAILED_ATTEMPTS,
    backoff_seconds: float = MARK_FAILED_BACKOFF_SECONDS,
) -> bool:
    """Restore a chunk's prior PG content after a failed vector update (#133).

    Same retry + loud-exhaustion pattern as :func:`delete_chunk_with_retry`.
    ``None`` from ``update_document_chunk`` (row gone, or CAS miss when
    ``only_if_content_hash`` no longer matches) counts as success — a
    concurrent edit won, so we must not clobber it.
    """
    for attempt in range(1, attempts + 1):
        try:
            await database.update_document_chunk(
                document_id,
                workspace_id,
                chunk_index,
                content,
                only_if_content_hash=only_if_content_hash,
            )
            return True
        except Exception as exc:
            if attempt < attempts:
                logger.warning(
                    "Compensating chunk content restore failed; retrying",
                    error=str(exc),
                    document_id=document_id,
                    chunk_index=chunk_index,
                    operation=operation,
                    attempt=attempt,
                    attempts=attempts,
                )
                await asyncio.sleep(backoff_seconds * 2 ** (attempt - 1))
            else:
                logger.critical(
                    "Compensation exhausted — chunk PG content may diverge from "
                    "vector; manual reconciliation required",
                    error=str(exc),
                    document_id=document_id,
                    workspace_id=workspace_id,
                    chunk_index=chunk_index,
                    operation=operation,
                    attempts=attempts,
                )
                record_compensation_exhausted(operation)
    return False
