"""Unit tests for src/services/document_intake.py (#87 Task 3).

``intake_document`` is a PURE MOVE of the POST /v1/documents body (steps 1-6:
content-type validation, size validation, content-hash dedup, S3 upload,
pending-row persistence, MQ publish) out of the REST handler so both REST and
the ``upload_document`` MCP tool share identical behaviour. These tests pin
that behaviour at the service boundary — the REST-layer tests in
tests/unit/test_upload_document.py exercise the same logic through the HTTP
route and must stay green after the extraction.
"""

from __future__ import annotations

import hashlib
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.exceptions import BadRequestError, ServiceUnavailableError
from src.services import document_intake

pytestmark = [pytest.mark.unit]

# Content-type "application/pdf" is now magic-byte sniffed at intake (#117),
# so placeholder bytes that don't start with the PDF signature (%PDF-) are
# rejected as a mismatch. Tests below that only care about the generic
# pipeline (storage/dedup/MQ), not real PDF parsing, use this PDF-shaped
# placeholder instead of arbitrary bytes so they exercise that pipeline
# without tripping the new sniff check.
_PDF_BYTES = b"%PDF-1.4\nhello world"


@pytest.fixture
def mock_db():
    db = AsyncMock()
    db.get_document_id_by_content_hash = AsyncMock(return_value=None)
    db.get_document_id_by_filename = AsyncMock(return_value=None)
    db.create_or_reset_pending_document = AsyncMock(return_value=None)
    db.mark_document_failed = AsyncMock(return_value=None)
    # Default: no existing row, so the identical-content short-circuit is off
    # unless a test opts in (see TestIntakeDocumentDedup). A bare AsyncMock
    # would otherwise auto-return a truthy mock and mis-trigger the fast path.
    db.get_document = AsyncMock(return_value=None)
    db.get_document_upload_fields = AsyncMock(return_value=None)
    return db


@pytest.fixture
def mock_storage():
    storage = MagicMock()
    storage.generate_key.return_value = "test-workspace-id/fake-uuid/test.pdf"
    storage.upload_file = AsyncMock(return_value="test-workspace-id/fake-uuid/test.pdf")
    storage.build_storage_url.return_value = (
        "s3://inherent-documents/test-workspace-id/fake-uuid/test.pdf"
    )
    storage._bucket = "inherent-documents"
    return storage


@pytest.fixture
def mock_mq():
    mq = AsyncMock()
    mq.publish = AsyncMock(return_value="1234567890-0")
    return mq


def _patches(mock_storage, mock_mq):
    return (
        patch.object(document_intake, "get_storage_service", return_value=mock_storage),
        patch.object(document_intake, "get_mq_service", new=AsyncMock(return_value=mock_mq)),
    )


