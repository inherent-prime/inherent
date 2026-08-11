"""Tests for image OCR extraction with graceful fallback (#61, #120).

Covers both the Temporal activity helper (``_extract_image_text`` in
``extract.py``) and the processor method (``_extract_image_text`` in
``processor.py``, which delegates to the activity helper). OCR is mocked
so these run WITHOUT the real tesseract system binary installed:

- OCR available  -> ``pytesseract.image_to_string`` returns text, which is
  returned verbatim (single-frame) or joined with ``## Page N`` markers
  (multi-frame TIFF).
- OCR unavailable -> ImportError of the OCR libs, a missing tesseract binary
  (``TesseractNotFoundError``), or empty OCR output all fall back to a
  placeholder string instead of raising.
"""

from __future__ import annotations

import sys
import types
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from src.config.settings import Settings
from src.models.document import DocumentUploadMessage
from src.services.processor import DocumentProcessor
from src.temporal.activities.extract import _MAX_IMAGE_OCR_PAGES, _extract_image_text

PNG_BYTES = b"\x89PNG\r\n\x1a\n fake png bytes"
JPEG_BYTES = b"\xff\xd8\xff fake jpeg bytes"
WEBP_BYTES = b"RIFF\x00\x00\x00\x00WEBP fake webp bytes"
TIFF_LE_BYTES = b"II*\x00 fake tiff bytes"
BMP_BYTES = b"BM fake bmp bytes"
FILENAME = "scan.png"
PLACEHOLDER = f"[image: {FILENAME}, no text extracted]"


@pytest.fixture(autouse=True)
def cleanup_test_data():
    """Override the DB-backed root autouse fixture so these stay offline.

    These OCR tests need neither PostgreSQL nor any live service; shadowing
    the root ``cleanup_test_data`` (which skips when PostgreSQL is down) lets
    them run unconditionally, including in local dev without Docker.
    """
    yield


def _install_fake_ocr(
    monkeypatch: pytest.MonkeyPatch,
    *,
    return_text: str = "",
    image_to_string_exc: type[BaseException] | None = None,
    n_frames: int = 1,
    page_texts: list[str] | None = None,
) -> None:
    """Install fake ``pytesseract`` and ``PIL`` modules into sys.modules.

    Args:
        return_text: Text the fake ``image_to_string`` returns for single-frame.
        image_to_string_exc: If set, ``image_to_string`` raises this instead.
        n_frames: Simulated ``Image.n_frames`` (``>1`` exercises multi-page TIFF).
        page_texts: Per-page OCR strings when ``n_frames > 1``. Defaults to
            repeating ``return_text`` for each frame.
    """

    class TesseractNotFoundError(Exception):
        pass

    fake_pytesseract = types.ModuleType("pytesseract")
    fake_pytesseract.TesseractNotFoundError = TesseractNotFoundError

    texts = page_texts if page_texts is not None else [return_text] * max(n_frames, 1)
    call_count = {"n": 0}

    def _image_to_string(_image):
        if image_to_string_exc is not None:
            raise image_to_string_exc("simulated tesseract failure")
        idx = min(call_count["n"], len(texts) - 1)
        call_count["n"] += 1
        return texts[idx]

    fake_pytesseract.image_to_string = _image_to_string

    class _FakeImage:
        def __init__(self):
            self.n_frames = n_frames

    fake_pil = types.ModuleType("PIL")
    fake_pil_image = types.ModuleType("PIL.Image")
    fake_pil_sequence = types.ModuleType("PIL.ImageSequence")

    def _open(_fp):
        return _FakeImage()

    def _iterator(image):
        for _ in range(getattr(image, "n_frames", 1)):
            yield object()

    fake_pil_image.open = _open
    fake_pil_sequence.Iterator = _iterator
    fake_pil.Image = fake_pil_image
    fake_pil.ImageSequence = fake_pil_sequence

    monkeypatch.setitem(sys.modules, "pytesseract", fake_pytesseract)
    monkeypatch.setitem(sys.modules, "PIL", fake_pil)
    monkeypatch.setitem(sys.modules, "PIL.Image", fake_pil_image)
    monkeypatch.setitem(sys.modules, "PIL.ImageSequence", fake_pil_sequence)


