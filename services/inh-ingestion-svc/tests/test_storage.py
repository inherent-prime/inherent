"""Tests for Storage service."""

from unittest.mock import MagicMock, patch

import pytest
from httpx import Request, Response

from src.config.settings import Settings
from src.services.storage import (
    LocalStorageBackend,
    S3StorageBackend,
    StorageService,
)


class TestS3StorageBackend:
    """Tests for S3StorageBackend."""

    @pytest.fixture
    def mock_settings(self):
        """Create mock settings."""
        settings = MagicMock(spec=Settings)
        settings.storage_bucket = "test-bucket"
        settings.s3_endpoint = "https://nbg1.your-objectstorage.com"
        settings.s3_access_key_id = "test-key"
        settings.s3_secret_access_key = "test-secret"
        settings.s3_region = "nbg1"
        return settings

    @patch("boto3.client")
    def test_connect(self, mock_boto_client, mock_settings):
        """Test connecting to S3."""
        backend = S3StorageBackend(mock_settings)
        backend.connect()

        mock_boto_client.assert_called_with(
            "s3",
            endpoint_url="https://nbg1.your-objectstorage.com",
            aws_access_key_id="test-key",
            aws_secret_access_key="test-secret",
            region_name="nbg1",
        )
        assert backend.client is not None

    def test_disconnect(self, mock_settings):
        """Test disconnecting from S3."""
        backend = S3StorageBackend(mock_settings)
        backend.client = MagicMock()

        backend.disconnect()

        assert backend.client is None

    def test_read_file(self, mock_settings):
        """Test reading file from S3."""
        backend = S3StorageBackend(mock_settings)
        mock_client = MagicMock()
        mock_body = MagicMock()
        mock_body.read.return_value = b"s3 content"
        mock_client.get_object.return_value = {"Body": mock_body}
        backend.client = mock_client

        content = backend.read_file("path/to/file")

        assert content == b"s3 content"
        mock_client.get_object.assert_called_with(Bucket="test-bucket", Key="path/to/file")

    def test_read_file_with_custom_bucket(self, mock_settings):
        """Test reading file from S3 with custom bucket."""
        backend = S3StorageBackend(mock_settings)
        mock_client = MagicMock()
        mock_body = MagicMock()
        mock_body.read.return_value = b"content"
        mock_client.get_object.return_value = {"Body": mock_body}
        backend.client = mock_client

        backend.read_file("file.txt", bucket="other-bucket")

        mock_client.get_object.assert_called_with(Bucket="other-bucket", Key="file.txt")

    def test_read_file_not_connected(self, mock_settings):
        """Test read_file raises when not connected."""
        backend = S3StorageBackend(mock_settings)

        with pytest.raises(RuntimeError, match="S3 not connected"):
            backend.read_file("path/to/file")

    def test_file_exists_true(self, mock_settings):
        """Test file_exists returns True when file exists."""
        backend = S3StorageBackend(mock_settings)
        mock_client = MagicMock()
        mock_client.head_object.return_value = {}
        backend.client = mock_client

        assert backend.file_exists("path/to/file") is True

    def test_file_exists_false(self, mock_settings):
        """Test file_exists returns False when file doesn't exist."""
        backend = S3StorageBackend(mock_settings)
        mock_client = MagicMock()
        mock_client.head_object.side_effect = Exception("Not Found")
        backend.client = mock_client

        assert backend.file_exists("path/to/file") is False

    def test_file_exists_not_connected(self, mock_settings):
        """Test file_exists returns False when not connected."""
        backend = S3StorageBackend(mock_settings)

        assert backend.file_exists("path/to/file") is False


