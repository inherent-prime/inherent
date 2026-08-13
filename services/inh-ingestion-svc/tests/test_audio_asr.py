"""Tests for audio ASR extraction (#128 Sprint 2–3).

Mirrors ``test_image_ocr.py``: ASR is mocked so these run WITHOUT the
optional ``asr`` extra (``faster-whisper``) installed.

- ASR available  -> mocked segments become prose + ``[t=MM:SS]`` markers
- ASR unavailable -> ``ImportError``, model-load failure, empty output, or
  unexpected errors all fall back to the placeholder string instead of raising
- Duration over ``ASR_MAX_DURATION_SECONDS`` -> non-retryable ApplicationError
- ``MemoryError`` propagates (not reclassified as a soft placeholder)
"""

from __future__ import annotations

import builtins
import sys
import types
from unittest.mock import MagicMock

import pytest
from temporalio.exceptions import ApplicationError

from src.temporal.activities import extract as extract_module
from src.temporal.activities.extract import (
    _extract_audio_text,
    _format_asr_transcript,
    _seconds_to_asr_marker,
)

AUDIO_BYTES = b"RIFF" + b"\x00\x00\x00\x00" + b"WAVE" + b"\x00" * 32
FILENAME = "meeting.mp3"
PLACEHOLDER = f"[audio: {FILENAME}, transcription unavailable]"


@pytest.fixture(autouse=True)
def cleanup_test_data(monkeypatch):
    """Keep these offline (no Postgres) and reset the ASR model singleton."""
    extract_module._asr_model = None
    extract_module._asr_model_key = None

    # Avoid requiring DATABASE_URL etc. when _get_asr_model reads settings.
    fake_settings = MagicMock()
    fake_settings.asr_model_size = "base"
    fake_settings.asr_device = "cpu"
    fake_settings.asr_compute_type = "int8"
    fake_settings.asr_max_duration_seconds = 7200
    monkeypatch.setattr(
        "src.config.settings.get_settings",
        lambda: fake_settings,
    )
    # extract.py imports get_settings locally in helpers — patch module attr too
    monkeypatch.setattr(extract_module, "_probe_audio_duration_seconds", lambda _p: None)

    yield
    extract_module._asr_model = None
    extract_module._asr_model_key = None


def _install_fake_faster_whisper(
    monkeypatch: pytest.MonkeyPatch,
    *,
    segment_texts: list[str] | None = None,
    segment_starts: list[float] | None = None,
    duration: float = 1.0,
    load_exc: BaseException | None = None,
    transcribe_exc: BaseException | None = None,
) -> MagicMock:
    """Install a fake ``faster_whisper`` package into ``sys.modules``."""
    if segment_texts is None:
        segment_texts = ["Hello from the meeting."]
    if segment_starts is None:
        segment_starts = [float(i) for i in range(len(segment_texts))]

    fake_pkg = types.ModuleType("faster_whisper")
    model_cls = MagicMock(name="WhisperModel")

    if load_exc is not None:

        def _raise_on_init(*_a, **_k):
            raise load_exc

        model_cls.side_effect = _raise_on_init
    else:
        instance = MagicMock(name="WhisperModelInstance")

        def _transcribe(_audio, **_kwargs):
            if transcribe_exc is not None:
                raise transcribe_exc
            segments = []
            for start, text in zip(segment_starts, segment_texts, strict=True):
                seg = MagicMock()
                seg.text = text
                seg.start = start
                segments.append(seg)
            info = MagicMock(language="en", duration=duration)
            return iter(segments), info

        instance.transcribe.side_effect = _transcribe
        model_cls.return_value = instance

    fake_pkg.WhisperModel = model_cls
    monkeypatch.setitem(sys.modules, "faster_whisper", fake_pkg)
    return model_cls