def _block_ocr_imports(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force ImportError for the OCR libraries to simulate them not installed."""
    import builtins

    real_import = builtins.__import__

    def _fake_import(name, *args, **kwargs):
        if name in ("pytesseract", "PIL") or name.startswith("PIL."):
            raise ImportError(f"No module named {name!r}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _fake_import)


# ---------------------------------------------------------------------------
# Activity helper: extract.py::_extract_image_text
# ---------------------------------------------------------------------------


class TestActivityImageOCR:
    def test_ocr_available_returns_text(self, monkeypatch):
        _install_fake_ocr(monkeypatch, return_text="Inherent OCR sample")
        text = _extract_image_text(PNG_BYTES, FILENAME)
        assert text == "Inherent OCR sample"

    def test_ocr_libs_missing_returns_placeholder(self, monkeypatch):
        _block_ocr_imports(monkeypatch)
        text = _extract_image_text(PNG_BYTES, FILENAME)
        assert text == PLACEHOLDER

    def test_tesseract_binary_missing_returns_placeholder(self, monkeypatch):
        _install_fake_ocr(monkeypatch)
        from pytesseract import TesseractNotFoundError  # the fake one

        _install_fake_ocr(monkeypatch, image_to_string_exc=TesseractNotFoundError)
        text = _extract_image_text(PNG_BYTES, FILENAME)
        assert text == PLACEHOLDER

    def test_empty_ocr_output_returns_placeholder(self, monkeypatch):
        _install_fake_ocr(monkeypatch, return_text="   \n  ")
        text = _extract_image_text(PNG_BYTES, FILENAME)
        assert text == PLACEHOLDER

    def test_unexpected_ocr_error_returns_placeholder(self, monkeypatch):
        _install_fake_ocr(monkeypatch, image_to_string_exc=ValueError)
        text = _extract_image_text(PNG_BYTES, FILENAME)
        assert text == PLACEHOLDER

    @pytest.mark.parametrize(
        ("content", "filename"),
        [
            (JPEG_BYTES, "scan.jpg"),
            (WEBP_BYTES, "scan.webp"),
            (TIFF_LE_BYTES, "scan.tiff"),
            (BMP_BYTES, "scan.bmp"),
        ],
        ids=["jpeg", "webp", "tiff", "bmp"],
    )
    def test_sibling_formats_ocr_and_placeholder(self, monkeypatch, content, filename):
        """#120: JPEG/WebP/TIFF/BMP share PNG's OCR success + placeholder paths."""
        _install_fake_ocr(monkeypatch, return_text="Sibling OCR text")
        assert _extract_image_text(content, filename) == "Sibling OCR text"

        _block_ocr_imports(monkeypatch)
        assert _extract_image_text(content, filename) == (
            f"[image: {filename}, no text extracted]"
        )

    def test_multipage_tiff_joins_pages_with_markers(self, monkeypatch):
        """#120: multi-frame TIFF yields per-page text with ``## Page N`` markers."""
        _install_fake_ocr(
            monkeypatch,
            n_frames=3,
            page_texts=["Page one text", "Page two text", "Page three text"],
        )
        text = _extract_image_text(TIFF_LE_BYTES, "multi.tiff")
        assert "## Page 1\nPage one text" in text
        assert "## Page 2\nPage two text" in text
        assert "## Page 3\nPage three text" in text

    def test_multipage_tiff_all_empty_returns_placeholder(self, monkeypatch):
        _install_fake_ocr(monkeypatch, n_frames=2, page_texts=["", "  "])
        text = _extract_image_text(TIFF_LE_BYTES, "empty.tiff")
        assert text == "[image: empty.tiff, no text extracted]"

    def test_multipage_tiff_respects_page_cap(self, monkeypatch):
        """#120: OCR stops after ``_MAX_IMAGE_OCR_PAGES`` frames."""
        n_frames = _MAX_IMAGE_OCR_PAGES + 5
        page_texts = [f"text-{i}" for i in range(1, n_frames + 1)]
        _install_fake_ocr(monkeypatch, n_frames=n_frames, page_texts=page_texts)
        text = _extract_image_text(TIFF_LE_BYTES, "huge.tiff")
        assert f"## Page {_MAX_IMAGE_OCR_PAGES}\ntext-{_MAX_IMAGE_OCR_PAGES}" in text
        assert f"## Page {_MAX_IMAGE_OCR_PAGES + 1}" not in text


# ---------------------------------------------------------------------------
# Processor method: processor.py::_extract_image_text (via _extract_text)
# ---------------------------------------------------------------------------


class TestProcessorImageOCR:
    @pytest.fixture
    def processor(self):
        settings = MagicMock(spec=Settings)
        settings.max_chunk_size = 1000
        settings.chunk_overlap = 200
        settings.chunking_strategy = "tokens"
        settings.database_url = "postgresql://mock:mock@localhost:5432/mock"
        proc = DocumentProcessor(settings)
        proc._initialized = True
        return proc

    def _message(self, *, content_type: str = "image/png", filename: str = FILENAME) -> DocumentUploadMessage:
        return DocumentUploadMessage(
            event_type="document.uploaded",
            document_id="doc-png-1",
            workspace_id="ws-1",
            user_id="user-1",
            filename=filename,
            original_filename=filename,
            content_type=content_type,
            size_bytes=100,
            storage_backend="local",
            storage_path=f"ws-1/doc-png-1/{filename}",
            storage_bucket="bucket",
            timestamp=datetime.now(UTC).isoformat(),
        )

    @pytest.mark.asyncio
    async def test_ocr_available_returns_text(self, processor, monkeypatch):
        _install_fake_ocr(monkeypatch, return_text="Inherent OCR sample")
        text = await processor._extract_text(PNG_BYTES, self._message())
        assert text == "Inherent OCR sample"

    @pytest.mark.asyncio
    async def test_ocr_libs_missing_returns_placeholder(self, processor, monkeypatch):
        _block_ocr_imports(monkeypatch)
        text = await processor._extract_text(PNG_BYTES, self._message())
        assert text == PLACEHOLDER

    @pytest.mark.asyncio
    async def test_tesseract_binary_missing_returns_placeholder(self, processor, monkeypatch):
        _install_fake_ocr(monkeypatch)
        from pytesseract import TesseractNotFoundError  # the fake one

        _install_fake_ocr(monkeypatch, image_to_string_exc=TesseractNotFoundError)
        text = await processor._extract_text(PNG_BYTES, self._message())
        assert text == PLACEHOLDER

    @pytest.mark.asyncio
    async def test_empty_ocr_output_returns_placeholder(self, processor, monkeypatch):
        _install_fake_ocr(monkeypatch, return_text="")
        text = await processor._extract_text(PNG_BYTES, self._message())
        assert text == PLACEHOLDER

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("content", "content_type", "filename"),
        [
            (JPEG_BYTES, "image/jpeg", "scan.jpg"),
            (WEBP_BYTES, "image/webp", "scan.webp"),
            (TIFF_LE_BYTES, "image/tiff", "scan.tiff"),
            (BMP_BYTES, "image/bmp", "scan.bmp"),
        ],
        ids=["jpeg", "webp", "tiff", "bmp"],
    )
    async def test_sibling_formats_routed_to_ocr(
        self, processor, monkeypatch, content, content_type, filename
    ):
        """#120: legacy processor routes the new image MIMEs to OCR."""
        _install_fake_ocr(monkeypatch, return_text="Processor sibling OCR")
        text = await processor._extract_text(
            content, self._message(content_type=content_type, filename=filename)
        )
        assert text == "Processor sibling OCR"
