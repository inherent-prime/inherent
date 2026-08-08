"""Tests for Temporal activities.

Covers:
- extract_text: error propagation, empty text guard, format handling

Activities use shared_services getters, so we patch those instead of
constructing service instances directly.
"""

from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
from temporalio.exceptions import ApplicationError

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
    async def test_extract_text_xlsx_corrupt_bytes_raises(self, mock_get_storage, mock_get_staging):
        """#118: XLSX now HAS a FILE_TYPE_REGISTRY entry and a real
        extractor. Garbage bytes (not a real workbook) still fail -- just at
        a different, more specific layer: openpyxl can't open them, wrapped
        as an actionable, non-retryable ApplicationError by
        `_extract_xlsx_text` (review follow-up: this used to be a bare
        RuntimeError, retried 3x by Temporal's default activity RetryPolicy
        for a deterministic, unfixable-by-retry failure -- see
        test_extraction_by_type.py::TestXlsxFailurePaths for the extractor
        unit test this pins end-to-end through the activity)."""
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

        with pytest.raises(ApplicationError, match="XLSX extraction failed") as exc_info:
            await extract_text(input_data)
        assert exc_info.value.non_retryable

        mock_staging.write_text.assert_not_called()

    @patch("src.temporal.shared_services.get_staging_service")
    @patch("src.temporal.shared_services.get_storage_service")
    @pytest.mark.asyncio
    async def test_extract_text_pptx_corrupt_bytes_raises(self, mock_get_storage, mock_get_staging):
        """#119: same shape as the XLSX case above, for PPTX."""
        mock_storage = MagicMock()
        mock_storage.read_file.return_value = b"PK\x03\x04fake deck bytes"
        mock_get_storage.return_value = mock_storage

        mock_staging = MagicMock()
        mock_get_staging.return_value = mock_staging

        input_data = ExtractTextInput(
            workflow_run_id="wf_pptx",
            storage_backend="local",
            storage_path="deck.pptx",
            content_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
            original_filename="deck.pptx",
        )

        from src.temporal.activities.extract import extract_text

        with pytest.raises(ApplicationError, match="PPTX extraction failed") as exc_info:
            await extract_text(input_data)
        assert exc_info.value.non_retryable

        mock_staging.write_text.assert_not_called()

    @patch("src.temporal.shared_services.get_staging_service")
    @patch("src.temporal.shared_services.get_storage_service")
    @pytest.mark.asyncio
    async def test_extract_text_pdf_corrupt_bytes_raises(self, mock_get_storage, mock_get_staging):
        """#195: `_extract_pdf_text` was completely unwrapped -- a corrupt
        PDF raised pypdf's raw exception type directly, and Temporal's
        default 3-attempt RetryPolicy retried it 3x for a deterministic,
        unfixable-by-retry failure. Now wrapped as an actionable,
        non-retryable ApplicationError, same shape as the XLSX/PPTX cases
        above (see test_extraction_by_type.py::TestPdfFailurePaths for the
        extractor unit test this pins end-to-end through the activity)."""
        mock_storage = MagicMock()
        mock_storage.read_file.return_value = b"%PDF-1.4\ngarbage, not a real pdf body"
        mock_get_storage.return_value = mock_storage

        mock_staging = MagicMock()
        mock_get_staging.return_value = mock_staging

        input_data = ExtractTextInput(
            workflow_run_id="wf_pdf",
            storage_backend="local",
            storage_path="doc.pdf",
            content_type="application/pdf",
            original_filename="doc.pdf",
        )

        from src.temporal.activities.extract import extract_text

        with pytest.raises(ApplicationError, match="PDF extraction failed") as exc_info:
            await extract_text(input_data)
        assert exc_info.value.non_retryable

        mock_staging.write_text.assert_not_called()

    @patch("src.temporal.shared_services.get_staging_service")
    @patch("src.temporal.shared_services.get_storage_service")
    @pytest.mark.asyncio
    async def test_extract_text_json_malformed_bytes_raises(
        self, mock_get_storage, mock_get_staging
    ):
        """#195: `_extract_json_text` called `json.loads` unwrapped -- same
        defect class as PDF above, lower severity (JSON corruption is
        rarer/cheaper to retry) but the same fix shape."""
        mock_storage = MagicMock()
        mock_storage.read_file.return_value = b'{"key": "value", "unterminated": '
        mock_get_storage.return_value = mock_storage

        mock_staging = MagicMock()
        mock_get_staging.return_value = mock_staging

        input_data = ExtractTextInput(
            workflow_run_id="wf_json_bad",
            storage_backend="local",
            storage_path="data.json",
            content_type="application/json",
            original_filename="data.json",
        )

        from src.temporal.activities.extract import extract_text

        with pytest.raises(ApplicationError, match="JSON extraction failed") as exc_info:
            await extract_text(input_data)
        assert exc_info.value.non_retryable

        mock_staging.write_text.assert_not_called()

    def test_xlsx_and_pptx_and_docx_failures_classify_as_extraction_failed(self):
        """Review follow-up: `DocumentIngestionWorkflow._classify_error`
        matches on the SUBSTRING "extract" in the lower-cased error message.
        Before this fix, only the two *_TEXT_CAP messages matched (by
        accident, via "extract**ed**") -- the open-failure and cap-breach
        messages did not contain "extract" at all and classified as
        "unknown", which means the dead-letter API's error_type filter would
        never surface an OOXML extraction failure. Every new failure message
        now explicitly says "extraction failed" by construction, not by
        accident -- this pins that against the real classifier. Extended for
        #195's PDF/JSON messages, which follow the identical convention."""
        from src.temporal.workflows.document_ingestion import DocumentIngestionWorkflow

        messages = [
            "XLSX extraction failed: could not open workbook (BadZipFile: File is not a zip file).",
            "XLSX extraction failed: evaluated-cell cap (500000) exceeded while reading sheet 'S1'.",
            "XLSX extraction failed: extracted text exceeds the 5000000-character cap.",
            "PPTX extraction failed: could not open presentation (BadZipFile: File is not a zip file).",
            "PPTX extraction failed: slide cap (5000) exceeded.",
            "PPTX extraction failed: extracted text exceeds the 5000000-character cap.",
            "DOCX extraction failed (report.docx): could not read the document (wrong OOXML content type (...)).",
            "PDF extraction failed: could not read the document (PdfStreamError: Stream has ended unexpectedly).",
            "JSON extraction failed: could not parse the document (Expecting value: line 1 column 1 (char 0)).",
        ]
        for message in messages:
            assert (
                DocumentIngestionWorkflow._classify_error(message) == "extraction_failed"
            ), message

    def test_legacy_xls_content_type_is_unregistered(self):
        """#118: legacy .xls (application/vnd.ms-excel, OLE2 binary format --
        NOT the same format as .xlsx) has no FILE_TYPE_REGISTRY entry, so it
        hits the SAME generic "unregistered content type" non-retryable
        failure #117 established -- never silently mis-parsed as its OOXML
        successor."""
        from src.temporal.activities.extract import _resolve_extractor

        with pytest.raises(ApplicationError, match="No extractor registered") as exc_info:
            _resolve_extractor("application/vnd.ms-excel")
        assert exc_info.value.non_retryable

    def test_legacy_ppt_content_type_is_unregistered(self):
        """#119: same contract as legacy .xls above, for legacy .ppt
        (application/vnd.ms-powerpoint, OLE2 binary format)."""
        from src.temporal.activities.extract import _resolve_extractor

        with pytest.raises(ApplicationError, match="No extractor registered") as exc_info:
            _resolve_extractor("application/vnd.ms-powerpoint")
        assert exc_info.value.non_retryable

    @patch("src.temporal.shared_services.get_staging_service")
    @patch("src.temporal.shared_services.get_storage_service")
    @pytest.mark.asyncio
    async def test_extract_text_yaml_success(self, mock_get_storage, mock_get_staging):
        """#121: YAML is decoded as plain text, no parse step."""
        mock_storage = MagicMock()
        mock_storage.read_file.return_value = b"service: inherent\nversion: 1\n"
        mock_get_storage.return_value = mock_storage
        mock_staging = MagicMock()
        mock_get_staging.return_value = mock_staging

        input_data = ExtractTextInput(
            workflow_run_id="wf_yaml",
            storage_backend="local",
            storage_path="config.yaml",
            content_type="application/yaml",
            original_filename="config.yaml",
        )

        from src.temporal.activities.extract import extract_text

        result = await extract_text(input_data)
        assert result.text_length > 0
        written_text = mock_staging.write_text.call_args[0][1]
        assert "service: inherent" in written_text

    @patch("src.temporal.shared_services.get_staging_service")
    @patch("src.temporal.shared_services.get_storage_service")
    @pytest.mark.asyncio
    async def test_extract_text_malformed_yaml_still_extracts(
        self, mock_get_storage, mock_get_staging
    ):
        """#121 deliberate design: YAML is never parsed, so a syntax error
        does not reject the upload -- the raw (malformed) text is still
        searchable. This is NOT a failure path; it documents the choice."""
        mock_storage = MagicMock()
        # Invalid YAML (unbalanced brackets / bad indentation) -- a real
        # parser would raise; the passthrough extractor does not care.
        mock_storage.read_file.return_value = b"key: [unclosed\n  bad indent: - -\n"
        mock_get_storage.return_value = mock_storage
        mock_staging = MagicMock()
        mock_get_staging.return_value = mock_staging

        input_data = ExtractTextInput(
            workflow_run_id="wf_yaml_bad",
            storage_backend="local",
            storage_path="bad.yaml",
            content_type="application/yaml",
            original_filename="bad.yaml",
        )

        from src.temporal.activities.extract import extract_text

        result = await extract_text(input_data)
        assert result.text_length > 0

    @patch("src.temporal.shared_services.get_staging_service")
    @patch("src.temporal.shared_services.get_storage_service")
    @pytest.mark.asyncio
    async def test_extract_text_xml_strips_tags(self, mock_get_storage, mock_get_staging):
        """#121: XML tags are stripped, element text survives."""
        mock_storage = MagicMock()
        mock_storage.read_file.return_value = (
            b'<?xml version="1.0"?><service name="inherent">'
            b"<description>Company knowledge backend</description></service>"
        )
        mock_get_storage.return_value = mock_storage
        mock_staging = MagicMock()
        mock_get_staging.return_value = mock_staging

        input_data = ExtractTextInput(
            workflow_run_id="wf_xml",
            storage_backend="local",
            storage_path="config.xml",
            content_type="application/xml",
            original_filename="config.xml",
        )

        from src.temporal.activities.extract import extract_text

        result = await extract_text(input_data)
        written_text = mock_staging.write_text.call_args[0][1]
        assert "Company knowledge backend" in written_text
        assert "<service" not in written_text
        # Attribute-value policy (#121 acceptance criteria): the "inherent"
        # attribute VALUE is dropped along with the tag it lived on -- only
        # element text content survives, same as the existing HTML behavior.
        assert "inherent" not in written_text
        assert result.text_length > 0

    @patch("src.temporal.shared_services.get_staging_service")
    @patch("src.temporal.shared_services.get_storage_service")
    @pytest.mark.asyncio
    async def test_extract_text_code_success(self, mock_get_storage, mock_get_staging):
        """#122: source code is decoded text, content_type an explicit alias."""
        mock_storage = MagicMock()
        mock_storage.read_file.return_value = b"def main():\n    print('hello')\n"
        mock_get_storage.return_value = mock_storage
        mock_staging = MagicMock()
        mock_get_staging.return_value = mock_staging

        input_data = ExtractTextInput(
            workflow_run_id="wf_code",
            storage_backend="local",
            storage_path="main.py",
            content_type="text/x-python",
            original_filename="main.py",
        )

        from src.temporal.activities.extract import extract_text

        result = await extract_text(input_data)
        written_text = mock_staging.write_text.call_args[0][1]
        assert "def main" in written_text
        assert result.text_length > 0

    @patch("src.temporal.shared_services.get_staging_service")
    @patch("src.temporal.shared_services.get_storage_service")
    @pytest.mark.asyncio
    async def test_extract_text_code_via_octet_stream_extension_fallback(
        self, mock_get_storage, mock_get_staging
    ):
        """#122: the extension-fallback contract must also hold at
        EXTRACTION time, not just at REST/MCP upload validation -- a
        document persisted with content_type='application/octet-stream'
        (the fallback path's stored value, unchanged from what the client
        declared) must still resolve an extractor via the filename."""
        mock_storage = MagicMock()
        mock_storage.read_file.return_value = b"console.log('hi');\n"
        mock_get_storage.return_value = mock_storage
        mock_staging = MagicMock()
        mock_get_staging.return_value = mock_staging

        input_data = ExtractTextInput(
            workflow_run_id="wf_code_octet",
            storage_backend="local",
            storage_path="app.js",
            content_type="application/octet-stream",
            original_filename="app.js",
        )

        from src.temporal.activities.extract import extract_text

        result = await extract_text(input_data)
        written_text = mock_staging.write_text.call_args[0][1]
        assert "console.log" in written_text
        assert result.text_length > 0

    @patch("src.temporal.shared_services.get_staging_service")
    @patch("src.temporal.shared_services.get_storage_service")
    @pytest.mark.asyncio
    async def test_extract_text_srt_success(self, mock_get_storage, mock_get_staging):
        """#127: SRT cue numbers/timestamps stripped, coarse marker kept."""
        mock_storage = MagicMock()
        mock_storage.read_file.return_value = (
            b"1\n00:00:00,000 --> 00:00:03,000\nWelcome to Inherent.\n\n"
            b"2\n00:00:03,500 --> 00:00:07,000\nLet's begin.\n"
        )
        mock_get_storage.return_value = mock_storage
        mock_staging = MagicMock()
        mock_get_staging.return_value = mock_staging

        input_data = ExtractTextInput(
            workflow_run_id="wf_srt",
            storage_backend="local",
            storage_path="talk.srt",
            content_type="application/x-subrip",
            original_filename="talk.srt",
        )

        from src.temporal.activities.extract import extract_text

        result = await extract_text(input_data)
        written_text = mock_staging.write_text.call_args[0][1]
        assert "Welcome to Inherent." in written_text
        assert "Let's begin." in written_text
        assert "-->" not in written_text
        assert "[t=00:00]" in written_text
        assert result.text_length > 0

    @patch("src.temporal.shared_services.get_staging_service")
    @patch("src.temporal.shared_services.get_storage_service")
    @pytest.mark.asyncio
    async def test_extract_text_srt_no_timestamps_raises(self, mock_get_storage, mock_get_staging):
        """#127 failure path: an SRT-labeled file with no cue timestamps at
        all must fail the document, not silently emit garbage/empty text.
        #206: deterministic given fixed content bytes -- must be a
        non-retryable ApplicationError, not a bare RuntimeError Temporal
        retries 3x for no reason."""
        mock_storage = MagicMock()
        mock_storage.read_file.return_value = b"This is just prose, not a real SRT file.\n"
        mock_get_storage.return_value = mock_storage
        mock_staging = MagicMock()
        mock_get_staging.return_value = mock_staging

        input_data = ExtractTextInput(
            workflow_run_id="wf_srt_bad",
            storage_backend="local",
            storage_path="not-really.srt",
            content_type="application/x-subrip",
            original_filename="not-really.srt",
        )

        from src.temporal.activities.extract import extract_text

        with pytest.raises(ApplicationError, match="no subtitle cues") as exc_info:
            await extract_text(input_data)
        assert exc_info.value.non_retryable
        mock_staging.write_text.assert_not_called()

    @patch("src.config.settings.get_settings")
    @patch("src.temporal.shared_services.get_storage_service")
    @pytest.mark.asyncio
    async def test_extract_text_azure_without_url_raises(self, mock_get_storage, mock_get_settings):
        """Azure backend without storage_url should raise -- with the #214
        url-based-ingestion gate explicitly enabled, so this test still
        exercises the url-required validation it was written for instead of
        being short-circuited by the (now default-off) gate. The gate's own
        default-off behavior is covered separately in
        tests/test_url_based_ingestion_gate.py."""
        mock_get_storage.return_value = MagicMock()
        mock_get_settings.return_value = MagicMock(allow_url_based_ingestion=True)

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
# #127: SRT / WebVTT subtitle extraction
# =========================================================================


