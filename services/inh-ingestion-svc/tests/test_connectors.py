"""Unit tests for FilesystemConnector."""

import pytest

from src.connectors.filesystem import FilesystemConnector
from src.models.document import DocumentMetadata


class TestFilesystemConnectorFetchDocument:
    """Tests for fetch_document method."""

    def test_fetch_document_returns_correct_bytes(self, tmp_path):
        """fetch_document should return the exact bytes written to the file."""
        content = b"hello, world"
        file = tmp_path / "doc.txt"
        file.write_bytes(content)

        connector = FilesystemConnector(base_path=str(tmp_path))
        result = connector.fetch_document("doc.txt")

        assert result == content

    def test_fetch_document_raises_file_not_found_for_missing_file(self, tmp_path):
        """fetch_document should raise FileNotFoundError when the file does not exist."""
        connector = FilesystemConnector(base_path=str(tmp_path))

        with pytest.raises(FileNotFoundError):
            connector.fetch_document("nonexistent.txt")

    def test_fetch_document_works_with_binary_files(self, tmp_path):
        """fetch_document should correctly return raw binary content."""
        binary_content = bytes(range(256))
        file = tmp_path / "binary.bin"
        file.write_bytes(binary_content)

        connector = FilesystemConnector(base_path=str(tmp_path))
        result = connector.fetch_document("binary.bin")

        assert result == binary_content

    def test_fetch_document_works_with_nested_subdirectory_paths(self, tmp_path):
        """fetch_document should resolve files inside nested subdirectories."""
        nested_dir = tmp_path / "a" / "b"
        nested_dir.mkdir(parents=True)
        content = b"nested content"
        (nested_dir / "file.txt").write_bytes(content)

        connector = FilesystemConnector(base_path=str(tmp_path))
        result = connector.fetch_document("a/b/file.txt")

        assert result == content


class TestFilesystemConnectorFetchMetadata:
    """Tests for fetch_metadata method."""

    def test_fetch_metadata_returns_document_metadata_with_correct_fields(self, tmp_path):
        """fetch_metadata should return a DocumentMetadata with correct filename, type, size, and location."""
        content = b"some data"
        file = tmp_path / "report.pdf"
        file.write_bytes(content)

        connector = FilesystemConnector(base_path=str(tmp_path))
        metadata = connector.fetch_metadata("report.pdf")

        assert isinstance(metadata, DocumentMetadata)
        assert metadata.filename == "report.pdf"
        assert metadata.file_type == ".pdf"
        assert metadata.file_size == len(content)
        assert str(tmp_path / "report.pdf") in metadata.file_location

    def test_fetch_metadata_returns_none_for_missing_file(self, tmp_path):
        """fetch_metadata should return None when the file does not exist."""
        connector = FilesystemConnector(base_path=str(tmp_path))
        result = connector.fetch_metadata("does_not_exist.txt")

        assert result is None


class TestFilesystemConnectorLifecycle:
    """Tests for connect/disconnect and initialisation."""

    def test_connect_and_disconnect_do_not_raise(self, tmp_path):
        """connect() and disconnect() are no-ops and must not raise any exception."""
        connector = FilesystemConnector(base_path=str(tmp_path))
        connector.connect()
        connector.disconnect()

    def test_default_base_path_is_cwd(self):
        """Default base_path resolves to the current working directory (#35).

        The connector now resolves base_path so containment checks compare
        absolute paths.
        """
        from pathlib import Path

        connector = FilesystemConnector()
        assert connector.base_path == Path(".").resolve()


class TestFilesystemConnectorTraversal:
    """FilesystemConnector must not read outside its base path (#35)."""

    def test_blocks_parent_traversal(self, tmp_path):
        from src.connectors.filesystem import FilesystemConnector

        base = tmp_path / "root"
        base.mkdir()
        connector = FilesystemConnector(str(base))
        import pytest

        with pytest.raises(PermissionError):
            connector.fetch_document("../../../etc/passwd")

    def test_blocks_absolute_path_escape(self, tmp_path):
        from src.connectors.filesystem import FilesystemConnector

        base = tmp_path / "root"
        base.mkdir()
        connector = FilesystemConnector(str(base))
        import pytest

        with pytest.raises(PermissionError):
            connector.fetch_document("/etc/passwd")

    def test_allows_legit_file(self, tmp_path):
        from src.connectors.filesystem import FilesystemConnector

        base = tmp_path / "root"
        base.mkdir()
        (base / "doc.txt").write_text("hello")
        connector = FilesystemConnector(str(base))
        assert connector.fetch_document("doc.txt") == b"hello"