class TestLocalStorageBackend:
    """Tests for LocalStorageBackend (filesystem-based)."""

    @pytest.fixture
    def mock_settings(self, tmp_path):
        settings = MagicMock(spec=Settings)
        settings.local_storage_path = str(tmp_path)
        return settings

    def test_connect_is_noop(self, mock_settings):
        """connect() should succeed without side effects."""
        backend = LocalStorageBackend(mock_settings)
        backend.connect()  # no-op, should not raise

    def test_disconnect_is_noop(self, mock_settings):
        """disconnect() should succeed without side effects."""
        backend = LocalStorageBackend(mock_settings)
        backend.disconnect()  # no-op, should not raise

    def test_read_file(self, mock_settings, tmp_path):
        """Test reading a file from local storage."""
        test_file = tmp_path / "doc.txt"
        test_file.write_bytes(b"hello world")

        backend = LocalStorageBackend(mock_settings)
        content = backend.read_file("doc.txt")

        assert content == b"hello world"

    def test_read_file_with_bucket(self, mock_settings, tmp_path):
        """Test reading a file with a bucket subdirectory."""
        bucket_dir = tmp_path / "documents"
        bucket_dir.mkdir()
        test_file = bucket_dir / "doc.txt"
        test_file.write_bytes(b"bucket content")

        backend = LocalStorageBackend(mock_settings)
        content = backend.read_file("doc.txt", bucket="documents")

        assert content == b"bucket content"

    def test_read_file_not_found(self, mock_settings, tmp_path):
        """Test read_file raises FileNotFoundError for missing files."""
        backend = LocalStorageBackend(mock_settings)

        with pytest.raises(FileNotFoundError):
            backend.read_file("missing.pdf")

    def test_file_exists_true(self, mock_settings, tmp_path):
        """Test file_exists returns True for existing files."""
        test_file = tmp_path / "exists.txt"
        test_file.write_bytes(b"data")

        backend = LocalStorageBackend(mock_settings)
        assert backend.file_exists("exists.txt") is True

    def test_file_exists_false(self, mock_settings, tmp_path):
        """Test file_exists returns False for missing files."""
        backend = LocalStorageBackend(mock_settings)
        assert backend.file_exists("nope.txt") is False

    def test_path_traversal_blocked(self, mock_settings, tmp_path):
        """Test that path traversal is blocked."""
        backend = LocalStorageBackend(mock_settings)

        with pytest.raises(PermissionError, match="Path traversal blocked"):
            backend.read_file("../../etc/passwd")


class TestStorageService:
    """Tests for StorageService."""

    @pytest.fixture
    def mock_settings(self):
        """Create mock settings."""
        settings = MagicMock(spec=Settings)
        settings.storage_backend = "s3"
        settings.storage_bucket = "test-bucket"
        settings.s3_endpoint = "https://nbg1.example.com"
        settings.s3_access_key_id = "key"
        settings.s3_secret_access_key = "secret"
        settings.s3_region = "nbg1"
        settings.intg_service_url = "http://localhost:4000"
        settings.local_storage_path = ""
        return settings

    @patch("src.services.storage.S3StorageBackend")
    @patch("src.services.storage.LocalStorageBackend")
    def test_connect_s3(self, mock_local_cls, mock_s3_cls, mock_settings):
        """Test connecting storage service with S3 backend."""
        service = StorageService(mock_settings)
        service.connect()

        mock_local_cls.return_value.connect.assert_called_once()
        mock_s3_cls.return_value.connect.assert_called_once()
        assert "local" in service._backends
        assert "s3" in service._backends

    def test_disconnect(self, mock_settings):
        """Test disconnecting storage service."""
        service = StorageService(mock_settings)
        mock_backend = MagicMock()
        service._backends["local"] = mock_backend
        service._connected = True

        service.disconnect()

        mock_backend.disconnect.assert_called_once()
        assert len(service._backends) == 0
        assert service._connected is False

    def test_get_backend(self, mock_settings):
        """Test getting a backend."""
        service = StorageService(mock_settings)
        mock_backend = MagicMock()
        service._backends["test"] = mock_backend

        assert service.get_backend("test") == mock_backend

        with pytest.raises(ValueError):
            service.get_backend("nonexistent")

    @patch("src.services.storage.httpx.Client")
    def test_read_file_from_url(self, mock_client_cls, mock_settings):
        """Test reading file from URL."""
        service = StorageService(mock_settings)

        mock_client = MagicMock()
        request = Request("GET", "http://example.com/file")
        mock_client.get.return_value = Response(200, content=b"content", request=request)
        mock_client_cls.return_value.__enter__.return_value = mock_client

        content = service.read_file_from_url("http://example.com/file")

        assert content == b"content"
        mock_client.get.assert_called_with("http://example.com/file")


class TestLocalStorageTraversal:
    """LocalStorageBackend._resolve must block escapes via a real path
    boundary, not a string prefix (a sibling dir sharing the base name used to
    slip through) (#11)."""

    def _backend(self, base):
        settings = MagicMock()
        settings.local_storage_path = str(base)
        return LocalStorageBackend(settings)

    def test_blocks_sibling_dir_prefix_escape(self, tmp_path):
        base = tmp_path / "store"
        base.mkdir()
        secrets = tmp_path / "store-secrets"
        secrets.mkdir()
        (secrets / "creds").write_text("top secret")

        backend = self._backend(base)
        # /tmp/../store-secrets/creds startswith('/tmp/.../store') -> True (old bug).
        with pytest.raises(PermissionError):
            backend._resolve("../store-secrets/creds")

    def test_blocks_absolute_and_parent_escape(self, tmp_path):
        base = tmp_path / "store"
        base.mkdir()
        backend = self._backend(base)
        with pytest.raises(PermissionError):
            backend._resolve("../../etc/passwd")

    def test_allows_legitimate_descendant(self, tmp_path):
        base = tmp_path / "store"
        base.mkdir()
        backend = self._backend(base)
        resolved = backend._resolve("sub/doc.txt")
        assert resolved.is_relative_to(base.resolve())