class TestExtractSubtitleText:
    """Tests for `_extract_subtitle_text`, shared by the 'srt' and 'vtt'
    EXTRACTORS entries -- both formats share the same cue shape (an
    optional identifier line, a 'HH:MM:SS[.,]mmm --> HH:MM:SS[.,]mmm'
    timestamp line, then text) closely enough for one parser."""

    def test_srt_strips_cue_numbers_and_timestamps(self):
        from src.temporal.activities.extract import _extract_subtitle_text

        srt = (
            b"1\n00:00:00,000 --> 00:00:03,000\nWelcome to Inherent.\n\n"
            b"2\n00:00:03,500 --> 00:00:07,000\nLet's begin.\n"
        )
        result = _extract_subtitle_text(srt, "talk.srt")
        assert "Welcome to Inherent." in result
        assert "Let's begin." in result
        # Zero cue-number noise (#127 acceptance criteria).
        assert "1\n" not in result and result.strip() != "1"
        assert "-->" not in result

    def test_vtt_strips_header_and_cue_settings(self):
        from src.temporal.activities.extract import _extract_subtitle_text

        vtt = (
            b"WEBVTT\n\n"
            b"00:00:00.000 --> 00:00:03.000 align:start position:10%\n"
            b"Welcome to Inherent.\n\n"
            b"00:00:03.500 --> 00:00:07.000\n"
            b"Let's begin.\n"
        )
        result = _extract_subtitle_text(vtt, "talk.vtt")
        assert "Welcome to Inherent." in result
        assert "Let's begin." in result
        assert "WEBVTT" not in result
        assert "align:start" not in result

    def test_vtt_note_blocks_are_skipped(self):
        from src.temporal.activities.extract import _extract_subtitle_text

        vtt = (
            b"WEBVTT\n\n"
            b"NOTE This is a comment, not a cue.\n\n"
            b"00:00:00.000 --> 00:00:03.000\n"
            b"Real cue text.\n"
        )
        result = _extract_subtitle_text(vtt, "talk.vtt")
        assert "Real cue text." in result
        assert "This is a comment" not in result

    def test_periodic_coarse_timestamp_markers(self):
        """Every Nth cue gets a '[t=MM:SS]' marker; the rest don't -- coarse
        citability without per-line timestamp noise polluting embeddings."""
        from src.temporal.activities.extract import (
            _TIMESTAMP_MARKER_EVERY_N_CUES,
            _extract_subtitle_text,
        )

        cues = "".join(
            f"{i + 1}\n00:00:{i:02d},000 --> 00:00:{i + 1:02d},000\nCue number {i}.\n\n"
            for i in range(_TIMESTAMP_MARKER_EVERY_N_CUES + 1)
        )
        result = _extract_subtitle_text(cues.encode(), "long.srt")
        assert result.count("[t=") == 2  # cue 0 and cue N both marked
        assert "[t=00:00]" in result

    def test_mixed_line_endings_do_not_break_cue_parsing(self):
        """#127 failure-adjacent robustness case: CRLF, bare CR, and LF
        mixed in the same file must not fragment a cue's text or hide a
        cue behind a spurious blank-line split."""
        from src.temporal.activities.extract import _extract_subtitle_text

        srt = (
            b"1\r\n00:00:00,000 --> 00:00:03,000\r\nWelcome to Inherent.\r\n\r\n"
            b"2\r00:00:03,500 --> 00:00:07,000\rLet's begin.\r\r"
            b"3\n00:00:07,500 --> 00:00:10,000\nAll done.\n"
        )
        result = _extract_subtitle_text(srt, "mixed.srt")
        assert "Welcome to Inherent." in result
        assert "Let's begin." in result
        assert "All done." in result

    def test_no_timestamps_raises(self):
        """#127 failure path: content with no cue timestamps at all is not
        a valid subtitle file -- fail loudly rather than emit nothing.
        #206: non-retryable -- deterministic given fixed content bytes."""
        from src.temporal.activities.extract import _extract_subtitle_text

        with pytest.raises(ApplicationError, match="no subtitle cues") as exc_info:
            _extract_subtitle_text(b"just some prose, no cues here", "fake.srt")
        assert exc_info.value.non_retryable

    def test_multiline_cue_text_is_joined(self):
        from src.temporal.activities.extract import _extract_subtitle_text

        srt = b"1\n00:00:00,000 --> 00:00:03,000\nLine one\nLine two\n"
        result = _extract_subtitle_text(srt, "talk.srt")
        assert "Line one Line two" in result


