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
from temporalio.client import Client
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError

from src.api.auth import verify_api_key
from src.config.settings import Settings
from src.services.metrics import get_metrics
from src.temporal.models import (
    ChunkEditInput,
    ChunkEditResult,
    DocumentIngestionInput,
    WorkflowResult,
)
from src.temporal.worker import TemporalWorkerManager
from src.temporal.workflows import ChunkEditWorkflow, DocumentIngestionWorkflow

logger = structlog.get_logger(__name__)


# =============================================================================
# Request / Response Models
# =============================================================================


class IngestRequest(BaseModel):
    """Request body for triggering document ingestion."""

    document_id: str = Field(..., description="Unique document identifier")
    workspace_id: str = Field(..., description="Workspace identifier")
    user_id: str = Field(..., description="User who uploaded the document")
    filename: str = Field(..., description="Storage filename (generated)")
    original_filename: str = Field(..., description="Original filename from upload")
    content_type: str = Field(..., description="MIME type of the document")
    size_bytes: int = Field(..., gt=0, description="File size in bytes")
    storage_backend: Literal["local", "s3", "gcs", "azure"] = Field(
        ..., description="Storage backend"
    )
    storage_path: str = Field(..., description="Path to file in storage")
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
                "storage_path": "workspaces/ws123/1234567890-document.pdf",
            }
        }
    }


class IngestAcceptedResponse(BaseModel):
    """Returned when a workflow is started asynchronously (HTTP 202)."""

    workflow_id: str
    document_id: str
    status: Literal["started", "already_running"] = "started"


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
        version="0.5.0",
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
            version="0.5.0",
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
            409: {"description": "Workflow already running for this document"},
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
        """
        client: Client = request.app.state.temporal_client
        settings: Settings = request.app.state.settings
        task_queue: str = settings.temporal_task_queue

        workflow_input = DocumentIngestionInput(
            document_id=body.document_id,
            workspace_id=body.workspace_id,
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
            result: WorkflowResult = await handle.result()
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
    )
    async def get_ingestion_status(document_id: str, request: Request):
        """Query the real-time progress of a running ingestion workflow."""
        client: Client = request.app.state.temporal_client
        workflow_id = f"ingest-{document_id}"

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
    )
    async def edit_chunk(
        document_id: str,
        chunk_index: int,
        body: ChunkEditRequest,
        request: Request,
    ):
        """Edit a chunk via Temporal workflow (updates PG + re-embeds in Weaviate)."""
        client: Client = request.app.state.temporal_client
        settings: Settings = request.app.state.settings

        workflow_input = ChunkEditInput(
            document_id=document_id,
            chunk_index=chunk_index,
            content=body.content,
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
    )
    async def delete_document(
        document_id: str,
        request: Request,
        workspace_id: str = Query(..., description="Workspace ID"),
        user_id: str = Query(..., description="User ID"),
    ):
        """Delete a document from PostgreSQL and its chunks from Weaviate.

        Weaviate cleanup is best-effort: if it fails, the PG delete still
        succeeds and the response indicates ``weaviate_cleaned=false``.
        """
        from src.temporal import shared_services

        # --- Weaviate cleanup (best-effort, before PG delete) ---
        weaviate_cleaned = False
        weaviate_svc = shared_services.get_weaviate_service()
        if weaviate_svc is not None:
            weaviate_cleaned, _ = await weaviate_svc.delete_document_chunks_graceful(
                workspace_id=workspace_id,
                document_id=document_id,
                user_id=user_id,
            )
        else:
            logger.warning(
                "Weaviate unavailable, skipping chunk cleanup",
                document_id=document_id,
            )

        # --- PostgreSQL delete ---
        db_svc = shared_services.get_db_service()
        deleted = await db_svc.delete_document(document_id)

        if not deleted:
            raise HTTPException(
                status_code=404,
                detail=f"Document {document_id} not found in PostgreSQL.",
            )

        logger.info(
            "Document deleted",
            document_id=document_id,
            workspace_id=workspace_id,
            user_id=user_id,
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

    @lineage_router.get("/{document_id}")
    async def get_lineage(document_id: str):
        """Get data lineage (ingestion events) for a document.

        Returns an ordered list of pipeline step events showing what
        happened during ingestion of the given document.
        """
        from src.temporal import shared_services

        db_svc = shared_services.get_db_service()
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
        workspace_id: str | None = Query(None),
        status: str | None = Query("pending"),
        limit: int = Query(50, ge=1, le=200),
    ):
        """List dead-letter jobs with optional filtering."""
        from src.temporal import shared_services

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

    @dl_router.get("/{job_id}")
    async def get_dead_letter_job(job_id: int):
        """Get a single dead-letter job by ID."""
        from src.temporal import shared_services

        db_svc = shared_services.get_db_service()
        job = await db_svc.get_dead_letter_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail=f"Dead-letter job {job_id} not found")

        row = {}
        for key, value in job.items():
            if hasattr(value, "isoformat"):
                row[key] = value.isoformat()
            else:
                row[key] = value
        return row

    @dl_router.post("/{job_id}/retry")
    async def retry_dead_letter_job(job_id: int, request: Request):
        """Retry a dead-letter job by re-publishing its original message."""
        from src.temporal import shared_services

        db_svc = shared_services.get_db_service()
        job = await db_svc.get_dead_letter_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail=f"Dead-letter job {job_id} not found")

        if job.get("status") not in ("pending", "retrying"):
            raise HTTPException(
                status_code=409,
                detail=f"Job {job_id} has status '{job.get('status')}', cannot retry",
            )

        # Increment retry count
        await db_svc.increment_dead_letter_retry(job_id)

        # Re-trigger workflow
        original_message = job.get("original_message", {})
        trigger = request.app.state.trigger
        try:
            workflow_id = await trigger.trigger_workflow_async(original_message)
            return {"retried": True, "job_id": job_id, "new_workflow_id": workflow_id}
        except Exception as e:
            # Reset status back to pending on failure
            await db_svc.update_dead_letter_status(job_id, "pending")
            raise HTTPException(status_code=500, detail=f"Retry failed: {e}") from e

    @dl_router.post("/{job_id}/abandon")
    async def abandon_dead_letter_job(job_id: int):
        """Mark a dead-letter job as permanently abandoned."""
        from src.temporal import shared_services

        db_svc = shared_services.get_db_service()
        job = await db_svc.get_dead_letter_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail=f"Dead-letter job {job_id} not found")

        await db_svc.update_dead_letter_status(job_id, "abandoned")
        return {"abandoned": True, "job_id": job_id}

    app.include_router(dl_router)
    return app