class TestIntakeDocumentSuccess:
    async def test_returns_pending_upload_response(self, mock_db, mock_storage, mock_mq):
        p1, p2 = _patches(mock_storage, mock_mq)
        with p1, p2:
            result = await document_intake.intake_document(
                database=mock_db,
                workspace_id="test-workspace-id",
                user_id="test-user-id",
                content_bytes=_PDF_BYTES,
                filename="test.pdf",
                content_type="application/pdf",
            )

        assert result.name == "test.pdf"
        assert result.workspace_id == "test-workspace-id"
        assert result.mime_type == "application/pdf"
        assert result.size_bytes == len(_PDF_BYTES)
        assert result.status == "pending"
        assert result.document_id
        assert result.storage_url

    async def test_calls_storage_with_expected_args(self, mock_db, mock_storage, mock_mq):
        p1, p2 = _patches(mock_storage, mock_mq)
        with p1, p2:
            await document_intake.intake_document(
                database=mock_db,
                workspace_id="test-workspace-id",
                user_id="test-user-id",
                content_bytes=_PDF_BYTES,
                filename="test.pdf",
                content_type="application/pdf",
            )

        mock_storage.generate_key.assert_called_once_with("test-workspace-id", "test.pdf")
        mock_storage.upload_file.assert_awaited_once()

    async def test_publishes_mq_message(self, mock_db, mock_storage, mock_mq):
        p1, p2 = _patches(mock_storage, mock_mq)
        with p1, p2:
            await document_intake.intake_document(
                database=mock_db,
                workspace_id="test-workspace-id",
                user_id="test-user-id",
                content_bytes=_PDF_BYTES,
                filename="test.pdf",
                content_type="application/pdf",
            )

        mock_mq.publish.assert_awaited_once()
        call_args = mock_mq.publish.call_args
        assert call_args[0][0] == "core.document.uploaded.v1"
        msg = call_args[0][1]
        assert msg["event_type"] == "document.uploaded"
        assert msg["workspace_id"] == "test-workspace-id"
        assert msg["user_id"] == "test-user-id"
        assert msg["original_filename"] == "test.pdf"
        assert msg["content_type"] == "application/pdf"
        assert msg["storage_backend"] == "s3"
        # inherent-prime/inherent#141 pattern-sweep finding: this function is
        # the ONLY in-repo publisher of core.document.uploaded.v1 (shared by
        # both the REST route and the MCP upload_document tool), so without
        # this the ingestion-svc Temporal memo showed "unknown" for every
        # upload this service makes -- indistinguishable from a message
        # produced before the source field existed.
        assert msg["source"] == "public-api"

    async def test_content_hash_persisted_and_checked_before_filename(
        self, mock_db, mock_storage, mock_mq
    ):
        p1, p2 = _patches(mock_storage, mock_mq)
        with p1, p2:
            await document_intake.intake_document(
                database=mock_db,
                workspace_id="test-workspace-id",
                user_id="test-user-id",
                content_bytes=_PDF_BYTES,
                filename="test.pdf",
                content_type="application/pdf",
            )

        kwargs = mock_db.create_or_reset_pending_document.call_args.kwargs
        assert kwargs["content_hash"] == hashlib.sha256(_PDF_BYTES).hexdigest()
        mock_db.get_document_id_by_content_hash.assert_awaited_once()
        mock_db.get_document_id_by_filename.assert_awaited_once()


class TestIntakeDocumentValidation:
    async def test_unsupported_mime_type_raises_bad_request(self, mock_db, mock_storage, mock_mq):
        p1, p2 = _patches(mock_storage, mock_mq)
        with p1, p2, pytest.raises(BadRequestError):
            await document_intake.intake_document(
                database=mock_db,
                workspace_id="test-workspace-id",
                user_id="test-user-id",
                content_bytes=b"hello world",
                filename="malware.exe",
                content_type="application/x-msdownload",
            )

    async def test_empty_content_raises_bad_request(self, mock_db, mock_storage, mock_mq):
        p1, p2 = _patches(mock_storage, mock_mq)
        with p1, p2, pytest.raises(BadRequestError):
            await document_intake.intake_document(
                database=mock_db,
                workspace_id="test-workspace-id",
                user_id="test-user-id",
                content_bytes=b"",
                filename="empty.txt",
                content_type="text/plain",
            )

    async def test_oversized_content_raises_bad_request(self, mock_db, mock_storage, mock_mq):
        big_content = b"x" * (50 * 1024 * 1024 + 1)
        p1, p2 = _patches(mock_storage, mock_mq)
        with p1, p2, pytest.raises(BadRequestError):
            await document_intake.intake_document(
                database=mock_db,
                workspace_id="test-workspace-id",
                user_id="test-user-id",
                content_bytes=big_content,
                filename="big.txt",
                content_type="text/plain",
            )


