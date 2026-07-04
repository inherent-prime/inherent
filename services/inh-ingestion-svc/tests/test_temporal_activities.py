"""Tests for Temporal activities.

Covers:
- extract_text: error propagation, empty text guard, format handling

Activities use shared_services getters, so we patch those instead of
constructing service instances directly.
"""

from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from src.temporal.models import (
    ExtractTextInput,
    ExtractTextOutput,
    SetDocumentStatusInput,
    StoreDocumentInput,
)


# Override autouse fixtures from conftest that require a real database
@pytest.fixture(autouse=True)
async def cleanup_test_data():
    """Override to skip DB cleanup for activity tests."""
    yield


@pytest.fixture()
def db_service():
    """Override to return None (activity tests don't use DB directly)."""
    yield None


# =========================================================================
# extract_text activity tests
# =========================================================================


class TestExtractTextActivity:
    """Tests for the extract_text activity."""

    @pytest.fixture
    def extract_input(self):
        """Standard extract text input."""
        return ExtractTextInput(
            workflow_run_id="wf_test_001",
            storage_backend="local",
            storage_path="storage/doc.txt",
            content_type="text/plain",
            original_filename="doc.txt",
        )

    @patch("src.temporal.shared_services.get_staging_service")
    @patch("src.temporal.shared_services.get_storage_service")
    @pytest.mark.asyncio
    async def test_extract_text_plain_success(
        self, mock_get_storage, mock_get_staging, extract_input
    ):
        """Plain text extraction should succeed and write to staging."""
        mock_storage = MagicMock()
        mock_storage.read_file.return_value = b"Hello, world!"
        mock_get_storage.return_value = mock_storage

        mock_staging = MagicMock()
        mock_get_staging.return_value = mock_staging

        from src.temporal.activities.extract import extract_text

        result = await extract_text(extract_input)

        assert isinstance(result, ExtractTextOutput)
        assert result.text_length == 13
        mock_staging.write_text.assert_called_once_with("wf_test_001", "Hello, world!")

    @patch("src.temporal.shared_services.get_staging_service")
    @patch("src.temporal.shared_services.get_storage_service")
    @pytest.mark.asyncio
    async def test_extract_text_strips_nul_bytes_before_staging(
        self, mock_get_storage, mock_get_staging, extract_input
    ):
        """NUL (0x00) bytes must be stripped before writing to Postgres staging.

        Regression for issue #84: Postgres text columns cannot store NUL bytes,
        so extraction succeeds but the staging write crashes permanently. The
        activity must sanitize NUL bytes so the write goes through.
        """
        mock_storage = MagicMock()
        # Extracted text with embedded NUL bytes (as some PDFs decode to).
        mock_storage.read_file.return_value = b"Hello\x00 world\x00!"
        mock_get_storage.return_value = mock_storage

        mock_staging = MagicMock()
        mock_get_staging.return_value = mock_staging

        from src.temporal.activities.extract import extract_text

        result = await extract_text(extract_input)

        # The text written to staging must contain no NUL bytes.
        written_text = mock_staging.write_text.call_args[0][1]
        assert "\x00" not in written_text
        assert written_text == "Hello world!"
        # Reported length reflects the sanitized text that was actually stored.
        assert result.text_length == len("Hello world!")

    @patch("src.temporal.shared_services.get_staging_service")
    @patch("src.temporal.shared_services.get_storage_service")
    @pytest.mark.asyncio
    async def test_extract_text_raises_on_empty_content(
        self, mock_get_storage, mock_get_staging, extract_input
    ):
        """Should raise RuntimeError when extraction yields empty text."""
        mock_storage = MagicMock()
        mock_storage.read_file.return_value = b"   \n   "
        mock_get_storage.return_value = mock_storage

        mock_staging = MagicMock()
        mock_get_staging.return_value = mock_staging

        from src.temporal.activities.extract import extract_text

        with pytest.raises(RuntimeError, match="quality check failed|empty"):
            await extract_text(extract_input)

        # Staging should NOT be written to on failure
        mock_staging.write_text.assert_not_called()

    @patch("src.temporal.shared_services.get_storage_service")
    @pytest.mark.asyncio
    async def test_extract_text_storage_failure_propagates(self, mock_get_storage, extract_input):
        """Storage read failure should propagate, not be silenced."""
        mock_storage = MagicMock()
        mock_storage.read_file.side_effect = FileNotFoundError("No such file")
        mock_get_storage.return_value = mock_storage

        from src.temporal.activities.extract import extract_text

        with pytest.raises(FileNotFoundError, match="No such file"):
            await extract_text(extract_input)

    @patch("src.temporal.shared_services.get_staging_service")
    @patch("src.temporal.shared_services.get_storage_service")
    @pytest.mark.asyncio
    async def test_extract_text_json_success(self, mock_get_storage, mock_get_staging):
        """JSON extraction should parse and pretty-print."""
        mock_storage = MagicMock()
        mock_storage.read_file.return_value = b'{"key": "value"}'
        mock_get_storage.return_value = mock_storage

        mock_staging = MagicMock()
        mock_get_staging.return_value = mock_staging

        input_data = ExtractTextInput(
            workflow_run_id="wf_json",
            storage_backend="local",
            storage_path="data.json",
            content_type="application/json",
            original_filename="data.json",
        )

        from src.temporal.activities.extract import extract_text

        result = await extract_text(input_data)

        assert result.text_length > 0
        written_text = mock_staging.write_text.call_args[0][1]
        assert '"key": "value"' in written_text

    @patch("src.temporal.shared_services.get_staging_service")
    @patch("src.temporal.shared_services.get_storage_service")
    @pytest.mark.asyncio
    async def test_extract_text_xlsx_raises_until_supported(
        self, mock_get_storage, mock_get_staging
    ):
        """Spreadsheet binaries should fail explicitly until XLSX extraction exists."""
        mock_storage = MagicMock()
        mock_storage.read_file.return_value = b"PK\x03\x04fake workbook bytes"
        mock_get_storage.return_value = mock_storage

        mock_staging = MagicMock()
        mock_get_staging.return_value = mock_staging

        input_data = ExtractTextInput(
            workflow_run_id="wf_xlsx",
            storage_backend="local",
            storage_path="sheet.xlsx",
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            original_filename="sheet.xlsx",
        )

        from src.temporal.activities.extract import extract_text

        with pytest.raises(RuntimeError, match="Unsupported spreadsheet"):
            await extract_text(input_data)

        mock_staging.write_text.assert_not_called()

    @patch("src.temporal.shared_services.get_storage_service")
    @pytest.mark.asyncio
    async def test_extract_text_azure_without_url_raises(self, mock_get_storage):
        """Azure backend without storage_url should raise."""
        mock_get_storage.return_value = MagicMock()

        input_data = ExtractTextInput(
            workflow_run_id="wf_azure",
            storage_backend="azure",
            storage_path="doc.txt",
            content_type="text/plain",
            original_filename="doc.txt",
        )

        from src.temporal.activities.extract import extract_text

        with pytest.raises(RuntimeError, match="requires storage_url"):
            await extract_text(input_data)


