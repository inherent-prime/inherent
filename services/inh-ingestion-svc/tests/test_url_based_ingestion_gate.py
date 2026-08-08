"""#214 gate: storage_backend="azure" bypasses the #210 storage_path /
workspace_id prefix check entirely.

require_storage_path_workspace_prefix (src/api/ownership.py, #210) verifies
storage_path is consistent with the caller's claimed workspace_id -- but the
"azure" branches of fetch_document (fetch.py) and extract_text (extract.py)
never look at storage_path at all. They fetch an arbitrary caller-supplied
storage_url instead, so there is no workspace-prefixed invariant for #210's
check to apply to. A caller could set storage_backend="azure" with a
throwaway own-workspace storage_path (satisfying #210 trivially) plus a
storage_url pointing anywhere externally reachable, and have that content
filed into their own tenant -- the only remaining guard is #34's SSRF check,
which blocks metadata/loopback/RFC1918 targets but allows any other
http/https URL.

Settings.allow_url_based_ingestion (src/config/settings.py) closes this by
gating the entire "azure" branch OFF by default: an operator who wants
direct-URL ingestion must opt in explicitly via ALLOW_URL_BASED_INGESTION,
at which point they are knowingly accepting that #34's SSRF guard is the
only remaining check on what gets fetched into a tenant.

fetch_document and extract_text each carry their own copy of this gate
(activities are independently retryable/replayable and must each be safe to
call on their own -- neither may assume the other's gate already ran), so
both are exercised here against the same three states to catch the two
gates drifting out of sync.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.config.settings import Settings
from src.temporal.activities.extract import extract_text
from src.temporal.activities.fetch import fetch_document
from src.temporal.models import ExtractTextInput, FetchDocumentInput


# Override the package-level DB-dependent autouse fixture (tests/conftest.py)
# with a no-op. Every test here mocks get_settings/get_storage_service/
# get_staging_service directly and never touches a real database -- without
# this override the whole module silently SKIPS wherever PostgreSQL is
# absent, so this security-relevant gate suite would report green while
# checking nothing. Same pattern as tests/test_temporal_trigger.py and
# tests/test_settings_config_dedup_contract.py.
@pytest.fixture(autouse=True)
def cleanup_test_data():
    """No-op override so this module's tests run without a live database."""
    yield


def _track_event_cm():
    """A no-op async context manager standing in for track_event, matching
    the pattern already used in tests/test_fetch_activity.py."""
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=None)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def test_allow_url_based_ingestion_defaults_off():
    """The gate must fail closed: an operator who never sets
    ALLOW_URL_BASED_INGESTION gets the safer (disabled) behavior.

    Asserted against the declared field default rather than an instantiated
    Settings(), because Settings has other required fields (DATABASE_URL,
    WEAVIATE_URL) unrelated to this gate -- same pattern as
    tests/test_storage_backend_enum.py.
    """
    assert Settings.model_fields["allow_url_based_ingestion"].default is False


