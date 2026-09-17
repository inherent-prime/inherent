"""Tests for image OCR extraction with graceful fallback (#61, #120).

Covers the Temporal activity helper (``_extract_image_text`` in
``extract.py``) -- the live ingestion path's OCR extraction. This file used
to also cover the equivalent method on the legacy ``DocumentProcessor``
(``processor.py``); that class was removed (#185) once its OCR behaviour was
confirmed fully duplicated here. OCR is mocked so these run WITHOUT the real
tesseract system binary installed:

- OCR available  -> ``pytesseract.image_to_string`` returns text, which is
  returned verbatim (single-frame) or joined with ``## Page N`` markers
  (multi-frame TIFF).
- OCR unavailable -> ImportError of the OCR libs, a missing tesseract binary
  (``TesseractNotFoundError``), or empty OCR output all fall back to a
  placeholder string instead of raising.
"""

from __future__ import annotations

import io
import sys
import types

import pytest

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
    image_format: str | None = None,
) -> None:
    """Install fake ``pytesseract`` and ``PIL`` modules into sys.modules.

    Args:
        return_text: Text the fake ``image_to_string`` returns for single-frame.
        image_to_string_exc: If set, ``image_to_string`` raises this instead.
        n_frames: Simulated ``Image.n_frames`` (``>1`` exercises multi-page TIFF).
        page_texts: Per-page OCR strings when ``n_frames > 1``. Defaults to
            repeating ``return_text`` for each frame.
        image_format: Simulated ``Image.format``. Defaults to ``"TIFF"`` when
            ``n_frames > 1`` (so the multipage TIFF gate in extract.py opens),
            otherwise ``"PNG"``.
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

    resolved_format = (
        image_format if image_format is not None else ("TIFF" if n_frames > 1 else "PNG")
    )

    class _FakeImage:
        def __init__(self):
            self.n_frames = n_frames
            self.format = resolved_format

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


def _render_text_fixture_bytes(image_format: str, text: str = "INHERENT OCR") -> bytes:
    """Render real image bytes in the requested format for decode/OCR tests."""
    image_module = pytest.importorskip("PIL.Image")
    image_draw = pytest.importorskip("PIL.ImageDraw")
    image_font = pytest.importorskip("PIL.ImageFont")

    image = image_module.new("RGB", (600, 200), "white")
    draw = image_draw.Draw(image)
    draw.text((40, 80), text, fill="black", font=image_font.load_default())

    buffer = io.BytesIO()
    image.save(buffer, format=image_format)
    return buffer.getvalue()


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
        assert _extract_image_text(content, filename) == (f"[image: {filename}, no text extracted]")

    @pytest.mark.parametrize(
        ("image_format", "filename"),
        [
            ("JPEG", "scan.jpg"),
            ("WEBP", "scan.webp"),
            ("TIFF", "scan.tiff"),
            ("BMP", "scan.bmp"),
        ],
        ids=["jpeg", "webp", "tiff", "bmp"],
    )
    def test_sibling_formats_decode_real_fixtures(self, monkeypatch, image_format, filename):
        """#120: real format bytes must decode through Pillow before OCR runs."""
        pytesseract = pytest.importorskip("pytesseract")
        monkeypatch.setattr(pytesseract, "image_to_string", lambda _image: "Decoded fixture text")

        content = _render_text_fixture_bytes(image_format)
        assert _extract_image_text(content, filename) == "Decoded fixture text"

    @pytest.mark.parametrize(
        ("image_format", "filename"),
        [
            ("JPEG", "scan.jpg"),
            ("WEBP", "scan.webp"),
            ("TIFF", "scan.tiff"),
            ("BMP", "scan.bmp"),
        ],
        ids=["jpeg", "webp", "tiff", "bmp"],
    )
    def test_sibling_formats_optional_integration_ocr(self, image_format, filename):
        """#120: optional integration check with rendered text fixtures."""
        pytesseract = pytest.importorskip("pytesseract")
        try:
            pytesseract.get_tesseract_version()
        except pytesseract.TesseractNotFoundError:
            pytest.skip("tesseract binary not available")

        content = _render_text_fixture_bytes(image_format)
        text = _extract_image_text(content, filename)
        assert "inherent" in text.lower()

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

    def test_animated_webp_stays_on_single_frame_path(self, monkeypatch):
        """#120: n_frames > 1 on non-TIFF must not emit ``## Page N`` markers."""
        _install_fake_ocr(
            monkeypatch,
            return_text="First frame only",
            n_frames=3,
            image_format="WEBP",
        )
        text = _extract_image_text(WEBP_BYTES, "anim.webp")
        assert text == "First frame only"
        assert "## Page" not in text