class TestIntakeDocumentExplicitlyRejectedLegacyFormats:
    """#124/#126: legacy .doc and Outlook .msg are intentionally NOT in
    FILE_TYPE_REGISTRY (accept-then-garble is explicitly disallowed), but
    they get a SPECIFIC, actionable 400 message pointing at the supported
    replacement -- not the generic "Unsupported file type" text every other
    unregistered type gets. Neither upload may leave any partial state:
    nothing stored, nothing persisted, for a request rejected before step 6.
    """

    async def test_legacy_doc_rejected_with_convert_to_docx_message(
        self, mock_db, mock_storage, mock_mq
    ):
        p1, p2 = _patches(mock_storage, mock_mq)
        with p1, p2, pytest.raises(BadRequestError) as exc_info:
            await document_intake.intake_document(
                database=mock_db,
                workspace_id="test-workspace-id",
                user_id="test-user-id",
                content_bytes=b"\xd0\xcf\x11\xe0 fake OLE compound file bytes",
                filename="report.doc",
                content_type="application/msword",
            )

        assert ".docx" in exc_info.value.detail
        mock_storage.upload_file.assert_not_awaited()
        mock_db.create_or_reset_pending_document.assert_not_awaited()

    async def test_outlook_msg_rejected_with_export_to_eml_message(
        self, mock_db, mock_storage, mock_mq
    ):
        p1, p2 = _patches(mock_storage, mock_mq)
        with p1, p2, pytest.raises(BadRequestError) as exc_info:
            await document_intake.intake_document(
                database=mock_db,
                workspace_id="test-workspace-id",
                user_id="test-user-id",
                content_bytes=b"\xd0\xcf\x11\xe0 fake OLE compound file bytes",
                filename="message.msg",
                content_type="application/vnd.ms-outlook",
            )

        assert ".eml" in exc_info.value.detail
        mock_storage.upload_file.assert_not_awaited()
        mock_db.create_or_reset_pending_document.assert_not_awaited()


class TestIntakeDocumentExtensionFallback:
    """#122: `intake_document` resolves via `get_spec_for_upload`, which
    consults `filename`'s extension ONLY when `content_type` is generic
    (`application/octet-stream`) or absent -- completing the design
    `FileTypeSpec.extensions` was reserved for at #117."""

    async def test_octet_stream_with_registered_extension_accepted(
        self, mock_db, mock_storage, mock_mq
    ):
        p1, p2 = _patches(mock_storage, mock_mq)
        with p1, p2:
            result = await document_intake.intake_document(
                database=mock_db,
                workspace_id="test-workspace-id",
                user_id="test-user-id",
                content_bytes=b"def main():\n    pass\n",
                filename="main.py",
                content_type="application/octet-stream",
            )
        assert result.status == "pending"
        # The client-sent value is preserved verbatim (#122) -- the resolved
        # 'code' spec validates the upload but never rewrites content_type.
        assert result.mime_type == "application/octet-stream"

    async def test_octet_stream_with_unregistered_extension_still_rejected(
        self, mock_db, mock_storage, mock_mq
    ):
        p1, p2 = _patches(mock_storage, mock_mq)
        with p1, p2, pytest.raises(BadRequestError):
            await document_intake.intake_document(
                database=mock_db,
                workspace_id="test-workspace-id",
                user_id="test-user-id",
                content_bytes=b"whatever",
                filename="notes.xyz",
                content_type="application/octet-stream",
            )

    async def test_specific_unregistered_mime_not_widened_by_extension(
        self, mock_db, mock_storage, mock_mq
    ):
        """SECURITY: a SPECIFIC (not generic) unregistered content type must
        not be widened just because the filename carries a known
        extension."""
        p1, p2 = _patches(mock_storage, mock_mq)
        with p1, p2, pytest.raises(BadRequestError):
            await document_intake.intake_document(
                database=mock_db,
                workspace_id="test-workspace-id",
                user_id="test-user-id",
                content_bytes=b"def main(): pass",
                filename="main.py",
                content_type="application/x-something-made-up",
            )

    async def test_octet_stream_binary_content_with_code_extension_rejected(
        self, mock_db, mock_storage, mock_mq
    ):
        """Failure path: the fallback resolves a spec to VALIDATE against --
        it is not a bypass. PNG bytes behind a '.py' filename are still
        caught by the magic-byte sniff."""
        p1, p2 = _patches(mock_storage, mock_mq)
        with p1, p2, pytest.raises(BadRequestError):
            await document_intake.intake_document(
                database=mock_db,
                workspace_id="test-workspace-id",
                user_id="test-user-id",
                content_bytes=b"\x89PNG\r\n\x1a\n fake png bytes",
                filename="malicious.py",
                content_type="application/octet-stream",
            )