def _block_asr_imports(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force ImportError for ``faster_whisper`` (extra not installed)."""
    real_import = builtins.__import__

    def _fake_import(name, *args, **kwargs):
        if name == "faster_whisper" or name.startswith("faster_whisper."):
            raise ImportError(f"No module named {name!r}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _fake_import)


class TestAsrMarkers:
    def test_seconds_to_marker_folds_hours(self):
        assert _seconds_to_asr_marker(0) == "[t=00:00]"
        assert _seconds_to_asr_marker(65) == "[t=01:05]"
        assert _seconds_to_asr_marker(90 * 60) == "[t=90:00]"

    def test_format_inserts_marker_every_30s_bucket(self):
        segs = []
        for start, text in ((0.0, "A"), (10.0, "B"), (35.0, "C"), (70.0, "D")):
            seg = MagicMock()
            seg.start = start
            seg.text = text
            segs.append(seg)
        text = _format_asr_transcript(segs)
        assert text.startswith("[t=00:00] A B")
        assert "[t=00:35] C" in text
        assert "[t=01:10] D" in text


class TestExtractAudioText:
    def test_asr_available_returns_transcript_with_marker(self, monkeypatch):
        _install_fake_faster_whisper(
            monkeypatch,
            segment_texts=["Hello from the meeting.", "Next topic."],
            segment_starts=[0.0, 5.0],
        )
        text = _extract_audio_text(AUDIO_BYTES, FILENAME)
        assert "[t=00:00]" in text
        assert "Hello from the meeting." in text
        assert "Next topic." in text
        assert PLACEHOLDER not in text

    def test_asr_extra_missing_returns_placeholder(self, monkeypatch):
        _block_asr_imports(monkeypatch)
        text = _extract_audio_text(AUDIO_BYTES, FILENAME)
        assert text == PLACEHOLDER

    def test_model_load_failure_returns_placeholder(self, monkeypatch):
        _install_fake_faster_whisper(
            monkeypatch, load_exc=RuntimeError("simulated model download failure")
        )
        text = _extract_audio_text(AUDIO_BYTES, FILENAME)
        assert text == PLACEHOLDER

    def test_empty_transcript_returns_placeholder(self, monkeypatch):
        _install_fake_faster_whisper(monkeypatch, segment_texts=["  ", ""])
        text = _extract_audio_text(AUDIO_BYTES, FILENAME)
        assert text == PLACEHOLDER

    def test_transcribe_error_returns_placeholder(self, monkeypatch):
        _install_fake_faster_whisper(
            monkeypatch, transcribe_exc=ValueError("simulated decode failure")
        )
        text = _extract_audio_text(AUDIO_BYTES, FILENAME)
        assert text == PLACEHOLDER

    def test_memory_error_propagates(self, monkeypatch):
        """OOM must not be softened into a successful placeholder (#206)."""
        _install_fake_faster_whisper(monkeypatch, transcribe_exc=MemoryError("oom"))
        with pytest.raises(MemoryError):
            _extract_audio_text(AUDIO_BYTES, FILENAME)

    def test_duration_over_cap_raises_non_retryable(self, monkeypatch):
        _install_fake_faster_whisper(
            monkeypatch,
            segment_texts=["too long"],
            duration=7201.0,
        )
        with pytest.raises(ApplicationError, match="7200") as exc_info:
            _extract_audio_text(AUDIO_BYTES, FILENAME)
        assert exc_info.value.non_retryable is True
        assert exc_info.value.type == "AudioDurationExceeded"

    def test_probe_duration_over_cap_raises_before_transcribe(self, monkeypatch):
        model_cls = _install_fake_faster_whisper(monkeypatch, duration=1.0)
        monkeypatch.setattr(extract_module, "_probe_audio_duration_seconds", lambda _p: 8000.0)
        with pytest.raises(ApplicationError, match="8000") as exc_info:
            _extract_audio_text(AUDIO_BYTES, FILENAME)
        assert exc_info.value.non_retryable is True
        # transcribe must not have been reached
        instance = model_cls.return_value
        instance.transcribe.assert_not_called()

    def test_placeholder_string_is_stable(self):
        """Pin the exact issue-#128 placeholder shape agents can grep for."""
        assert PLACEHOLDER == "[audio: meeting.mp3, transcription unavailable]"


class TestExtractTimeoutHelper:
    """Pin the workflow's audio vs non-audio timeout split (#128 Sprint 3)."""

    def test_audio_mime_gets_60_minute_timeout(self):
        from datetime import timedelta

        from src.temporal.workflows.document_ingestion import _extract_timeout_for_content_type

        assert _extract_timeout_for_content_type("audio/mpeg") == timedelta(minutes=60)
        assert _extract_timeout_for_content_type("audio/wav; charset=binary") == timedelta(
            minutes=60
        )

    def test_non_audio_stays_at_5_minutes(self):
        from datetime import timedelta

        from src.temporal.workflows.document_ingestion import _extract_timeout_for_content_type

        assert _extract_timeout_for_content_type("application/pdf") == timedelta(minutes=5)
        assert _extract_timeout_for_content_type("text/plain") == timedelta(minutes=5)
