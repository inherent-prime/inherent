"""Multi-backend storage service for fetching documents."""

import ipaddress
import socket
from abc import ABC, abstractmethod
from pathlib import Path
from urllib.parse import urlparse

import httpx
import structlog

from src.config.settings import Settings

logger = structlog.get_logger(__name__)

_BLOCKED_HOSTNAMES = {"localhost", "metadata", "metadata.google.internal"}


def _is_internal_ip(candidate: str) -> bool:
    """True if ``candidate`` is a literal IP in a private/loopback/link-local/reserved range."""
    try:
        ip = ipaddress.ip_address(candidate)
    except ValueError:
        return False
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def _validate_fetch_url(url: str) -> None:
    """Reject URLs that could be an SSRF vector (#34).

    Only http/https to a non-internal address is allowed; cloud-metadata,
    loopback, and RFC1918 targets are blocked. Hostnames are resolved
    best-effort and rejected if they map to an internal address.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise PermissionError(f"URL scheme not allowed for fetch: {parsed.scheme!r}")

    host = (parsed.hostname or "").strip("[]")
    if not host or host.lower() in _BLOCKED_HOSTNAMES:
        raise PermissionError(f"Blocked host for fetch: {host!r}")
    if _is_internal_ip(host):
        raise PermissionError(f"Blocked internal address for fetch: {host!r}")

    # Best-effort DNS: reject a hostname that resolves to an internal address.
    try:
        for info in socket.getaddrinfo(host, None):
            if _is_internal_ip(info[4][0]):
                raise PermissionError(f"Host resolves to internal address: {host!r}")
    except socket.gaierror:
        pass  # let the HTTP client surface an unresolved-host error


class BaseStorageBackend(ABC):
    """Abstract base class for storage backends."""

    @abstractmethod
    def connect(self) -> None:
        """Connect to storage backend."""
        pass

    @abstractmethod
    def disconnect(self) -> None:
        """Disconnect from storage backend."""
        pass

    @abstractmethod
    def read_file(self, path: str, bucket: str | None = None) -> bytes:
        """Read a file from storage."""
        pass

    @abstractmethod
    def file_exists(self, path: str, bucket: str | None = None) -> bool:
        """Check if a file exists."""
        pass

    def get_size(self, path: str, bucket: str | None = None) -> int:
        """Return the file size in bytes.

        Default reads the file; backends should override with a stat/HEAD that
        avoids downloading the content (#22).
        """
        return len(self.read_file(path, bucket))


class S3StorageBackend(BaseStorageBackend):
    """S3-compatible storage backend (Hetzner Object Storage, AWS S3, MinIO)."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = None
        self.default_bucket = settings.storage_bucket

    def connect(self) -> None:
        """Connect to S3-compatible storage."""
        import boto3

        self.client = boto3.client(
            "s3",
            endpoint_url=self.settings.s3_endpoint,
            aws_access_key_id=self.settings.s3_access_key_id,
            aws_secret_access_key=self.settings.s3_secret_access_key,
            region_name=self.settings.s3_region,
        )
        logger.info(
            "Connected to S3-compatible storage",
            endpoint=self.settings.s3_endpoint,
            region=self.settings.s3_region,
        )

    def disconnect(self) -> None:
        """Disconnect from S3."""
        self.client = None
        logger.info("Disconnected from S3")

    def read_file(self, path: str, bucket: str | None = None) -> bytes:
        """Read a file from S3."""
        if not self.client:
            raise RuntimeError("S3 not connected")

        target_bucket = bucket or self.default_bucket
        if not target_bucket:
            raise RuntimeError("No bucket specified")

        response = self.client.get_object(Bucket=target_bucket, Key=path)
        content = response["Body"].read()
        logger.info("Read file from S3", path=path, bucket=target_bucket, size=len(content))
        return content

    def file_exists(self, path: str, bucket: str | None = None) -> bool:
        """Check if a file exists in S3."""
        if not self.client:
            return False

        target_bucket = bucket or self.default_bucket
        if not target_bucket:
            return False

        try:
            self.client.head_object(Bucket=target_bucket, Key=path)
            return True
        except Exception:
            return False

    def get_size(self, path: str, bucket: str | None = None) -> int:
        """Return object size via a HEAD request — no content download (#22)."""
        if not self.client:
            raise RuntimeError("S3 client not connected")
        target_bucket = bucket or self.default_bucket
        if not target_bucket:
            raise RuntimeError("No S3 bucket configured")
        resp = self.client.head_object(Bucket=target_bucket, Key=path)
        return int(resp["ContentLength"])


class LocalStorageBackend(BaseStorageBackend):
    """Local filesystem storage backend.

    Reads files from a configurable base directory. When LOCAL_STORAGE_PATH
    is set, files are resolved relative to that path. Otherwise, falls back
    to the service working directory.

    intg-svc stores files as: <base>/documents/<storage_path>
    where 'documents' is the storage_bucket. This backend mirrors that
    layout by incorporating the bucket into the resolved path.
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        base = settings.local_storage_path
        self._base_path = Path(base).resolve() if base else Path.cwd().resolve()

    def connect(self) -> None:
        """No-op — filesystem is always available."""
        logger.info("Local storage backend ready", base_path=str(self._base_path))

    def disconnect(self) -> None:
        """No-op."""
        pass

    def _resolve(self, path: str, bucket: str | None = None) -> Path:
        """Resolve a storage path to an absolute filesystem path.

        Builds: <base_path>/<bucket>/<path>

        Prevents path traversal by ensuring the resolved path stays
        within the base path.
        """
        if bucket:
            resolved = (self._base_path / bucket / path).resolve()
        else:
            resolved = (self._base_path / path).resolve()

        # Use a real path-boundary check, not a string prefix: a prefix match
        # lets a sibling directory that shares the base's name escape the base
        # (base=/data/store, path=../store-secrets/x -> /data/store-secrets/x
        # passes ``startswith('/data/store')``). ``is_relative_to`` compares path
        # components, so only true descendants (and the base itself) are allowed (#11).
        if resolved != self._base_path and not resolved.is_relative_to(self._base_path):
            raise PermissionError(f"Path traversal blocked: {path}")

        return resolved

    def read_file(self, path: str, bucket: str | None = None) -> bytes:
        """Read a file from the local filesystem.

        Args:
            path: Relative path to the file
            bucket: Storage bucket (subdirectory under base path)
        """
        resolved = self._resolve(path, bucket)

        if not resolved.is_file():
            raise FileNotFoundError(f"File not found: {resolved}")

        content = resolved.read_bytes()
        logger.info("Read file from local storage", path=str(resolved), size=len(content))
        return content

    def file_exists(self, path: str, bucket: str | None = None) -> bool:
        """Check if a file exists on disk."""
        try:
            return self._resolve(path, bucket).is_file()
        except (PermissionError, ValueError):
            return False

    def get_size(self, path: str, bucket: str | None = None) -> int:
        """Return file size via stat — no content read (#22)."""
        return self._resolve(path, bucket).stat().st_size


class StorageService:
    """Multi-backend storage service."""

    def __init__(self, settings: Settings):
        """Initialize storage service."""
        self.settings = settings
        self._backends: dict[str, BaseStorageBackend] = {}
        self._connected = False

    def connect(self) -> None:
        """Initialize all storage backends."""
        if self._connected:
            return

        # Always initialize local backend for reading from disk
        local_backend = LocalStorageBackend(self.settings)
        local_backend.connect()
        self._backends["local"] = local_backend

        # Initialize S3 if configured
        if self.settings.storage_backend == "s3":
            try:
                s3_backend = S3StorageBackend(self.settings)
                s3_backend.connect()
                self._backends["s3"] = s3_backend
            except Exception as e:
                logger.warning("S3 backend not available", error=str(e))

        self._connected = True
        logger.info(
            "Storage service connected",
            backends=list(self._backends.keys()),
        )

    def disconnect(self) -> None:
        """Disconnect all storage backends."""
        for name, backend in self._backends.items():
            try:
                backend.disconnect()
            except Exception as e:
                logger.warning(f"Error disconnecting {name} backend", error=str(e))
        self._backends.clear()
        self._connected = False
        logger.info("Storage service disconnected")

    def get_backend(self, backend_type: str) -> BaseStorageBackend:
        """Get a specific storage backend."""
        if backend_type not in self._backends:
            raise ValueError(
                f"Backend '{backend_type}' not available. Available: {list(self._backends.keys())}"
            )
        return self._backends[backend_type]

    def read_file(self, path: str, backend: str = "local", bucket: str | None = None) -> bytes:
        """Read a file from the specified backend."""
        storage_backend = self.get_backend(backend)
        return storage_backend.read_file(path, bucket)

    def read_file_from_url(self, url: str) -> bytes:
        """Read a file directly from a URL (validated against SSRF, #34)."""
        _validate_fetch_url(url)
        logger.info("Fetching file from URL", url=url)
        # follow_redirects=False: a redirect could point at an internal host that
        # bypasses the pre-fetch validation.
        with httpx.Client(timeout=60.0, follow_redirects=False) as client:
            response = client.get(url)
            response.raise_for_status()
            return response.content
