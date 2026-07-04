"""Document fetch activity -- lightweight validation that the file exists in storage.

Uses file_exists() for a stat-only check instead of reading the full file.
This avoids downloading the entire document just to verify accessibility,
since extract_text reads the file immediately after.
"""

import structlog
from temporalio import activity

from src.temporal.lineage import track_event
from src.temporal.models import FetchDocumentInput, FetchDocumentOutput

logger = structlog.get_logger(__name__)


@activity.defn
async def fetch_document(input: FetchDocumentInput) -> FetchDocumentOutput:
    """Validate that the document exists in storage and return its size.

    This activity performs a lightweight existence check without downloading
    the file content. The actual content is read by extract_text.

    For local storage, uses file_exists() (os.path.is_file()).
    For GCS, uses blob.exists().
    For URL-based backends, falls back to a full read since HEAD
    requests may not be supported by all storage URLs.

    Args:
        input: Contains storage backend, path, bucket, and optional URL

    Returns:
        FetchDocumentOutput with the document size in bytes

    Raises:
        RuntimeError: If document cannot be found or accessed
    """
    from src.temporal.shared_services import get_storage_service

    storage_service = get_storage_service()

    async with track_event(
        workflow_run_id=input.workflow_run_id or "",
        document_id=input.document_id,
        workspace_id=input.workspace_id,
        event_type="document_fetched",
    ):
        logger.info(
            "Validating document in storage",
            document_id=input.document_id,
            backend=input.storage_backend,
            path=input.storage_path,
            bucket=input.storage_bucket,
        )

        if input.storage_backend in ("local", "gcs", "s3"):
            backend = storage_service.get_backend(input.storage_backend)

            if not backend.file_exists(input.storage_path, input.storage_bucket):
                raise RuntimeError(
                    f"Document not found in {input.storage_backend} storage: "
                    f"{input.storage_path}"
                )

            # Stat/HEAD-based size — never downloads the content. extract_text
            # reads the bytes immediately after, so a full read here just
            # doubled egress on remote (S3/GCS) objects (#22).
            size_bytes = backend.get_size(input.storage_path, input.storage_bucket)

        elif input.storage_backend == "azure":
            if not input.storage_url:
                raise RuntimeError(
                    f"Storage backend '{input.storage_backend}' requires storage_url"
                )
            # URL-based: must read to validate
            content = storage_service.read_file_from_url(input.storage_url)
            size_bytes = len(content)

        else:
            raise RuntimeError(f"Unknown storage backend: {input.storage_backend}")

        logger.info(
            "Document validated in storage",
            document_id=input.document_id,
            size_bytes=size_bytes,
        )

    return FetchDocumentOutput(size_bytes=size_bytes)
