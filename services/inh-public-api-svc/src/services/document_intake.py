"""Shared document intake pipeline (#87 API parity Task 3).

Extracted from the POST /v1/documents REST handler (src/api/v1/documents.py)
so both the REST route and the ``upload_document`` MCP tool run the exact
same validation + storage + persistence + enqueue logic — a PURE MOVE with no
behaviour change. The REST route now reads the ``UploadFile`` bytes and
delegates everything else here; the MCP tool UTF-8 encodes its text ``content``
argument and calls the same function.
"""

import hashlib
import uuid
from datetime import datetime, timezone

from inh_contracts.file_types import (
    ContentTypeMismatchError,
    ExtensionMismatchError,
    check_extension_consistency,
    explicitly_unsupported_message_for_mime,
    get_spec_for_upload,
    sniff_content_type,
)

from src.config import settings
from src.config.constants import ALLOWED_MIME_TYPES, MAX_UPLOAD_SIZE_BYTES
from src.core.exceptions import BadRequestError, ServiceUnavailableError
from src.models.document import DocumentUploadResponse
from src.services.compensation import mark_document_failed_with_retry
from src.services.database import DatabaseService
from src.services.mq import get_mq_service
from src.services.storage import get_storage_service
from src.utils import get_logger

logger = get_logger(__name__)