class TestIntakeDocumentMagicByteSniffing:
    """#117: MIME type is entirely client-supplied and, before this, was
    never checked against the actual bytes. These pin the closed hole --
    every case named in the #117 issue and review note.
    """

    async def test_png_bytes_declared_as_text_plain_rejected(self, mock_db, mock_storage, mock_mq):
        """The acceptance-criteria scenario verbatim: a mislabeled binary
        (PNG bytes) declared as text/plain must be rejected at upload.

        Filename deliberately does NOT end in '.png' (unlike the sibling
        extension-consistency tests) so this exercises the BYTE-level sniff
        specifically -- content_intake.py runs the extension check first,
        and a filename ending in '.png' would be caught by that check
        instead, leaving this test not actually proving the byte sniff
        works. A generic '.upload' extension is not in the registry, so
        the extension check is a no-op here and the byte mismatch is what
        gets caught.
        """
        p1, p2 = _patches(mock_storage, mock_mq)
        with p1, p2, pytest.raises(BadRequestError) as exc_info:
            await document_intake.intake_document(
                database=mock_db,
                workspace_id="test-workspace-id",
                user_id="test-user-id",
                content_bytes=b"\x89PNG\r\n\x1a\n fake png bytes",
                filename="scan.upload",
                content_type="text/plain",
            )
        assert "text/plain" in str(exc_info.value)
        # Nothing downstream must have run -- the file was never dedup'd,
        # stored, or persisted, so no half-applied state exists.
        mock_storage.upload_file.assert_not_awaited()
        mock_db.create_or_reset_pending_document.assert_not_awaited()
        mock_mq.publish.assert_not_awaited()

    async def test_png_bytes_and_png_filename_declared_as_text_plain_rejected(
        self, mock_db, mock_storage, mock_mq
    ):
        """The SAME mislabeling but with a revealing '.png' filename: now
        BOTH the extension check and the byte sniff would object -- whichever
        runs first still rejects with 400 and zero side effects, so the two
        checks compose instead of interfering."""
        p1, p2 = _patches(mock_storage, mock_mq)
        with p1, p2, pytest.raises(BadRequestError):
            await document_intake.intake_document(
                database=mock_db,
                workspace_id="test-workspace-id",
                user_id="test-user-id",
                content_bytes=b"\x89PNG\r\n\x1a\n fake png bytes",
                filename="scan.png",
                content_type="text/plain",
            )
        mock_storage.upload_file.assert_not_awaited()
        mock_db.create_or_reset_pending_document.assert_not_awaited()
        mock_mq.publish.assert_not_awaited()

    async def test_pdf_declared_but_bytes_are_not_pdf_rejected(
        self, mock_db, mock_storage, mock_mq
    ):
        """'A file whose magic bytes contradict its name': declared PDF, but
        the bytes don't start with the PDF signature."""
        p1, p2 = _patches(mock_storage, mock_mq)
        with p1, p2, pytest.raises(BadRequestError):
            await document_intake.intake_document(
                database=mock_db,
                workspace_id="test-workspace-id",
                user_id="test-user-id",
                content_bytes=b"this is not a pdf, just plain text",
                filename="report.pdf",
                content_type="application/pdf",
            )
        mock_storage.upload_file.assert_not_awaited()
        mock_db.create_or_reset_pending_document.assert_not_awaited()

    async def test_correctly_labeled_pdf_passes_the_sniff(self, mock_db, mock_storage, mock_mq):
        """Sanity check in the other direction: real PDF bytes with the
        correct declared type sail through untouched."""
        p1, p2 = _patches(mock_storage, mock_mq)
        with p1, p2:
            result = await document_intake.intake_document(
                database=mock_db,
                workspace_id="test-workspace-id",
                user_id="test-user-id",
                content_bytes=_PDF_BYTES,
                filename="report.pdf",
                content_type="application/pdf",
            )
        assert result.status == "pending"

    async def test_empty_file_reports_empty_not_mismatch(self, mock_db, mock_storage, mock_mq):
        """Size validation runs BEFORE the magic-byte sniff, so an empty
        upload keeps the specific, actionable 'file is empty' message
        instead of a less-useful generic signature-mismatch one."""
        p1, p2 = _patches(mock_storage, mock_mq)
        with p1, p2, pytest.raises(BadRequestError) as exc_info:
            await document_intake.intake_document(
                database=mock_db,
                workspace_id="test-workspace-id",
                user_id="test-user-id",
                content_bytes=b"",
                filename="report.pdf",
                content_type="application/pdf",
            )
        assert "empty" in str(exc_info.value).lower()