class TestFetchDocumentAzureGate:
    """fetch_document's azure branch (src/temporal/activities/fetch.py)."""

    @patch("src.config.settings.get_settings")
    @patch("src.temporal.activities.fetch.track_event")
    @patch("src.temporal.shared_services.get_storage_service")
    @pytest.mark.asyncio
    async def test_disabled_by_default_blocks_even_with_url(
        self, mock_get_storage, mock_track, mock_get_settings
    ):
        """The gate must fire before the storage_url-presence check. A
        caller mounting the #214 bypass always HAS a storage_url (it's the
        attack) -- if presence were checked first, the gate would be a
        no-op for exactly the caller it exists to stop."""
        mock_track.return_value = _track_event_cm()
        mock_get_storage.return_value = MagicMock()
        mock_get_settings.return_value = MagicMock(allow_url_based_ingestion=False)

        with pytest.raises(RuntimeError, match="disabled"):
            await fetch_document(
                FetchDocumentInput(
                    document_id="d",
                    storage_backend="azure",
                    storage_path="doc.txt",
                    storage_url="https://example.com/doc.txt",
                    workflow_run_id="wf",
                    workspace_id="ws",
                )
            )

    @patch("src.config.settings.get_settings")
    @patch("src.temporal.activities.fetch.track_event")
    @patch("src.temporal.shared_services.get_storage_service")
    @pytest.mark.asyncio
    async def test_enabled_without_url_raises(
        self, mock_get_storage, mock_track, mock_get_settings
    ):
        """With the gate explicitly enabled, the pre-existing url-required
        validation still applies underneath it."""
        mock_track.return_value = _track_event_cm()
        mock_get_storage.return_value = MagicMock()
        mock_get_settings.return_value = MagicMock(allow_url_based_ingestion=True)

        with pytest.raises(RuntimeError, match="requires storage_url"):
            await fetch_document(
                FetchDocumentInput(
                    document_id="d",
                    storage_backend="azure",
                    storage_path="doc.txt",
                    workflow_run_id="wf",
                    workspace_id="ws",
                )
            )

    @patch("src.config.settings.get_settings")
    @patch("src.temporal.activities.fetch.track_event")
    @patch("src.temporal.shared_services.get_storage_service")
    @pytest.mark.asyncio
    async def test_enabled_with_url_fetches(self, mock_get_storage, mock_track, mock_get_settings):
        """With the gate explicitly enabled and a storage_url present, the
        existing direct-fetch behavior is unchanged."""
        mock_track.return_value = _track_event_cm()
        storage = MagicMock()
        storage.read_file_from_url.return_value = b"content"
        mock_get_storage.return_value = storage
        mock_get_settings.return_value = MagicMock(allow_url_based_ingestion=True)

        out = await fetch_document(
            FetchDocumentInput(
                document_id="d",
                storage_backend="azure",
                storage_path="doc.txt",
                storage_url="https://example.com/doc.txt",
                workflow_run_id="wf",
                workspace_id="ws",
            )
        )
        assert out.size_bytes == len(b"content")
        storage.read_file_from_url.assert_called_once_with("https://example.com/doc.txt")


class TestExtractTextAzureGate:
    """extract_text's azure branch (src/temporal/activities/extract.py) --
    must mirror fetch_document's gate exactly (same messages, same order of
    checks) so the two don't silently drift apart."""

    @patch("src.config.settings.get_settings")
    @patch("src.temporal.shared_services.get_storage_service")
    @pytest.mark.asyncio
    async def test_disabled_by_default_blocks_even_with_url(
        self, mock_get_storage, mock_get_settings
    ):
        mock_get_storage.return_value = MagicMock()
        mock_get_settings.return_value = MagicMock(allow_url_based_ingestion=False)

        input_data = ExtractTextInput(
            workflow_run_id="wf_azure",
            storage_backend="azure",
            storage_path="doc.txt",
            content_type="text/plain",
            original_filename="doc.txt",
            storage_url="https://example.com/doc.txt",
        )

        with pytest.raises(RuntimeError, match="disabled"):
            await extract_text(input_data)

    @patch("src.config.settings.get_settings")
    @patch("src.temporal.shared_services.get_staging_service")
    @patch("src.temporal.shared_services.get_storage_service")
    @pytest.mark.asyncio
    async def test_enabled_with_url_fetches(
        self, mock_get_storage, mock_get_staging, mock_get_settings
    ):
        """With the gate explicitly enabled and a storage_url present, the
        existing direct-fetch behavior is unchanged -- content reaches the
        extractor rather than failing before it's read."""
        storage = MagicMock()
        storage.read_file_from_url.return_value = b"hello world"
        mock_get_storage.return_value = storage
        mock_get_staging.return_value = MagicMock()
        mock_get_settings.return_value = MagicMock(allow_url_based_ingestion=True)

        input_data = ExtractTextInput(
            workflow_run_id="wf_azure",
            storage_backend="azure",
            storage_path="doc.txt",
            content_type="text/plain",
            original_filename="doc.txt",
            storage_url="https://example.com/doc.txt",
        )

        result = await extract_text(input_data)
        assert result.text_length == len("hello world")
        storage.read_file_from_url.assert_called_once_with("https://example.com/doc.txt")
