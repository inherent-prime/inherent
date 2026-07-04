"""Local filesystem connector."""

from pathlib import Path

from src.connectors.base import BaseConnector
from src.models.document import DocumentMetadata


class FilesystemConnector(BaseConnector):
    """Connector for local filesystem."""

    def __init__(self, base_path: str = "."):
        """Initialize filesystem connector."""
        self.base_path = Path(base_path).resolve()

    def connect(self) -> None:
        """Connect to filesystem (no-op)."""
        pass

    def disconnect(self) -> None:
        """Disconnect from filesystem (no-op)."""
        pass

    def _safe_path(self, location: str) -> Path:
        """Resolve ``location`` within base_path, rejecting traversal (#35).

        ``pathlib`` discards the base when the right operand is absolute
        (``base / "/etc/passwd" == /etc/passwd``), and ``../`` sequences escape,
        so join alone is not containment. Resolve and require the result to stay
        under the (resolved) base.
        """
        resolved = (self.base_path / location).resolve()
        if resolved != self.base_path and not resolved.is_relative_to(self.base_path):
            raise PermissionError(f"Path traversal blocked: {location}")
        return resolved

    def fetch_document(self, location: str) -> bytes:
        """Fetch document from filesystem."""
        file_path = self._safe_path(location)
        if not file_path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")
        return file_path.read_bytes()

    def fetch_metadata(self, location: str) -> DocumentMetadata | None:
        """Fetch metadata from filesystem."""
        file_path = self._safe_path(location)
        if not file_path.exists():
            return None

        stat = file_path.stat()
        return DocumentMetadata(
            filename=file_path.name,
            file_type=file_path.suffix,
            file_size=stat.st_size,
            file_location=str(file_path),
        )