class TestIntakeDocumentExtensionConsistency:
    """#117: the filename's extension is cross-checked against the declared
    content type, independent of the bytes-vs-declared sniff above. Between
    the two checks, any pairwise disagreement among {declared type, filename,
    bytes} is caught by at least one of them.
    """

    async def test_extension_mismatch_rejected(self, mock_db, mock_storage, mock_mq):
        """Filename says '.pdf' (a KNOWN, registered extension), declared
        type says text/plain -- a real, actionable disagreement."""
        p1, p2 = _patches(mock_storage, mock_mq)
        with p1, p2, pytest.raises(BadRequestError) as exc_info:
            await document_intake.intake_document(
                database=mock_db,
                workspace_id="test-workspace-id",
                user_id="test-user-id",
                content_bytes=b"just plain text, matches the declared type",
                filename="report.pdf",
                content_type="text/plain",
            )
        assert "report.pdf" in str(exc_info.value)
        mock_storage.upload_file.assert_not_awaited()
        mock_db.create_or_reset_pending_document.assert_not_awaited()

    async def test_unknown_extension_is_accepted(self, mock_db, mock_storage, mock_mq):
        """An extension the registry doesn't recognize (e.g. '.log', not yet
        a supported format) must NOT be rejected on that basis -- content
        type remains authoritative for anything the registry doesn't know."""
        p1, p2 = _patches(mock_storage, mock_mq)
        with p1, p2:
            result = await document_intake.intake_document(
                database=mock_db,
                workspace_id="test-workspace-id",
                user_id="test-user-id",
                content_bytes=b"2026-08-06 some log line",
                filename="server.log",
                content_type="text/plain",
            )
        assert result.status == "pending"

    async def test_no_extension_is_accepted(self, mock_db, mock_storage, mock_mq):
        """The REST route's 'unnamed' fallback (no extension at all) must
        keep working -- nothing to compare, so nothing to reject."""
        p1, p2 = _patches(mock_storage, mock_mq)
        with p1, p2:
            result = await document_intake.intake_document(
                database=mock_db,
                workspace_id="test-workspace-id",
                user_id="test-user-id",
                content_bytes=b"just plain text",
                filename="unnamed",
                content_type="text/plain",
            )
        assert result.status == "pending"


