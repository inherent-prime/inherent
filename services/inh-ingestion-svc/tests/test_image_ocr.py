"""Tests for PNG image OCR extraction with graceful fallback (#61).

Covers the Temporal activity helper (``_extract_image_text`` in
``extract.py``) -- the live ingestion path's OCR extraction. This file used
to also cover the equivalent method on the legacy ``DocumentProcessor``
(``processor.py``); that class was removed (#185) once its OCR behaviour was
confirmed fully duplicated here. OCR is mocked so these run WITHOUT the real
tesseract system binary installed:

- OCR available  -> ``pytesseract.image_to_string`` returns text, which is
  returned verbatim.
- OCR unavailable -> ImportError of the OCR libs, a missing tesseract binary
  (``TesseractNotFoundError``), or empty OCR output all fall back to a
  placeholder string instead of raising.
"""

from __future__ import annotations

import sys
import types

import pytest

from src.temporal.activities.extract import _extract_image_text

PNG_BYTES = b"\x89PNG\r\n\x1a\n fake png bytes"
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
) -> None:
    """Install fake ``pytesseract`` and ``PIL`` modules into sys.modules.

    Args:
        return_text: Text the fake ``image_to_string`` returns.
        image_to_string_exc: If set, ``image_to_string`` raises this instead.
    """

    class TesseractNotFoundError(Exception):
        pass

    fake_pytesseract = types.ModuleType("pytesseract")
    fake_pytesseract.TesseractNotFoundError = TesseractNotFoundError

    def _image_to_string(_image):
        if image_to_string_exc is not None:
            raise image_to_string_exc("simulated tesseract failure")
        return return_text

    fake_pytesseract.image_to_string = _image_to_string

    fake_pil = types.ModuleType("PIL")
    fake_pil_image = types.ModuleType("PIL.Image")

    def _open(_fp):
        return object()  # placeholder image object; never really decoded

    fake_pil_image.open = _open
    fake_pil.Image = fake_pil_image

    monkeypatch.setitem(sys.modules, "pytesseract", fake_pytesseract)
    monkeypatch.setitem(sys.modules, "PIL", fake_pil)
    monkeypatch.setitem(sys.modules, "PIL.Image", fake_pil_image)


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

