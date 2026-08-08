"""Versioned event contract tests (milestone #17, consumer side).

Pins the canonical v1 upload-event key set and verifies:
- lossless round-trip dict -> DocumentUploadMessage -> dict
- backward compat: a dict WITHOUT contract_version still validates (defaults)
- forward compat: an unknown extra field is ignored without error
"""

import pytest

from src.models.document import DocumentCompletionMessage, DocumentUploadMessage


@pytest.fixture(autouse=True)
def cleanup_test_data():
    """No-op override of the package-level DB-dependent autouse fixture.

    The package ``tests/conftest.py`` defines an autouse ``cleanup_test_data``
    that depends on ``db_service`` and skips when PostgreSQL is unavailable.
    These tests are pure/offline, so we override it with a no-op (same pattern
    as tests/failure_injection/conftest.py).
    """
    yield


# Canonical v1 upload-event key set. The producer (public-api) emits exactly
# these keys; keep this list in lockstep with the producer-side contract test.
CANONICAL_UPLOAD_KEYS_V1 = {
    "event_type",
    "document_id",
    "workspace_id",
    "user_id",
    "filename",
    "original_filename",
    "content_type",
    "size_bytes",
    "storage_backend",
    "storage_path",
    "storage_bucket",
    "storage_url",
    "timestamp",
    "contract_version",
    # Ingestion source labeling (inherent-systems/prime#187) — additive, optional, backward compatible.
    "source",
    "connection_id",
    "sync_id",
}


def _canonical_upload_event() -> dict:
    return {
        "event_type": "document.uploaded",
        "document_id": "507f1f77bcf86cd799439011",
        "workspace_id": "507f1f77bcf86cd799439012",
        "user_id": "507f1f77bcf86cd799439013",
        "filename": "1234567890-abc12345-document.pdf",
        "original_filename": "document.pdf",
        "content_type": "application/pdf",
        "size_bytes": 102400,
        "storage_backend": "gcs",
        "storage_path": "workspaces/507f1f77bcf86cd799439012/doc.pdf",
        "storage_bucket": "documents",
        "storage_url": "https://storage.googleapis.com/documents/workspaces/doc.pdf",
        "timestamp": "2024-01-15T10:30:00Z",
        "contract_version": "1.0.0",
        "source": "connector:notion",
        "connection_id": "conn_123",
        "sync_id": "sync_456",
    }


def test_canonical_key_set_matches_model_fields():
    """The canonical key set must equal the model's declared field set."""
    assert set(DocumentUploadMessage.model_fields.keys()) == CANONICAL_UPLOAD_KEYS_V1


def test_upload_event_round_trip_lossless():
    """Canonical dict -> model -> dict is lossless."""
    event = _canonical_upload_event()
    msg = DocumentUploadMessage(**event)
    dumped = msg.model_dump()

    assert set(dumped.keys()) == CANONICAL_UPLOAD_KEYS_V1
    assert dumped == event
    assert dumped["contract_version"] == "1.0.0"


def test_upload_event_without_contract_version_defaults():
    """A message WITHOUT contract_version still validates (backward compat)."""
    event = _canonical_upload_event()
    del event["contract_version"]

    msg = DocumentUploadMessage(**event)
    assert msg.contract_version == "1.0.0"


def test_upload_event_ignores_unknown_extra_field():
    """An unknown extra field is ignored, not an error (forward compat)."""
    event = _canonical_upload_event()
    event["some_future_field"] = "ignore-me"

    msg = DocumentUploadMessage(**event)
    assert not hasattr(msg, "some_future_field")
    assert "some_future_field" not in msg.model_dump()


def test_completion_message_has_contract_version_default():
    """Completion message also carries the versioned contract field."""
    msg = DocumentCompletionMessage(
        event_type="document.processed",
        document_id="d",
        workspace_id="w",
        user_id="u",
        original_filename="f.pdf",
        success=True,
        status="ready",
        timestamp="2024-01-15T10:30:00Z",
    )
    assert msg.contract_version == "1.0.0"

    # Explicit override survives a round-trip.
    msg2 = DocumentCompletionMessage(
        event_type="document.processed",
        document_id="d",
        workspace_id="w",
        user_id="u",
        original_filename="f.pdf",
        success=True,
        status="ready",
        timestamp="2024-01-15T10:30:00Z",
        contract_version="1.2.3",
    )
    assert msg2.model_dump()["contract_version"] == "1.2.3"