# =========================================================================
# set_document_status activity tests
# =========================================================================


class TestFileTypeRegistryDispatch:
    """#117: extraction dispatch is driven by inh_contracts.FILE_TYPE_REGISTRY
    instead of an if/elif content-type chain. Two failure modes the review
    note calls out explicitly, both now actionable RuntimeErrors instead of
    a silent lossy decode or a bare KeyError crash.
    """

    def test_every_registry_extractor_key_is_wired(self):
        """Every FILE_TYPE_REGISTRY entry's `extractor` key must resolve to a
        real function in EXTRACTORS -- a registry entry with no extractor
        wired up is exactly the gap #117 calls out. This is the sibling-issue
        tripwire: #118 (XLSX) etc. adding a FileTypeSpec without also adding
        its EXTRACTORS entry fails THIS test, not a confusing prod KeyError.
        """
        from inh_contracts.file_types import FILE_TYPE_REGISTRY

        from src.temporal.activities.extract import EXTRACTORS

        missing = [spec.key for spec in FILE_TYPE_REGISTRY if spec.extractor not in EXTRACTORS]
        assert not missing, f"FILE_TYPE_REGISTRY entries with no EXTRACTORS wiring: {missing}"

    def test_unregistered_content_type_raises_actionable_error(self):
        """A content type with no FILE_TYPE_REGISTRY entry at all fails with
        a message naming the offending type and the supported set -- never a
        silent decode-and-hope. Non-retryable (#117 review item 13): the
        same content_type fails identically on every retry."""
        from src.temporal.activities.extract import _resolve_extractor

        with pytest.raises(ApplicationError, match="application/x-made-up") as exc_info:
            _resolve_extractor("application/x-made-up")
        assert exc_info.value.non_retryable

    def test_registry_entry_with_unwired_extractor_fails_loudly(self, monkeypatch):
        """The second failure mode: a VALID registry entry whose `extractor`
        key has no matching EXTRACTORS function. This must never surface as
        a bare KeyError (a confusing crash in Temporal's activity worker) --
        it's a wiring bug, and the message must say so. Non-retryable: a
        wiring bug cannot be fixed by retrying."""
        import src.temporal.activities.extract as extract_module

        monkeypatch.setattr(extract_module, "EXTRACTORS", {})  # simulate the gap

        with pytest.raises(ApplicationError, match="wiring") as exc_info:
            extract_module._resolve_extractor("application/pdf")
        assert exc_info.value.non_retryable

    def test_correctly_registered_type_resolves(self):
        from src.temporal.activities.extract import _resolve_extractor

        extractor = _resolve_extractor("application/pdf")
        assert callable(extractor)

    def test_octet_stream_resolves_via_filename_extension(self):
        """#122: extraction must honor the SAME octet-stream + extension
        fallback contract intake validation does (both call
        `inh_contracts.get_spec_for_upload`) -- otherwise a document that
        was ACCEPTED at upload via the fallback would permanently fail at
        extraction, since 'application/octet-stream' alone has no registry
        entry."""
        from src.temporal.activities.extract import _resolve_extractor

        extractor = _resolve_extractor("application/octet-stream", "main.py")
        assert callable(extractor)

    def test_octet_stream_without_filename_still_fails(self):
        """No filename to fall back on -- the original #117 behavior for a
        generic content type is preserved."""
        from src.temporal.activities.extract import _resolve_extractor

        with pytest.raises(ApplicationError, match="application/octet-stream"):
            _resolve_extractor("application/octet-stream")