async def intake_document(
    *,
    database: DatabaseService,
    workspace_id: str,
    user_id: str,
    content_bytes: bytes,
    filename: str,
    content_type: str,
) -> DocumentUploadResponse:
    """Validate, dedup, store and enqueue a document for ingestion.

    Mirrors (byte for byte) the former inline body of POST /v1/documents,
    plus the #117 validation steps that close real validation holes -- three
    independent signals describe an upload (declared content type, filename,
    actual bytes), and any pairwise disagreement among them is now caught:

    1. Validate ``content_type`` against ``ALLOWED_MIME_TYPES`` (derived from
       the FILE_TYPE_REGISTRY single source of truth, see constants.py). A
       DELIBERATELY-unsupported format with a real replacement (legacy .doc,
       Outlook .msg -- see ``EXPLICITLY_UNSUPPORTED``) is checked FIRST and
       rejected with a specific, actionable message naming the replacement
       (#124/#126) -- before falling through to the generic registry lookup.
       A GENERIC or absent content type (``application/octet-stream``, the
       REST route's own fallback for a missing header) additionally
       consults `filename`'s extension via ``get_spec_for_upload`` (#122) --
       completing the design ``FileTypeSpec.extensions`` was reserved for at
       #117. This never widens acceptance of a SPECIFIC-but-unregistered
       declared type -- see that function's docstring for the security
       rationale.
    2. Cross-check the filename's extension against the declared type
       (#117). A known BINARY-format extension (e.g. ``.pdf``, ``.docx``,
       ``.png``) registered to a DIFFERENT type than the one declared is a
       real disagreement. A text-format extension (``.txt``/``.md``/``.csv``/
       ``.html``/``.json``) never triggers this -- ``text/plain`` is a
       truthful, IANA-valid Content-Type for any of those, and real clients
       routinely send it; an unrecognized or absent extension is likewise
       not evidence of anything. Only a genuine binary-vs-declared
       contradiction is rejected.
    3. Validate size (non-empty, under the type's ``max_size_bytes`` override
       or the global ``MAX_UPLOAD_SIZE_BYTES`` default).
    4. Sniff: verify the bytes' magic signature agrees with the declared
       ``content_type`` (#117). Content-Type is entirely client-supplied and
       was previously never checked against the actual bytes, so a
       mislabeled binary (e.g. PNG bytes declared ``text/plain``) passed
       validation and was garbled downstream instead of rejected.
    5. Dedup: reuse an existing ``document_id`` keyed on (workspace,
       content_hash) first, then (workspace, filename) — see #75/#60.
    6. Upload the bytes to S3.
    7. Persist a durable ``pending`` row before enqueueing (#7).
    8. Publish the ``document.uploaded`` MQ message; on publish failure mark
       the row ``failed`` and return a ``status="failed"`` response instead of
       raising (the file IS stored, so this is not a request failure).

    Raises:
        BadRequestError: unsupported content type, a filename extension that
            contradicts the declared type, empty content, content over the
            size limit, or bytes whose magic signature contradicts the
            declared content type (#117).
        ServiceUnavailableError: S3 upload or pending-row persistence failed.
    """
    # --- 1. Validate content type -------------------------------------------
    # Checked BEFORE the generic registry lookup below (#124/#126): a format
    # that is deliberately unsupported but has a real replacement (legacy
    # .doc -> .docx, Outlook .msg -> .eml) gets a message naming that
    # replacement, not the generic allow-list dump every other unrecognized
    # type gets -- "explicit 400, never accept-then-garble" per both issues.
    # Sourced from inh_contracts' EXPLICITLY_UNSUPPORTED table (#124/#126
    # review blocker 3) -- a hand-maintained copy of this table lived here
    # alone until a review caught it meant the MCP `upload_document` surface
    # never learned about it and could accept the exact formats this is
    # supposed to reject.
    rejection_message = explicitly_unsupported_message_for_mime(content_type)
    if rejection_message is not None:
        raise BadRequestError(detail=rejection_message)

    # `get_spec_for_upload` resolves the declared MIME type directly when
    # it's registered (the common case, filename never inspected); it only
    # falls back to `filename`'s extension when `content_type` is generic/
    # absent (#122) -- see that function's docstring for why a SPECIFIC but
    # unregistered MIME type is deliberately NOT widened by this fallback.
    spec = get_spec_for_upload(content_type, filename)
    if spec is None:
        raise BadRequestError(
            detail=(
                f"Unsupported file type '{content_type}'. "
                f"Allowed types: {', '.join(ALLOWED_MIME_TYPES)}"
            ),
        )

    # --- 2. Cross-check the filename extension against the declared type ----
    # (#117). Independent of the byte-level sniff below: this catches a file
    # named "report.pdf" declared as text/plain even when its bytes ARE
    # perfectly valid plain text (so the sniff below has nothing to object
    # to) -- the filename itself is the contradicting signal here.
    try:
        check_extension_consistency(filename, spec)
    except ExtensionMismatchError as exc:
        raise BadRequestError(detail=str(exc)) from exc

    # --- 3. Validate size ----------------------------------------------------
    size_bytes = len(content_bytes)

    if size_bytes == 0:
        raise BadRequestError(detail="Uploaded file is empty.")

    # `is not None` (not `or`) so a hypothetical future override of exactly 0
    # is still honored rather than silently falling back to the global cap.
    max_size = spec.max_size_bytes if spec.max_size_bytes is not None else MAX_UPLOAD_SIZE_BYTES
    if size_bytes > max_size:
        max_mb = max_size // (1024 * 1024)
        raise BadRequestError(
            detail=f"File size ({size_bytes} bytes) exceeds the {max_mb} MB limit.",
        )

    # --- 4. Sniff magic bytes against the declared type (#117) ---------------
    # `spec` above already resolved successfully, so the only failure this
    # can raise is a mismatch -- an UnknownContentTypeError here would mean
    # step 1's own lookup was wrong, which is a contract bug, not a valid
    # runtime outcome for a client to trigger. `resolved_spec=spec` (#122) is
    # required, not optional, for a GENERIC content type (e.g.
    # "application/octet-stream"): `sniff_content_type` re-deriving from
    # `content_type` alone would fail to find it (that's exactly why step 1
    # needed the extension fallback in the first place), so the already-
    # resolved spec is threaded through instead of re-looked-up.
    try:
        sniff_content_type(content_bytes, content_type, resolved_spec=spec)
    except ContentTypeMismatchError as exc:
        raise BadRequestError(detail=str(exc)) from exc

    content_hash = hashlib.sha256(content_bytes).hexdigest()

    # --- 5. Dedup: reuse document_id rather than flood the workspace --------
    # Two re-upload shapes must collapse onto an existing document_id so
    # ingestion reindexes it instead of creating a duplicate document (with
    # duplicate chunks + embeddings) that floods top-k search results (#75):
    #   1. Same CONTENT under any filename — keyed on (workspace, content_hash).
    #      Checked first so a verbatim copy uploaded as ``guide-copy.md``
    #      collapses onto the original ``guide.md`` instead of multiplying it.
    #   2. Same FILENAME with changed content — keyed on (workspace, filename).
    #      Preserves the existing reindex-on-edit behaviour (#60) for a file
    #      whose bytes changed but whose logical identity (name) is unchanged.
    existing_document_id = await database.get_document_id_by_content_hash(
        workspace_id, content_hash
    )
    dedup_reason = "content_hash" if existing_document_id else None
    if not existing_document_id:
        existing_document_id = await database.get_document_id_by_filename(workspace_id, filename)
        dedup_reason = "filename" if existing_document_id else None

    if existing_document_id:
        document_id = existing_document_id
        logger.info(
            "Reusing existing document_id for re-upload (reindex)",
            document_id=document_id,
            workspace_id=workspace_id,
            filename=filename,
            dedup_reason=dedup_reason,
        )

        # Identical-content short-circuit (#75). A content-hash match means the
        # exact bytes are already known to this workspace, so re-running the
        # extract→chunk→embed→index pipeline would produce byte-identical chunks
        # and embeddings — pure wasted compute for the agent. It is also unsafe
        # under load: the ingestion workflow id is fixed per document
        # (`ingest-{document_id}`), so a redundant re-index serializes behind the
        # in-flight one and can leave the document stranded non-'processed' for
        # minutes. Unless the existing document actually needs recovery (status
        # 'failed'), return it as-is without re-uploading, resetting the row, or
        # re-enqueuing. Filename dedup and edited-content re-uploads (#60) have a
        # DIFFERENT content_hash, so they still fall through and re-index.
        if dedup_reason == "content_hash":
            existing = await database.get_document(document_id, workspace_id)
            if existing is not None and existing.status != "failed":
                upload_fields = await database.get_document_upload_fields(document_id, workspace_id)
                logger.info(
                    "Identical content already ingested; skipping redundant re-index",
                    document_id=document_id,
                    workspace_id=workspace_id,
                    status=existing.status,
                )
                return DocumentUploadResponse(
                    document_id=document_id,
                    name=existing.name,
                    workspace_id=workspace_id,
                    storage_url=(upload_fields or {}).get("storage_url") or "",
                    mime_type=existing.mime_type or content_type,
                    size_bytes=existing.size_bytes or size_bytes,
                    status=existing.status,
                    message="Identical content already ingested; returning existing document.",
                )
    else:
        document_id = str(uuid.uuid4())
        logger.info(
            "Assigning new document_id for upload",
            document_id=document_id,
            workspace_id=workspace_id,
            filename=filename,
        )

    # --- 6. Upload to S3 ----------------------------------------------------
    try:
        storage = get_storage_service()
        s3_key = storage.generate_key(workspace_id, filename)
        await storage.upload_file(content_bytes, s3_key, content_type)
        storage_url = storage.build_storage_url(s3_key)
    except Exception as exc:
        logger.error("S3 upload failed", error=str(exc), document_id=document_id)
        raise ServiceUnavailableError(
            service_name="storage",
            detail="Failed to store the uploaded file. Please try again later.",
        ) from exc

    # --- 7. Persist a durable 'pending' row BEFORE enqueueing ----------------
    # This makes the upload recoverable and lets GET /v1/documents/{id} return
    # the document (status='pending') immediately, instead of 404ing until
    # ingestion finishes. On re-upload of the same document_id, this resets the
    # row to a clean pending state.
    try:
        await database.create_or_reset_pending_document(
            document_id=document_id,
            workspace_id=workspace_id,
            user_id=user_id,
            filename=s3_key.rsplit("/", 1)[-1],
            original_filename=filename,
            content_type=content_type,
            size_bytes=size_bytes,
            storage_backend="s3",
            storage_path=s3_key,
            storage_bucket=storage._bucket,
            storage_url=storage_url,
            content_hash=content_hash,
        )
    except Exception as exc:
        logger.error(
            "Failed to persist pending document row",
            error=str(exc),
            document_id=document_id,
        )
        raise ServiceUnavailableError(
            service_name="database",
            detail="Failed to record the upload. Please try again later.",
        ) from exc

    # --- 8. Publish MQ message ----------------------------------------------
    now_iso = datetime.now(timezone.utc).isoformat()
    mq_message = {
        "event_type": "document.uploaded",
        "document_id": document_id,
        "workspace_id": workspace_id,
        "user_id": user_id,
        "filename": s3_key.rsplit("/", 1)[-1],
        "original_filename": filename,
        "content_type": content_type,
        "size_bytes": size_bytes,
        "storage_backend": "s3",
        "storage_path": s3_key,
        "storage_bucket": storage._bucket,
        "storage_url": storage_url,
        "timestamp": now_iso,
        "contract_version": "1.0.0",
        # Ingestion source labeling (inherent-systems/prime#187, ingestion-svc
        # consumer side: inherent-prime/inherent#141). This function is the
        # single intake path shared by both the REST route
        # (POST /v1/documents, src/api/v1/documents.py) and the MCP
        # upload_document tool (src/mcp_server/server.py) — both are the
        # public API surface, so "public-api" is correct for every call here.
        # Without this, ingestion-svc's Temporal memo shows "unknown" for
        # every upload this service makes, which reads to an operator as
        # "producer running stale code" rather than "not yet labeled".
        "source": "public-api",
    }

    try:
        mq = await get_mq_service()
        await mq.publish(settings.mq_topic_document_uploaded, mq_message)
    except Exception as exc:
        # The file is in S3 and a durable 'pending' row exists, so the upload
        # is recoverable. But ingestion was NOT triggered, so we must NOT
        # report success: mark the row 'failed' and reflect that in the
        # response. We keep "stored" semantics (no raise) because the file IS
        # stored — REST maps this to 201 with status="failed" in the body.
        logger.error(
            "MQ publish failed — file stored but ingestion not enqueued",
            error=str(exc),
            document_id=document_id,
        )
        # The mark is retried with backoff; on exhaustion the helper emits the
        # CRITICAL log + metric that flag the orphaned 'pending' row (#99).
        await mark_document_failed_with_retry(
            database,
            document_id,
            workspace_id,
            "ingestion enqueue failed",
            operation="upload_enqueue",
        )

        return DocumentUploadResponse(
            document_id=document_id,
            name=filename,
            workspace_id=workspace_id,
            storage_url=storage_url,
            mime_type=content_type,
            size_bytes=size_bytes,
            status="failed",
            message=(
                "Document was stored but could not be queued for processing "
                "(ingestion enqueue failed). Please retry the upload."
            ),
        )

    # --- 9. Return response --------------------------------------------------
    logger.info(
        "Document upload accepted",
        document_id=document_id,
        workspace_id=workspace_id,
        filename=filename,
        size_bytes=size_bytes,
    )

    return DocumentUploadResponse(
        document_id=document_id,
        name=filename,
        workspace_id=workspace_id,
        storage_url=storage_url,
        mime_type=content_type,
        size_bytes=size_bytes,
        status="pending",
        message="Document uploaded successfully. Processing will begin shortly.",
    )
