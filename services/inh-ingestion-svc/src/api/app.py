"""FastAPI application for the standalone ingestion service.

Provides HTTP endpoints for triggering and monitoring Temporal document
ingestion workflows without requiring Google Cloud Pub/Sub infrastructure.

Usage:
    SERVICE_MODE=standalone INGESTION_API_KEY=<secret> python -m src.main
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Literal

import structlog
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from temporalio.client import Client, WorkflowFailureError
from temporalio.exceptions import TerminatedError, WorkflowAlreadyStartedError
from temporalio.service import RPCError

from src.api.auth import verify_api_key
from src.api.ownership import (
    require_storage_path_workspace_prefix,
    require_workspace_id,
    resolve_owned_dead_letter_job,
    resolve_owned_document,
)
from src.config.settings import Settings
from src.services.metrics import get_metrics
from src.temporal.models import (
    ChunkEditInput,
    ChunkEditResult,
    DocumentIngestionInput,
    WorkflowResult,
)
from src.temporal.trigger import build_ingestion_source_memo
from src.temporal.worker import TemporalWorkerManager
from src.temporal.workflows import ChunkEditWorkflow, DocumentIngestionWorkflow

logger = structlog.get_logger(__name__)

# #178: POST /ingest is inherently a direct/manual trigger -- it has no
# connector to attribute an ingestion to, unlike the MQ-driven paths in
# trigger.py that carry a real source/connection_id/sync_id from the
# upstream DocumentUploadMessage. Hardcoding this label (rather than adding
# unused source/connection_id/sync_id fields to IngestRequest -- there is no
# connector-provided value to put in them on this path) is the design
# decision #178 asked for; see build_ingestion_source_memo's docstring for
# why the memo SHAPE itself still comes from one shared function.
_DIRECT_API_INGESTION_SOURCE = "api-direct"


# =============================================================================
# Request / Response Models
# =============================================================================


class IngestRequest(BaseModel):
    """Request body for triggering document ingestion."""

    document_id: str = Field(..., description="Unique document identifier")
    # Security (#210): min_length=1 is layer 1 of the falsy-vs-absent fix --
    # Field(...) alone enforces PRESENCE, not non-emptiness, so a bare
    # `"workspace_id": ""` would otherwise slip past this model entirely and
    # reach require_storage_path_workspace_prefix's internal
    # require_workspace_id call as the FIRST line of defense instead of a
    # cheap 422 at the boundary. Layer 2 (require_workspace_id, which also
    # rejects whitespace-only) and layer 3 (require_storage_path_workspace_prefix
    # itself raising, never silently widening) are in the route body below.
    workspace_id: str = Field(..., min_length=1, description="Workspace identifier")
    user_id: str = Field(..., description="User who uploaded the document")
    filename: str = Field(..., description="Storage filename (generated)")
    original_filename: str = Field(..., description="Original filename from upload")
    content_type: str = Field(..., description="MIME type of the document")
    size_bytes: int = Field(..., gt=0, description="File size in bytes")
    storage_backend: Literal["local", "s3", "gcs", "azure"] = Field(
        ..., description="Storage backend"
    )
    storage_path: str = Field(..., min_length=1, description="Path to file in storage")
    storage_bucket: str | None = Field(None, description="Storage bucket name")
    storage_url: str | None = Field(None, description="Direct URL to the file")

    model_config = {
        "json_schema_extra": {
            "example": {
                "document_id": "507f1f77bcf86cd799439011",
                "workspace_id": "507f1f77bcf86cd799439012",
                "user_id": "507f1f77bcf86cd799439013",
                "filename": "1234567890-abc12345-document.pdf",
                "original_filename": "document.pdf",
                "content_type": "application/pdf",
                "size_bytes": 102400,
                "storage_backend": "local",
                # Security (#210): storage_path MUST be prefixed by
                # workspace_id above -- this example was previously
                # "workspaces/ws123/..." next to workspace_id
                # "507f1f77bcf86cd799439012", a mismatch that 403s under the
                # #210 fix. Caught by attacker-persona acceptance testing:
                # the auto-generated /docs "Try it out" prefill must itself
                # pass the check it's demonstrating, or a reader's first
                # experience of this endpoint is the new error.
                "storage_path": (
                    "workspaces/507f1f77bcf86cd799439012/1234567890-abc12345-document.pdf"
                ),
            }
        }
    }


class IngestAcceptedResponse(BaseModel):
    """Returned when a workflow is started asynchronously (HTTP 202)."""

    workflow_id: str
    document_id: str
    status: Literal["started", "already_running", "superseded_by_newer_request"] = "started"


class IngestResultResponse(BaseModel):
    """Returned when wait=true and the workflow runs to completion (HTTP 200)."""

    workflow_id: str
    document_id: str
    success: bool
    chunks_created: int = 0
    processing_time_ms: int = 0
    error: str | None = None


class WorkflowStatusResponse(BaseModel):
    """Real-time status of a running or completed workflow."""

    workflow_id: str
    document_id: str
    step: str
    progress: int
    chunks_created: int


class ChunkEditRequest(BaseModel):
    """Request body for editing a chunk's content."""

    content: str = Field(..., min_length=1, description="New chunk content")