class TestExtractHelpers:
    """Tests for format-specific extraction helpers."""

    def test_extract_pdf_text_with_pypdf(self):
        """PDF extraction should work with pypdf."""
        import io

        from src.temporal.activities.extract import _extract_pdf_text

        try:
            import pypdf

            # Use a real but minimal PDF to test the path
            writer = pypdf.PdfWriter()
            writer.add_blank_page(width=72, height=72)
            buf = io.BytesIO()
            writer.write(buf)
            pdf_bytes = buf.getvalue()

            result = _extract_pdf_text(pdf_bytes)
            # Blank page produces empty text — that's valid for the helper
            assert isinstance(result, str)
        except ImportError:
            pytest.skip("pypdf not installed")

    def test_extract_html_text_with_beautifulsoup(self):
        """HTML extraction should strip tags."""
        from src.temporal.activities.extract import _extract_html_text

        html = b"<html><body><p>Hello</p><script>alert('x')</script></body></html>"

        try:
            from bs4 import BeautifulSoup  # noqa: F401

            result = _extract_html_text(html)
            assert "Hello" in result
            assert "alert" not in result
        except ImportError:
            # Without bs4, fallback returns raw content
            result = _extract_html_text(html)
            assert "Hello" in result


# =========================================================================
# set_document_status activity tests
# =========================================================================