class TestDecodeText:
    """#117: text decode now uses charset-normalizer instead of
    `errors="ignore"`, which silently DROPPED any byte that wasn't valid
    UTF-8 -- data loss with no signal. These pin that bytes are never
    silently vanished anymore.
    """

    def test_valid_utf8_decodes_unchanged(self):
        from src.temporal.activities.extract import _decode_text

        assert _decode_text(b"Hello, world!") == "Hello, world!"

    def test_embedded_nul_bytes_preserved(self):
        """Regression guard: strict UTF-8 (tried first) must keep decoding
        NUL-containing-but-otherwise-valid-UTF-8 content correctly, so the
        activity's own downstream NUL-stripping (#84) still has literal
        \\x00 characters to find and strip."""
        from src.temporal.activities.extract import _decode_text

        assert _decode_text(b"Hello\x00 world\x00!") == "Hello\x00 world\x00!"

    def test_non_utf8_bytes_never_silently_dropped(self):
        """The exact regression #117 fixes: compare directly against what
        the OLD `errors="ignore"` behavior produced. 0xE9 alone is not valid
        UTF-8 (a continuation byte with no lead byte); `errors="ignore"`
        silently deleted it. `_decode_text` must not reproduce that byte
        loss -- it either decodes the byte via charset detection or replaces
        it with a visible marker, but the surrounding text is never mangled
        or shortened by a vanished byte."""
        from src.temporal.activities.extract import _decode_text

        raw = b"caf\xe9 report"
        silently_dropped = raw.decode("utf-8", errors="ignore")
        assert silently_dropped == "caf report"  # the old, lossy behavior

        result = _decode_text(raw)
        assert result != silently_dropped
        assert "caf" in result
        assert "report" in result


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
        # workflow_run_id defaults to None (unfenced) when the caller doesn't
        # supply one -- the real workflow always supplies it (#110 follow-up).
        mock_db.update_document_status.assert_awaited_once_with(
            document_id="doc_1",
            status=DocumentStatus.PROCESSING,
            error_message=None,
            workflow_run_id=None,
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
            workflow_run_id=None,
        )

    @patch("src.temporal.shared_services.get_db_service")
    @pytest.mark.asyncio
    async def test_set_status_forwards_workflow_run_id_for_fencing(self, mock_get_db):
        """(#110 follow-up) When the caller supplies workflow_run_id (the
        real workflow always does), it must reach update_document_status so
        the write is fenced -- a terminated run's stale status write must
        not be able to land after a newer run finished."""
        from src.services.database import DocumentStatus
        from src.temporal.activities.status import set_document_status

        mock_db = MagicMock()
        mock_db.update_document_status = AsyncMock(return_value=True)
        mock_get_db.return_value = mock_db

        await set_document_status(
            SetDocumentStatusInput(
                document_id="doc_1",
                workspace_id="ws_1",
                status="processing",
                workflow_run_id="run-xyz",
            )
        )

        mock_db.update_document_status.assert_awaited_once_with(
            document_id="doc_1",
            status=DocumentStatus.PROCESSING,
            error_message=None,
            workflow_run_id="run-xyz",
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
        # (#110) fencing pre-check must pass so the test's actual concern
        # (delete-before-store ordering) is reached at all.
        mock_db.is_active_run = AsyncMock(return_value=True)
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
        # (#110) fencing pre-check must pass so the test's actual concern
        # (delete-unavailable still proceeds to store) is reached at all.
        mock_db.is_active_run = AsyncMock(return_value=True)
        mock_get_db.return_value = mock_db

        result = await store_in_weaviate(self._store_input())

        assert result.success is True
        weaviate.delete_document_chunks_graceful.assert_awaited_once()
        weaviate.store_chunks_with_tenant.assert_awaited_once()


# =========================================================================
# Retry-honouring failure tests (Fix #2)
# =========================================================================


class TestStoreAndTenantRaiseOnFailure:
    """Store/tenant activities must RAISE on failure so Temporal's RetryPolicy
    fires. Returning success=False / tenant_id=None is a *successful* activity
    completion in Temporal's eyes → no retry, instant dead-letter, and (for
    tenant) a NULL-tenant document. A transient DB/Weaviate blip on attempt 1
    must be retried, not immediately dead-lettered."""

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

    def _one_chunk(self):
        return [
            {
                "document_id": "doc_1",
                "content": "chunk text",
                "chunk_index": 0,
                "start_char": 0,
                "end_char": 10,
            }
        ]

    @patch("src.temporal.shared_services.get_db_service")
    @patch("src.temporal.shared_services.get_staging_service")
    @pytest.mark.asyncio
    async def test_store_in_postgresql_raises_on_db_failure(self, mock_get_staging, mock_get_db):
        from src.temporal.activities.store import store_in_postgresql

        mock_staging = MagicMock()
        mock_staging.read_chunks.return_value = self._one_chunk()
        mock_get_staging.return_value = mock_staging

        mock_db = MagicMock()
        mock_db.store_processed_document = AsyncMock(
            side_effect=RuntimeError("transient pg connection reset")
        )
        mock_db.record_ingestion_event = AsyncMock(return_value=None)
        mock_get_db.return_value = mock_db

        # Must propagate so Temporal retries (not swallow into success=False).
        with pytest.raises(RuntimeError, match="transient pg connection reset"):
            await store_in_postgresql(self._store_input())
        # Failure lineage still recorded before re-raising.
        mock_db.record_ingestion_event.assert_awaited()

    @patch("src.temporal.shared_services.get_db_service")
    @patch("src.temporal.shared_services.get_weaviate_service")
    @patch("src.temporal.shared_services.get_staging_service")
    @pytest.mark.asyncio
    async def test_store_in_weaviate_raises_on_store_failure(
        self, mock_get_staging, mock_get_weaviate, mock_get_db
    ):
        from src.temporal.activities.store import store_in_weaviate

        mock_staging = MagicMock()
        mock_staging.read_chunks.return_value = self._one_chunk()
        mock_get_staging.return_value = mock_staging

        weaviate = MagicMock()
        weaviate.is_connected.return_value = True
        weaviate.delete_document_chunks_graceful = AsyncMock(return_value=(True, 0))
        weaviate.store_chunks_with_tenant = AsyncMock(side_effect=RuntimeError("weaviate 503"))
        mock_get_weaviate.return_value = weaviate

        mock_db = MagicMock()
        mock_db.record_ingestion_event = AsyncMock(return_value=None)
        # (#110) fencing pre-check must pass so the test reaches the actual
        # store call whose failure it's asserting on.
        mock_db.is_active_run = AsyncMock(return_value=True)
        mock_get_db.return_value = mock_db

        with pytest.raises(RuntimeError, match="weaviate 503"):
            await store_in_weaviate(self._store_input())

    @patch("src.temporal.shared_services.get_db_service")
    @patch("src.temporal.shared_services.get_weaviate_service")
    @pytest.mark.asyncio
    async def test_ensure_tenant_ready_raises_on_failure(self, mock_get_weaviate, mock_get_db):
        from src.temporal.activities.tenant import ensure_tenant_ready
        from src.temporal.models import EnsureTenantInput

        mock_db = MagicMock()
        mock_db.record_ingestion_event = AsyncMock(return_value=None)
        mock_get_db.return_value = mock_db
        mock_get_weaviate.return_value = MagicMock()

        # TenantManager and get_settings are imported inside the activity body.
        with (
            patch("src.services.tenant_manager.TenantManager") as mock_tm_cls,
            patch("src.config.settings.get_settings", return_value=MagicMock()),
        ):
            instance = mock_tm_cls.return_value
            instance.ensure_workspace_ready = AsyncMock(
                side_effect=RuntimeError("tenant bootstrap failed")
            )
            # Must raise so the 3-attempt RetryPolicy fires instead of
            # silently returning tenant_id=None (NULL-tenant attribution).
            with pytest.raises(RuntimeError, match="tenant bootstrap failed"):
                await ensure_tenant_ready(
                    EnsureTenantInput(
                        workspace_id="ws_1",
                        user_id="user_1",
                        workflow_run_id="wf_1",
                        document_id="doc_1",
                    )
                )
        # Failure lineage still recorded before re-raising.
        mock_db.record_ingestion_event.assert_awaited()


# =========================================================================
# Superseded-run handling (#110 blocker 1)
# =========================================================================
#
# When a fenced write is rejected (a newer workflow run has since claimed
# the document -- see DatabaseService.store_processed_document /
# is_active_run), the store activities must return normally with
# StoreDocumentOutput(success=False, superseded=True) rather than raise.
# Raising would trigger Temporal's RetryPolicy to retry an outcome that can
# never change (the newer run's claim doesn't go away), and by the time this
# happens the owning workflow has typically already been terminated anyway,
# so nothing is listening for a retry to matter.


class TestStoreActivitiesSupersededHandling:
    def _store_input(self):
        return StoreDocumentInput(
            workflow_run_id="wf_stale",
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

    def _one_chunk(self):
        return [
            {
                "document_id": "doc_1",
                "content": "chunk text",
                "chunk_index": 0,
                "start_char": 0,
                "end_char": 10,
            }
        ]

    @patch("src.temporal.shared_services.get_db_service")
    @patch("src.temporal.shared_services.get_staging_service")
    @pytest.mark.asyncio
    async def test_store_in_postgresql_returns_superseded_without_raising(
        self, mock_get_staging, mock_get_db
    ):
        """store_processed_document returning None (fenced out) must produce
        a normal, non-raising StoreDocumentOutput(superseded=True) -- NOT an
        exception (which would trigger a pointless RetryPolicy retry, #110)."""
        from src.temporal.activities.store import store_in_postgresql

        mock_staging = MagicMock()
        mock_staging.read_chunks.return_value = self._one_chunk()
        mock_get_staging.return_value = mock_staging

        mock_db = MagicMock()
        mock_db.store_processed_document = AsyncMock(return_value=None)  # fenced out
        mock_db.record_ingestion_event = AsyncMock(return_value=None)
        mock_get_db.return_value = mock_db

        result = await store_in_postgresql(self._store_input())

        assert result.success is False
        assert result.superseded is True
        assert result.error == "superseded_by_newer_workflow_run"
        # Lineage still recorded, as "superseded" not "failed" -- this is
        # not an error condition, just a benign no-op.
        mock_db.record_ingestion_event.assert_awaited_once()
        assert mock_db.record_ingestion_event.await_args.kwargs["status"] == "superseded"

    @patch("src.temporal.shared_services.get_db_service")
    @patch("src.temporal.shared_services.get_weaviate_service")
    @patch("src.temporal.shared_services.get_staging_service")
    @pytest.mark.asyncio
    async def test_store_in_weaviate_skips_write_when_fenced_out(
        self, mock_get_staging, mock_get_weaviate, mock_get_db
    ):
        """is_active_run() returning False must skip the destructive
        delete+write entirely -- this is the check that narrows Weaviate's
        TOCTOU window (it has no transactional WHERE-on-write like
        Postgres's conditional UPSERT)."""
        from src.temporal.activities.store import store_in_weaviate

        mock_staging = MagicMock()
        mock_staging.read_chunks.return_value = self._one_chunk()
        mock_get_staging.return_value = mock_staging

        weaviate = MagicMock()
        weaviate.is_connected.return_value = True
        weaviate.delete_document_chunks_graceful = AsyncMock(return_value=(True, 0))
        weaviate.store_chunks_with_tenant = AsyncMock(return_value=None)
        mock_get_weaviate.return_value = weaviate

        mock_db = MagicMock()
        mock_db.is_active_run = AsyncMock(return_value=False)  # fenced out
        mock_db.record_ingestion_event = AsyncMock(return_value=None)
        mock_get_db.return_value = mock_db

        result = await store_in_weaviate(self._store_input())

        assert result.success is False
        assert result.superseded is True
        # The whole point: neither the delete nor the write happened.
        weaviate.delete_document_chunks_graceful.assert_not_awaited()
        weaviate.store_chunks_with_tenant.assert_not_awaited()

    @patch("src.temporal.shared_services.get_db_service")
    @patch("src.temporal.shared_services.get_weaviate_service")
    @patch("src.temporal.shared_services.get_staging_service")
    @pytest.mark.asyncio
    async def test_store_in_weaviate_proceeds_when_not_fenced_out(
        self, mock_get_staging, mock_get_weaviate, mock_get_db
    ):
        """Sanity check for the happy path: is_active_run() True (the
        normal, non-superseded case) must not block the write."""
        from src.temporal.activities.store import store_in_weaviate

        mock_staging = MagicMock()
        mock_staging.read_chunks.return_value = self._one_chunk()
        mock_get_staging.return_value = mock_staging

        weaviate = MagicMock()
        weaviate.is_connected.return_value = True
        weaviate.delete_document_chunks_graceful = AsyncMock(return_value=(True, 0))
        weaviate.store_chunks_with_tenant = AsyncMock(return_value=None)
        mock_get_weaviate.return_value = weaviate

        mock_db = MagicMock()
        mock_db.is_active_run = AsyncMock(return_value=True)
        mock_db.record_ingestion_event = AsyncMock(return_value=None)
        mock_get_db.return_value = mock_db

        result = await store_in_weaviate(self._store_input())

        assert result.success is True
        assert result.superseded is False
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