class ChunkEditResponse(BaseModel):
    """Response after successfully editing a chunk."""

    document_id: str
    chunk_index: int
    updated: bool


class DeleteDocumentResponse(BaseModel):
    """Response after deleting a document."""

    deleted: bool
    document_id: str
    weaviate_cleaned: bool


class HealthResponse(BaseModel):
    """Health check response."""

    status: Literal["healthy", "degraded"]
    temporal_worker: bool
    version: str


# =============================================================================
# Application Factory
# =============================================================================


def create_app(settings: Settings) -> FastAPI:
    """Create the FastAPI application with embedded Temporal worker.

    The lifespan context manager starts the Temporal worker on startup
    and stops it on shutdown. The Temporal client is stored in app.state
    so route handlers can start workflows.

    Args:
        settings: Application settings.

    Returns:
        Configured FastAPI application.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # --- Startup ---
        manager = TemporalWorkerManager(settings)
        await manager.start()

        app.state.temporal_client = await manager.get_client()
        app.state.worker_manager = manager
        app.state.settings = settings

        # Expose the workflow trigger so the dead-letter retry endpoint can
        # re-publish/restart failed jobs (#8). In worker mode this returns the
        # already-initialized global trigger; in api-only mode it self-initializes
        # on first use.
        from src.temporal.shared_services import get_db_service
        from src.temporal.trigger import get_workflow_trigger

        # Wire db_service so poison-message dead-lettering works (#6). In worker
        # mode main.py already wired it; this backfills it in api-only mode.
        # db_service is optional for the trigger, so a bootstrap failure here
        # must not block app startup — degrade to no dead-lettering.
        try:
            db_service = get_db_service()
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("db_service unavailable; trigger dead-lettering disabled", error=str(e))
            db_service = None
        app.state.trigger = get_workflow_trigger(settings, db_service=db_service)

        logger.info(
            "Standalone API ready",
            task_queue=settings.temporal_task_queue,
            temporal_host=settings.temporal_host,
        )

        yield

        # --- Shutdown ---
        await manager.stop()
        logger.info("Standalone API shut down")

    app = FastAPI(
        title="Inherent Ingestion Service",
        description="Standalone HTTP API for triggering document ingestion via Temporal.",
        version="0.6.0",
        lifespan=lifespan,
    )

    # ------------------------------------------------------------------
    # Public routes (no auth)
    # ------------------------------------------------------------------

    @app.get("/metrics", tags=["ops"])
    async def metrics():
        return Response(content=get_metrics(), media_type="text/plain; charset=utf-8")

    @app.get("/health", response_model=HealthResponse, tags=["ops"])
    async def health(request: Request) -> HealthResponse:
        manager: TemporalWorkerManager = request.app.state.worker_manager
        return HealthResponse(
            status="healthy" if manager.is_running else "degraded",
            temporal_worker=manager.is_running,
            version="0.6.0",
        )

    # ------------------------------------------------------------------
    # Protected routes (require API key)
    # ------------------------------------------------------------------

    router = APIRouter(
        prefix="/ingest",
        tags=["ingestion"],
        dependencies=[Depends(verify_api_key)],
    )

    @router.post(
        "",
        status_code=202,
        response_model=IngestAcceptedResponse,
        responses={
            200: {"model": IngestResultResponse, "description": "Completed (wait=true)"},
            403: {
                "description": (
                    "storage_path does not belong to the claimed workspace_id (#210). "
                    "NOT a tenant-entitlement check -- see IngestRequest.workspace_id's "
                    "field description and CHANGELOG.md for the limitation this leaves."
                )
            },
            409: {
                "description": (
                    "Workflow already running for this document, OR (wait=true only) "
                    "this run was terminated mid-wait by a newer concurrent request for "
                    "the same document (#110) -- check GET /ingest/{document_id}/status "
                    "or the document's own status endpoint for the outcome that won."
                )
            },
        },
    )
    async def trigger_ingestion(
        body: IngestRequest,
        request: Request,
        wait: bool = Query(False, description="Block until the workflow completes"),
    ):
        """Start a document ingestion workflow.

        By default returns **202 Accepted** immediately with the workflow ID.
        Pass `?wait=true` to block until the workflow finishes and receive
        the full result as a **200 OK** response.

        Security (#210): before this fix, ``storage_path`` and
        ``workspace_id`` were both caller-supplied with no check that either
        one belonged to the other -- a caller holding the shared
        ``INGESTION_API_KEY`` could pair a VICTIM's ``storage_path`` with
        their OWN ``workspace_id`` and the pipeline would fetch, chunk,
        embed, and file the victim's content into the attacker's own
        tenant, readable afterwards through the attacker's own legitimate
        key. There is no existing PostgreSQL row to resolve this against
        (this endpoint CREATES one) -- see
        ``src.api.ownership.require_storage_path_workspace_prefix`` for the
        prefix check this route now runs first, and its docstring for what
        this does NOT prove (``INGESTION_API_KEY`` has no key->workspace
        binding, so this is workspace<->path CONSISTENCY, not caller
        entitlement -- see #177, and CHANGELOG.md).
        """
        client: Client = request.app.state.temporal_client
        settings: Settings = request.app.state.settings
        task_queue: str = settings.temporal_task_queue

        # Security (#210): verify storage_path is prefixed by the CLAIMED
        # workspace_id before the pipeline ever reads it -- must run BEFORE
        # DocumentIngestionInput/start_workflow, not after, since the whole
        # point is to refuse dispatching the workflow at all on a mismatch.
        # Returns the stripped/validated workspace_id; every field below
        # uses THIS value, never body.workspace_id directly, matching the
        # "never forward the raw caller value" pattern the other routes in
        # this module already follow (see ownership.py).
        workspace_id = require_storage_path_workspace_prefix(body.storage_path, body.workspace_id)

        workflow_input = DocumentIngestionInput(
            document_id=body.document_id,
            workspace_id=workspace_id,
            user_id=body.user_id,
            filename=body.filename,
            original_filename=body.original_filename,
            content_type=body.content_type,
            size_bytes=body.size_bytes,
            storage_backend=body.storage_backend,
            storage_path=body.storage_path,
            storage_bucket=body.storage_bucket,
            storage_url=body.storage_url,
            timestamp=datetime.now(UTC).isoformat(),
        )

        workflow_id = f"ingest-{body.document_id}"

        try:
            handle = await client.start_workflow(
                DocumentIngestionWorkflow.run,
                workflow_input,
                id=workflow_id,
                task_queue=task_queue,
                # #178: this direct/manual trigger path had no memo at all
                # pre-fix, unlike the two MQ-driven start sites #141 covered
                # -- see _DIRECT_API_INGESTION_SOURCE's module-level comment
                # for why the source is hardcoded rather than derived.
                memo=build_ingestion_source_memo(source=_DIRECT_API_INGESTION_SOURCE),
            )
        except WorkflowAlreadyStartedError:
            logger.info("Workflow already running", workflow_id=workflow_id)
            return JSONResponse(
                status_code=409,
                content=IngestAcceptedResponse(
                    workflow_id=workflow_id,
                    document_id=body.document_id,
                    status="already_running",
                ).model_dump(),
            )
        except RPCError as e:
            logger.error("Temporal unavailable", error=str(e))
            raise HTTPException(status_code=503, detail="Temporal service unavailable") from e

        logger.info(
            "Workflow started",
            workflow_id=workflow_id,
            document_id=body.document_id,
            wait=wait,
        )

        if wait:
            # #110 blocker 4: this run can now be terminated out from under us
            # by an UNRELATED concurrent MQ refresh/re-index for the same
            # document_id (trigger_workflow_async's supersede_running=True
            # default, see src/temporal/trigger.py) -- Temporal workflow ids
            # are global, not scoped to how the run was started. Pre-#110 this
            # path was safe uncaught: the workflow always caught its own
            # exceptions and returned WorkflowResult(success=False, ...), so
            # handle.result() effectively never raised. Post-#110 it can raise
            # WorkflowFailureError(cause=TerminatedError) here, which without
            # this except would surface as an unhandled 500. Report it as a
            # clear 409 instead -- the caller's own request is not what
            # failed; a newer one for the same document won the race.
            try:
                result: WorkflowResult = await handle.result()
            except WorkflowFailureError as e:
                if isinstance(e.cause, TerminatedError):
                    logger.info(
                        "Ingestion terminated by a newer request for this document",
                        workflow_id=workflow_id,
                        document_id=body.document_id,
                    )
                    return JSONResponse(
                        status_code=409,
                        content=IngestAcceptedResponse(
                            workflow_id=workflow_id,
                            document_id=body.document_id,
                            status="superseded_by_newer_request",
                        ).model_dump(),
                    )
                # #230: DocumentIngestionWorkflow raises ApplicationError
                # type=DocumentIngestionFailed after marking the doc failed,
                # dead-lettering, and publishing document.failed — so Temporal
                # close status is Failed (monitorable) while wait=true callers
                # still get a structured success=False body (not a 500).
                from temporalio.exceptions import ApplicationError

                from src.temporal.document_failure import DOCUMENT_INGESTION_FAILED_TYPE

                if (
                    isinstance(e.cause, ApplicationError)
                    and e.cause.type == DOCUMENT_INGESTION_FAILED_TYPE
                ):
                    err_msg = e.cause.message or str(e.cause)
                    return JSONResponse(
                        status_code=200,
                        content=IngestResultResponse(
                            workflow_id=workflow_id,
                            document_id=body.document_id,
                            success=False,
                            chunks_created=0,
                            processing_time_ms=0,
                            error=err_msg,
                        ).model_dump(),
                    )
                # Cancellation / timeout / other unexpected close statuses.
                logger.error(
                    "Unexpected workflow failure while waiting for result",
                    workflow_id=workflow_id,
                    document_id=body.document_id,
                    error=str(e.cause),
                )
                raise HTTPException(
                    status_code=500, detail=f"Ingestion workflow failed: {e.cause}"
                ) from e

            return JSONResponse(
                status_code=200,
                content=IngestResultResponse(
                    workflow_id=workflow_id,
                    document_id=result.document_id,
                    success=result.success,
                    chunks_created=result.chunks_created,
                    processing_time_ms=result.processing_time_ms,
                    error=result.error,
                ).model_dump(),
            )

        return IngestAcceptedResponse(
            workflow_id=workflow_id,
            document_id=body.document_id,
            status="started",
        )

    @router.get(
        "/{document_id}/status",
        response_model=WorkflowStatusResponse,
        responses={404: {"description": "Document not found in the given workspace"}},
    )
    async def get_ingestion_status(
        document_id: str,
        request: Request,
        workspace_id: str = Query(
            ..., min_length=1, description="Workspace that must own document_id"
        ),
    ):
        """Query the real-time progress of a running ingestion workflow.

        Security (#177): gated only by ``verify_api_key`` before this fix,
        with no check that the caller's claimed workspace actually owns
        ``document_id`` -- any caller holding the one shared
        ``INGESTION_API_KEY`` could poll any other tenant's ingestion
        progress (step, percent complete, chunk counts) by guessing/
        enumerating document ids. Mirrors #134's guard: resolve
        ``document_id`` against PostgreSQL first and 404 unless its stored
        ``workspace_id`` matches, same response for "no such document" and
        "wrong workspace" so existence doesn't leak.

        Note: ``processed_documents`` is claimed by the workflow's own first
        activity (``create_pending_document``), not by this endpoint or by
        ``POST /ingest`` itself -- so there is an unavoidable, normally
        sub-second window right after a fresh ``POST /ingest`` where this
        endpoint can 404 even though the workflow really did start. That is
        the accepted cost of not being able to answer "who owns
        document_id" from Temporal alone.
        """
        from src.temporal import shared_services

        client: Client = request.app.state.temporal_client
        workflow_id = f"ingest-{document_id}"

        db_svc = shared_services.get_db_service()
        await resolve_owned_document(db_svc, document_id, workspace_id)

        try:
            handle = client.get_workflow_handle(workflow_id)
            status: dict = await handle.query(DocumentIngestionWorkflow.get_status)
        except RPCError as e:
            logger.warning("Status query failed", workflow_id=workflow_id, error=str(e))
            raise HTTPException(
                status_code=404,
                detail=f"Workflow {workflow_id} not found or not queryable.",
            ) from e

        return WorkflowStatusResponse(
            workflow_id=workflow_id,
            document_id=document_id,
            step=status.get("step", "unknown"),
            progress=status.get("progress", 0),
            chunks_created=status.get("chunks_created", 0),
        )

    app.include_router(router)

    # ------------------------------------------------------------------
    # Chunk edit route (protected)
    # ------------------------------------------------------------------

    chunks_router = APIRouter(
        prefix="/chunks",
        tags=["chunks"],
        dependencies=[Depends(verify_api_key)],
    )

    @chunks_router.patch(
        "/{document_id}/{chunk_index}",
        response_model=ChunkEditResponse,
        responses={404: {"description": "Document not found in the given workspace"}},
    )
    async def edit_chunk(
        document_id: str,
        chunk_index: int,
        body: ChunkEditRequest,
        request: Request,
        workspace_id: str = Query(
            ..., min_length=1, description="Workspace that must own document_id"
        ),
    ):
        """Edit a chunk via Temporal workflow (updates PG + re-embeds in Weaviate).

        Security (#134): before this fix, ChunkEditInput left workspace_id/
        user_id unset, so the Weaviate write derived its collection/tenant
        from "" -- no tenant scope at all. We now resolve document_id
        against PostgreSQL and 404 unless its stored workspace_id equals the
        caller's claimed workspace_id, then forward only the *resolved*
        workspace_id/user_id (never caller-supplied) into ChunkEditInput, so
        a self-consistent (document_id, workspace_id) pair always lands the
        Weaviate write in the document's real tenant instead of "".

        This is workspace<->document CONSISTENCY, not caller<->workspace
        ENTITLEMENT -- narrower than what it may look like at a glance.
        verify_api_key is one shared secret with no key->workspace binding
        (unlike the public API's resolve_workspace_read, which validates
        that the calling API key's owner is actually entitled to the
        workspace before ever looking at a document). Here, workspace_id
        stays entirely caller-asserted: this check only rejects a caller
        that gets the pairing wrong, not one that already knows a valid
        (document_id, workspace_id) pair for a workspace it doesn't own --
        e.g. by reading one out of GET /dead-letter, which used to return
        rows across all workspaces. #177 closed that specific hole (GET
        /dead-letter now requires and enforces workspace_id, and the
        single-job dead-letter routes gained the same ownership guard this
        endpoint uses), but this endpoint's own check remains consistency-
        only, not caller entitlement -- see src/api/ownership.py's module
        docstring for the full picture.
        """
        from src.temporal import shared_services

        client: Client = request.app.state.temporal_client
        settings: Settings = request.app.state.settings

        db_svc = shared_services.get_db_service()
        # OWNERSHIP GUARD -- must run, and must keep returning exactly the
        # same 404 for both cases (see resolve_owned_document's docstring),
        # before ANY other check on `document` (including the chunk_count
        # check right below). Do not reorder the chunk_count check above
        # this one: it also 404s and it also reads from `document`, so
        # swapping the order would let an attacker distinguish "wrong
        # workspace" from "chunk_index out of range" for a document it
        # doesn't own -- reintroducing the #134 existence leak this guard
        # exists to close.
        document = await resolve_owned_document(db_svc, document_id, workspace_id)

        # Reject an out-of-range chunk_index before doing any more work
        # (#134 follow-up item 8): get_document_status already returned
        # chunk_count for free, so this costs zero extra queries, and it
        # saves a wasted embed_text round-trip (and, pre-the-#137-fix, a
        # confusingly "successful" no-op) for a chunk that was never going
        # to exist. NOTE: chunk_count is nullable (Column default=0, but the
        # column itself allows NULL) and is legitimately 0 for a document
        # that's still `pending`/`processing` -- every chunk_index 404s in
        # that case, which is CORRECT (there is nothing to edit yet), not a
        # symptom of the ownership guard above misfiring. This check only
        # runs once ownership is already proven, so it is a distinct 404
        # from the one above, not a workspace-scoping bug.
        chunk_count = document.get("chunk_count") or 0
        if chunk_index < 0 or chunk_index >= chunk_count:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"Chunk {chunk_index} not found for document {document_id} "
                    f"(document has {chunk_count} chunks)."
                ),
            )

        workflow_input = ChunkEditInput(
            document_id=document_id,
            chunk_index=chunk_index,
            content=body.content,
            workspace_id=document["workspace_id"],
            user_id=document["user_id"],
        )

        workflow_id = f"chunk-edit-{document_id}-{chunk_index}"

        try:
            handle = await client.start_workflow(
                ChunkEditWorkflow.run,
                workflow_input,
                id=workflow_id,
                task_queue=settings.temporal_task_queue,
            )
            result: ChunkEditResult = await handle.result()
        except WorkflowAlreadyStartedError:
            raise HTTPException(
                status_code=409,
                detail=f"Edit already in progress for chunk {chunk_index}.",
            )
        except RPCError as e:
            logger.error("Temporal unavailable for chunk edit", error=str(e))
            raise HTTPException(status_code=503, detail="Temporal service unavailable") from e

        if not result.success:
            raise HTTPException(status_code=500, detail=result.error or "Chunk edit failed")

        return ChunkEditResponse(
            document_id=document_id,
            chunk_index=chunk_index,
            updated=True,
        )

    app.include_router(chunks_router)

    # ------------------------------------------------------------------
    # Document delete route (protected)
    # ------------------------------------------------------------------

    documents_router = APIRouter(
        prefix="/documents",
        tags=["documents"],
        dependencies=[Depends(verify_api_key)],
    )

    @documents_router.delete(
        "/{document_id}",
        response_model=DeleteDocumentResponse,
        responses={404: {"description": "Document not found in the given workspace"}},
    )
    async def delete_document(
        document_id: str,
        request: Request,
        workspace_id: str = Query(
            ..., min_length=1, description="Workspace that must own document_id"
        ),
        user_id: str = Query(
            ...,
            description=(
                "Ignored for authorization (kept for backward-compatible request "
                "shape) -- the resolved document's own user_id is always used."
            ),
        ),
    ):
        """Delete a document from PostgreSQL and its chunks from Weaviate.

        Security (#175): same missing-ownership-check pattern as #134.
        Before this fix, workspace_id/user_id were caller-supplied and used
        UNVERIFIED: the Weaviate cleanup call picked its collection/tenant
        from them directly, and the PostgreSQL delete matched on
        document_id alone -- a caller that knew (or guessed/enumerated) a
        document_id could delete it from PostgreSQL and attempt Weaviate
        cleanup under ANY workspace_id/user_id it supplied, including a
        foreign tenant's. Mirrors #134's guard: document_id is resolved
        against PostgreSQL and 404s unless its stored workspace_id matches
        the caller's claim (same response for "no such document" and
        "wrong workspace", so existence doesn't leak). Only the *resolved*
        workspace_id/user_id -- never the caller-supplied ones -- drive the
        Weaviate tenant lookup and scope the PostgreSQL delete's own WHERE
        clause.

        Weaviate cleanup is best-effort: if it fails, the PG delete still
        succeeds and the response indicates ``weaviate_cleaned=false``.
        """
        from src.temporal import shared_services

        db_svc = shared_services.get_db_service()
        document = await resolve_owned_document(db_svc, document_id, workspace_id)
        resolved_workspace_id: str = document["workspace_id"]
        resolved_user_id: str = document["user_id"]

        # --- Weaviate cleanup (best-effort, before PG delete) ---
        weaviate_cleaned = False
        weaviate_svc = shared_services.get_weaviate_service()
        if weaviate_svc is not None:
            weaviate_cleaned, _ = await weaviate_svc.delete_document_chunks_graceful(
                workspace_id=resolved_workspace_id,
                document_id=document_id,
                user_id=resolved_user_id,
            )
        else:
            logger.warning(
                "Weaviate unavailable, skipping chunk cleanup",
                document_id=document_id,
            )

        # --- PostgreSQL delete ---
        # Scoped to resolved_workspace_id as defense-in-depth against a
        # TOCTOU race between the ownership check above and this delete
        # (see DatabaseService.delete_document's docstring) -- the normal
        # not-found/wrong-workspace case is already caught by
        # resolve_owned_document above, so `not deleted` here means the
        # row vanished in between, not a routine miss.
        deleted = await db_svc.delete_document(document_id, workspace_id=resolved_workspace_id)

        if not deleted:
            raise HTTPException(
                status_code=404,
                detail=f"Document {document_id} not found in PostgreSQL.",
            )

        logger.info(
            "Document deleted",
            document_id=document_id,
            workspace_id=resolved_workspace_id,
            user_id=resolved_user_id,
            weaviate_cleaned=weaviate_cleaned,
        )

        return DeleteDocumentResponse(
            deleted=True,
            document_id=document_id,
            weaviate_cleaned=weaviate_cleaned,
        )

    app.include_router(documents_router)

    # ------------------------------------------------------------------
    # Lineage route (protected)
    # ------------------------------------------------------------------

    lineage_router = APIRouter(
        prefix="/lineage",
        tags=["lineage"],
        dependencies=[Depends(verify_api_key)],
    )

    @lineage_router.get(
        "/{document_id}",
        responses={404: {"description": "Document not found in the given workspace"}},
    )
    async def get_lineage(
        document_id: str,
        workspace_id: str = Query(
            ..., min_length=1, description="Workspace that must own document_id"
        ),
    ):
        """Get data lineage (ingestion events) for a document.

        Returns an ordered list of pipeline step events showing what
        happened during ingestion of the given document.

        Security (#177): gated only by ``verify_api_key`` before this fix,
        with no check that the caller's claimed workspace actually owns
        ``document_id`` -- any caller holding the shared
        ``INGESTION_API_KEY`` could read any other tenant's ingestion
        pipeline events (including error messages, which can carry
        sensitive detail). Mirrors #134's guard: see
        ``src.api.ownership.resolve_owned_document``.
        """
        from src.temporal import shared_services

        db_svc = shared_services.get_db_service()
        await resolve_owned_document(db_svc, document_id, workspace_id)

        events = await db_svc.get_ingestion_events(document_id)

        # Convert datetime objects to ISO strings for JSON serialization
        serialized_events = []
        for event in events:
            serialized = {}
            for key, value in event.items():
                if hasattr(value, "isoformat"):
                    serialized[key] = value.isoformat()
                else:
                    serialized[key] = value
            serialized_events.append(serialized)

        return {"document_id": document_id, "events": serialized_events}

    app.include_router(lineage_router)

    # ------------------------------------------------------------------
    # Dead-letter routes (protected)
    # ------------------------------------------------------------------

    dl_router = APIRouter(
        prefix="/dead-letter",
        tags=["dead-letter"],
        dependencies=[Depends(verify_api_key)],
    )

    @dl_router.get("")
    async def list_dead_letter_jobs(
        workspace_id: str = Query(
            ..., min_length=1, description="Workspace to list dead-letter jobs for (required, #177)"
        ),
        status: str | None = Query("pending", min_length=1),
        limit: int = Query(50, ge=1, le=200),
    ):
        """List dead-letter jobs, scoped to a workspace.

        Security (#177): ``workspace_id`` was an OPTIONAL filter -- omitting
        it returned dead-letter rows across EVERY workspace, each carrying a
        genuine ``(document_id, workspace_id, user_id)`` triple. That is the
        sharpest edge in the #177 escalation chain: a caller holding only
        the shared ``INGESTION_API_KEY`` could harvest a real cross-tenant
        pair here, then present it to ``PATCH /chunks/{document_id}/{chunk_index}``
        -- #134's ownership guard checks (document_id, workspace_id)
        CONSISTENCY, which this harvested pair genuinely satisfies, so it
        would pass.

        ``workspace_id`` is now REQUIRED at the FastAPI layer (``min_length=1``
        rejects a fully-empty ``?workspace_id=``), boundary-validated AGAIN
        via ``require_workspace_id`` (rejects a whitespace-only value like
        ``?workspace_id=%20``, which ``min_length=1`` alone does not catch),
        and REQUIRED (not optional, no falsy-skips-the-filter path) in
        ``DatabaseService.get_dead_letter_jobs`` itself. Post-#177-review
        finding: an adversarial pass proved the FIRST version of this fix
        was bypassable -- ``Query(...)`` alone only enforces PRESENCE of the
        query param, not non-emptiness, so ``?workspace_id=`` (present,
        empty) passed FastAPI validation, then hit
        ``get_dead_letter_jobs``'s old ``if workspace_id:`` guard (falsy for
        ``""``), which silently skipped the WHERE clause and returned every
        workspace's rows -- the exact cross-tenant harvest this endpoint
        exists to prevent. All three layers now have to independently agree
        the value is non-blank before this endpoint can be the source of a
        genuine cross-tenant pair.
        """
        from src.temporal import shared_services

        workspace_id = require_workspace_id(workspace_id)

        db_svc = shared_services.get_db_service()
        jobs = await db_svc.get_dead_letter_jobs(
            workspace_id=workspace_id,
            status=status,
            limit=limit,
        )

        serialized = []
        for job in jobs:
            row = {}
            for key, value in job.items():
                if hasattr(value, "isoformat"):
                    row[key] = value.isoformat()
                else:
                    row[key] = value
            serialized.append(row)

        return {"jobs": serialized, "total": len(serialized)}

    @dl_router.get(
        "/{job_id}",
        responses={404: {"description": "Job not found in the given workspace"}},
    )
    async def get_dead_letter_job(
        job_id: int,
        workspace_id: str = Query(..., min_length=1, description="Workspace that must own job_id"),
    ):
        """Get a single dead-letter job by ID.

        Security (#177): gated only by ``verify_api_key`` before this fix,
        with no check that the caller's claimed workspace actually owns
        ``job_id`` -- any caller holding the shared ``INGESTION_API_KEY``
        could enumerate small integer job ids and read any tenant's
        dead-letter row (including its ``original_message`` payload).
        Mirrors #134's guard, applied to ``dead_letter_jobs``: see
        ``src.api.ownership.resolve_owned_dead_letter_job``.
        """
        from src.temporal import shared_services

        db_svc = shared_services.get_db_service()
        job = await resolve_owned_dead_letter_job(db_svc, job_id, workspace_id)

        row = {}
        for key, value in job.items():
            if hasattr(value, "isoformat"):
                row[key] = value.isoformat()
            else:
                row[key] = value
        return row

    @dl_router.post(
        "/{job_id}/retry",
        responses={404: {"description": "Job not found in the given workspace"}},
    )
    async def retry_dead_letter_job(
        job_id: int,
        request: Request,
        workspace_id: str = Query(..., min_length=1, description="Workspace that must own job_id"),
    ):
        """Retry a dead-letter job by re-publishing its original message.

        Security (#177): a write, gated only by ``verify_api_key`` before
        this fix -- any caller holding the shared ``INGESTION_API_KEY``
        could re-trigger ingestion for any tenant's failed job by
        enumerating job ids, with no check it owned the job. Mirrors #134's
        guard: see ``src.api.ownership.resolve_owned_dead_letter_job``.
        """
        from src.temporal import shared_services

        db_svc = shared_services.get_db_service()
        job = await resolve_owned_dead_letter_job(db_svc, job_id, workspace_id)

        if job.get("status") not in ("pending", "retrying"):
            raise HTTPException(
                status_code=409,
                detail=f"Job {job_id} has status '{job.get('status')}', cannot retry",
            )

        # Increment retry count
        await db_svc.increment_dead_letter_retry(job_id)

        # Re-trigger workflow. supersede_running=False (#110 blocker 3): this
        # replays a POTENTIALLY STALE payload (whatever failed and got
        # dead-lettered, possibly long ago). If a healthy, newer run for the
        # same document_id is meanwhile in flight (e.g. the user re-uploaded
        # corrected content after the original failure), superseding it would
        # silently terminate that newer run and overwrite it with this old
        # payload. Keeping the default (raise-on-collision) behavior here
        # means that case surfaces as the 500 below instead.
        original_message = job.get("original_message", {})
        trigger = request.app.state.trigger
        try:
            workflow_id = await trigger.trigger_workflow_async(
                original_message, supersede_running=False
            )
            return {"retried": True, "job_id": job_id, "new_workflow_id": workflow_id}
        except Exception as e:
            # Reset status back to pending on failure
            await db_svc.update_dead_letter_status(job_id, "pending")
            raise HTTPException(status_code=500, detail=f"Retry failed: {e}") from e

    @dl_router.post(
        "/{job_id}/abandon",
        responses={404: {"description": "Job not found in the given workspace"}},
    )
    async def abandon_dead_letter_job(
        job_id: int,
        workspace_id: str = Query(..., min_length=1, description="Workspace that must own job_id"),
    ):
        """Mark a dead-letter job as permanently abandoned.

        Security (#177): a write, gated only by ``verify_api_key`` before
        this fix -- any caller holding the shared ``INGESTION_API_KEY``
        could abandon any tenant's failed job (silently suppressing its
        recovery) by enumerating job ids, with no check it owned the job.
        Mirrors #134's guard: see
        ``src.api.ownership.resolve_owned_dead_letter_job``.
        """
        from src.temporal import shared_services

        db_svc = shared_services.get_db_service()
        await resolve_owned_dead_letter_job(db_svc, job_id, workspace_id)

        await db_svc.update_dead_letter_status(job_id, "abandoned")
        return {"abandoned": True, "job_id": job_id}

    app.include_router(dl_router)
    return app