class TestSetDocumentStatusActivity:
    """Tests for the set_document_status activity (Fix #7)."""

    @patch("src.temporal.shared_services.get_db_service")
    @pytest.mark.asyncio
    async def test_set_status_calls_update_document_status(self, mock_get_db):
        """Activity should delegate to db.update_document_status with the enum."""
        from src.services.database import DocumentStatus
        from src.temporal.activities.status import set_document_status

        mock_db = MagicMock()
        mock_db.update_document_status = AsyncMock(return_value=True)
        mock_get_db.return_value = mock_db

        result = await set_document_status(
            SetDocumentStatusInput(
                document_id="doc_1",
                workspace_id="ws_1",
                status="processing",
            )
        )

        assert result is True
        mock_db.update_document_status.assert_awaited_once_with(
            document_id="doc_1",
            status=DocumentStatus.PROCESSING,
            error_message=None,
        )

    @patch("src.temporal.shared_services.get_db_service")
    @pytest.mark.asyncio
    async def test_set_status_failed_passes_error_message(self, mock_get_db):
        """Failed status should forward the error message."""
        from src.services.database import DocumentStatus
        from src.temporal.activities.status import set_document_status

        mock_db = MagicMock()
        mock_db.update_document_status = AsyncMock(return_value=True)
        mock_get_db.return_value = mock_db

        await set_document_status(
            SetDocumentStatusInput(
                document_id="doc_1",
                workspace_id="ws_1",
                status="failed",
                error_message="boom",
            )
        )

        mock_db.update_document_status.assert_awaited_once_with(
            document_id="doc_1",
            status=DocumentStatus.FAILED,
            error_message="boom",
        )

    @patch("src.temporal.shared_services.get_db_service")
    @pytest.mark.asyncio
    async def test_set_status_noop_when_row_missing(self, mock_get_db):
        """A missing row (UPDATE affects 0 rows) returns False, not an error."""
        from src.temporal.activities.status import set_document_status

        mock_db = MagicMock()
        mock_db.update_document_status = AsyncMock(return_value=False)
        mock_get_db.return_value = mock_db

        result = await set_document_status(
            SetDocumentStatusInput(
                document_id="missing",
                workspace_id="ws_1",
                status="processing",
            )
        )

        assert result is False


# =========================================================================
# store_in_weaviate idempotent reindex tests (Fix #11)
# =========================================================================