class TestIntakeDocumentDedup:
    async def test_reupload_reuses_existing_document_id_by_filename(
        self, mock_db, mock_storage, mock_mq
    ):
        existing_id = "existing-doc-id-123"
        mock_db.get_document_id_by_filename = AsyncMock(return_value=existing_id)

        p1, p2 = _patches(mock_storage, mock_mq)
        with p1, p2:
            result = await document_intake.intake_document(
                database=mock_db,
                workspace_id="test-workspace-id",
                user_id="test-user-id",
                content_bytes=_PDF_BYTES,
                filename="test.pdf",
                content_type="application/pdf",
            )

        assert result.document_id == existing_id
        assert (
            mock_db.create_or_reset_pending_document.call_args.kwargs["document_id"] == existing_id
        )
        assert mock_mq.publish.call_args[0][1]["document_id"] == existing_id

    async def test_reupload_reuses_existing_document_id_by_content_hash(
        self, mock_db, mock_storage, mock_mq
    ):
        existing_id = "original-doc-id-abc"
        mock_db.get_document_id_by_content_hash = AsyncMock(return_value=existing_id)
        mock_db.get_document_id_by_filename = AsyncMock(return_value=None)

        p1, p2 = _patches(mock_storage, mock_mq)
        with p1, p2:
            result = await document_intake.intake_document(
                database=mock_db,
                workspace_id="test-workspace-id",
                user_id="test-user-id",
                content_bytes=b"verbatim",
                filename="guide-copy.md",
                content_type="text/markdown",
            )

        assert result.document_id == existing_id
        mock_db.get_document_id_by_filename.assert_not_awaited()

    async def test_content_hash_match_already_ingested_short_circuits(
        self, mock_db, mock_storage, mock_mq
    ):
        """Identical content whose existing doc is not 'failed' must NOT re-index.

        A content-hash match means the exact bytes are already known; re-running
        the pipeline yields byte-identical chunks/embeddings (wasted compute) and
        a redundant re-index can strand the document non-'processed' under load.
        The fast path returns the existing document without resetting its row,
        re-uploading to S3, or re-publishing an ingestion event.
        """
        existing_id = "already-ingested-id"
        mock_db.get_document_id_by_content_hash = AsyncMock(return_value=existing_id)
        mock_db.get_document = AsyncMock(
            return_value=SimpleNamespace(
                status="processed",
                name="guide.md",
                mime_type="text/markdown",
                size_bytes=8,
            )
        )
        mock_db.get_document_upload_fields = AsyncMock(
            return_value={"storage_url": "s3://inherent-documents/ws/existing/guide.md"}
        )

        p1, p2 = _patches(mock_storage, mock_mq)
        with p1, p2:
            result = await document_intake.intake_document(
                database=mock_db,
                workspace_id="test-workspace-id",
                user_id="test-user-id",
                content_bytes=b"verbatim",
                filename="guide-copy.md",
                content_type="text/markdown",
            )

        assert result.document_id == existing_id
        assert result.status == "processed"
        # No side effects: the row is untouched and nothing is re-enqueued.
        mock_db.create_or_reset_pending_document.assert_not_awaited()
        mock_mq.publish.assert_not_awaited()
        mock_storage.upload_file.assert_not_awaited()

    async def test_content_hash_match_failed_doc_reindexes(self, mock_db, mock_storage, mock_mq):
        """A content match on a 'failed' document must still re-index to recover."""
        existing_id = "failed-doc-id"
        mock_db.get_document_id_by_content_hash = AsyncMock(return_value=existing_id)
        mock_db.get_document = AsyncMock(
            return_value=SimpleNamespace(
                status="failed",
                name="guide.md",
                mime_type="text/markdown",
                size_bytes=8,
            )
        )

        p1, p2 = _patches(mock_storage, mock_mq)
        with p1, p2:
            result = await document_intake.intake_document(
                database=mock_db,
                workspace_id="test-workspace-id",
                user_id="test-user-id",
                content_bytes=b"verbatim",
                filename="guide.md",
                content_type="text/markdown",
            )

        assert result.document_id == existing_id
        # Recovery: the row is reset and a fresh ingestion event is published.
        mock_db.create_or_reset_pending_document.assert_awaited_once()
        mock_mq.publish.assert_awaited_once()

    async def test_new_content_generates_new_uuid(self, mock_db, mock_storage, mock_mq):
        p1, p2 = _patches(mock_storage, mock_mq)
        with p1, p2:
            result = await document_intake.intake_document(
                database=mock_db,
                workspace_id="test-workspace-id",
                user_id="test-user-id",
                content_bytes=_PDF_BYTES,
                filename="test.pdf",
                content_type="application/pdf",
            )

        assert uuid.UUID(result.document_id)


class TestIntakeDocumentServiceFailures:
    async def test_storage_failure_raises_service_unavailable(self, mock_db, mock_mq):
        failing_storage = MagicMock()
        failing_storage.generate_key.return_value = "ws/uuid/file.pdf"
        failing_storage.upload_file = AsyncMock(side_effect=Exception("S3 unreachable"))

        p1, p2 = _patches(failing_storage, mock_mq)
        with p1, p2, pytest.raises(ServiceUnavailableError):
            await document_intake.intake_document(
                database=mock_db,
                workspace_id="test-workspace-id",
                user_id="test-user-id",
                content_bytes=_PDF_BYTES,
                filename="test.pdf",
                content_type="application/pdf",
            )

    async def test_mq_failure_returns_failed_status_not_raise(self, mock_db, mock_storage):
        failing_mq = AsyncMock()
        failing_mq.publish = AsyncMock(side_effect=Exception("Redis down"))

        p1, p2 = _patches(mock_storage, failing_mq)
        with p1, p2:
            result = await document_intake.intake_document(
                database=mock_db,
                workspace_id="test-workspace-id",
                user_id="test-user-id",
                content_bytes=_PDF_BYTES,
                filename="test.pdf",
                content_type="application/pdf",
            )

        assert result.status == "failed"
        assert "enqueue" in result.message.lower()
        mock_db.mark_document_failed.assert_awaited_once()