class TestStoreInWeaviateReindex:
    """store_in_weaviate must delete stale chunks before writing new ones."""

    def _store_input(self):
        return StoreDocumentInput(
            workflow_run_id="wf_1",
            document_id="doc_1",
            workspace_id="ws_1",
            user_id="user_1",
            filename="f.txt",
            original_filename="f.txt",
            content_type="text/plain",
            size_bytes=10,
            storage_backend="local",
            storage_path="storage/f.txt",
            text_length=10,
            processing_time_ms=5,
        )

    @patch("src.temporal.shared_services.get_db_service")
    @patch("src.temporal.shared_services.get_weaviate_service")
    @patch("src.temporal.shared_services.get_staging_service")
    @pytest.mark.asyncio
    async def test_deletes_before_storing(self, mock_get_staging, mock_get_weaviate, mock_get_db):
        """delete_document_chunks_graceful must be called before store_chunks_with_tenant."""
        from src.temporal.activities.store import store_in_weaviate

        mock_staging = MagicMock()
        mock_staging.read_chunks.return_value = [
            {
                "document_id": "doc_1",
                "content": "chunk text",
                "chunk_index": 0,
                "start_char": 0,
                "end_char": 10,
            }
        ]
        mock_get_staging.return_value = mock_staging

        # Track call ordering across both methods via a shared parent mock.
        manager = MagicMock()
        weaviate = MagicMock()
        weaviate.is_connected.return_value = True
        weaviate.delete_document_chunks_graceful = AsyncMock(return_value=(True, 3))
        weaviate.store_chunks_with_tenant = AsyncMock(return_value=None)
        manager.attach_mock(weaviate.delete_document_chunks_graceful, "delete")
        manager.attach_mock(weaviate.store_chunks_with_tenant, "store")
        mock_get_weaviate.return_value = weaviate

        mock_db = MagicMock()
        mock_db.record_ingestion_event = AsyncMock(return_value=None)
        mock_get_db.return_value = mock_db

        result = await store_in_weaviate(self._store_input())

        assert result.success is True
        weaviate.delete_document_chunks_graceful.assert_awaited_once_with(
            workspace_id="ws_1",
            document_id="doc_1",
            user_id="user_1",
        )
        weaviate.store_chunks_with_tenant.assert_awaited_once()
        # Assert order: delete first, then store.
        assert manager.mock_calls[0] == call.delete(
            workspace_id="ws_1", document_id="doc_1", user_id="user_1"
        )
        method_order = [c[0] for c in manager.mock_calls]
        assert method_order.index("delete") < method_order.index("store")

    @patch("src.temporal.shared_services.get_db_service")
    @patch("src.temporal.shared_services.get_weaviate_service")
    @patch("src.temporal.shared_services.get_staging_service")
    @pytest.mark.asyncio
    async def test_store_proceeds_when_delete_unavailable(
        self, mock_get_staging, mock_get_weaviate, mock_get_db
    ):
        """A graceful-delete failure (returns False) must not block the write."""
        from src.temporal.activities.store import store_in_weaviate

        mock_staging = MagicMock()
        mock_staging.read_chunks.return_value = [
            {
                "document_id": "doc_1",
                "content": "chunk text",
                "chunk_index": 0,
                "start_char": 0,
                "end_char": 10,
            }
        ]
        mock_get_staging.return_value = mock_staging

        weaviate = MagicMock()
        weaviate.is_connected.return_value = True
        weaviate.delete_document_chunks_graceful = AsyncMock(return_value=(False, 0))
        weaviate.store_chunks_with_tenant = AsyncMock(return_value=None)
        mock_get_weaviate.return_value = weaviate

        mock_db = MagicMock()
        mock_db.record_ingestion_event = AsyncMock(return_value=None)
        mock_get_db.return_value = mock_db

        result = await store_in_weaviate(self._store_input())

        assert result.success is True
        weaviate.delete_document_chunks_graceful.assert_awaited_once()
        weaviate.store_chunks_with_tenant.assert_awaited_once()


# =========================================================================
# Model tests
# =========================================================================


class TestUpdatedModels:
    """Tests for updated dataclass models."""

    def test_update_stats_input_has_workflow_run_id(self):
        """UpdateStatsInput should accept optional workflow_run_id."""
        from src.temporal.models import UpdateStatsInput

        # Without workflow_run_id (backwards compatible)
        input1 = UpdateStatsInput(
            workspace_id="ws_1",
            document_delta=1,
            chunk_delta=10,
            size_delta=1000,
        )
        assert input1.workflow_run_id is None

        # With workflow_run_id
        input2 = UpdateStatsInput(
            workspace_id="ws_1",
            document_delta=1,
            chunk_delta=10,
            size_delta=1000,
            workflow_run_id="wf_abc123",
        )
        assert input2.workflow_run_id == "wf_abc123"
