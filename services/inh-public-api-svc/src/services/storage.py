"""S3-compatible storage service for document uploads.

Uploads files to an S3-compatible bucket (Hetzner Object Storage, AWS S3, MinIO, s3rver).
Uses boto3 with run_in_executor to avoid blocking the async event loop.
"""

from __future__ import annotations

import asyncio
import re
import uuid

import boto3
from botocore.config import Config as BotoConfig

from src.config import settings
from src.utils import get_logger

logger = get_logger(__name__)


class StorageService:
    """S3-compatible storage service."""

    def __init__(self, s3_settings=None) -> None:
        s = s3_settings or settings
        client_kwargs: dict = {
            "region_name": s.aws_s3_region,
            "aws_access_key_id": s.aws_access_key_id,
            "aws_secret_access_key": s.aws_secret_access_key,
            "config": BotoConfig(signature_version="s3v4"),
        }
        if s.aws_s3_endpoint:
            client_kwargs["endpoint_url"] = s.aws_s3_endpoint

        self._client = boto3.client("s3", **client_kwargs)
        self._bucket = s.aws_s3_bucket
        logger.info(
            "StorageService initialized",
            bucket=self._bucket,
            endpoint=s.aws_s3_endpoint or "default",
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def upload_file(self, file_content: bytes, key: str, content_type: str) -> str:
        """Upload *file_content* to S3 under *key*. Returns the S3 key."""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            self._put_object,
            key,
            file_content,
            content_type,
        )
        logger.info(
            "File uploaded to S3",
            bucket=self._bucket,
            key=key,
            size=len(file_content),
            content_type=content_type,
        )
        return key

    async def delete_file(self, key: str) -> None:
        """Delete the object stored under *key* (#87 document delete).

        S3 DeleteObject is idempotent: deleting a missing key succeeds, so
        callers don't need an existence check first.
        """
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._delete_object, key)
        logger.info("File deleted from S3", bucket=self._bucket, key=key)

    def generate_key(self, workspace_id: str, filename: str) -> str:
        """Generate a deterministic-format S3 object key.

        Format mirrors intg-svc: ``workspaces/{workspace_id}/{timestamp}-{uuid}-{sanitized}``
        but simplified to ``{workspace_id}/{uuid}/{filename}`` for the public API.
        """
        safe_name = self._sanitize_filename(filename)
        return f"{workspace_id}/{uuid.uuid4()}/{safe_name}"

    def build_storage_url(self, key: str) -> str:
        """Return an ``s3://{bucket}/{key}`` URI for the uploaded object."""
        return f"s3://{self._bucket}/{key}"

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _put_object(self, key: str, body: bytes, content_type: str) -> None:
        """Synchronous S3 PutObject (called inside run_in_executor)."""
        self._client.put_object(
            Bucket=self._bucket,
            Key=key,
            Body=body,
            ContentType=content_type,
        )

    def _delete_object(self, key: str) -> None:
        """Synchronous S3 DeleteObject (called inside run_in_executor)."""
        self._client.delete_object(Bucket=self._bucket, Key=key)

    @staticmethod
    def _sanitize_filename(filename: str) -> str:
        """Strip unsafe characters, keeping extension."""
        # Replace anything except alphanumeric, dots, hyphens, underscores
        return re.sub(r"[^a-zA-Z0-9._-]", "_", filename)[:255]


# ---------------------------------------------------------------------------
# Singleton management
# ---------------------------------------------------------------------------

_storage_service: StorageService | None = None


def get_storage_service() -> StorageService:
    """Return (and lazily create) the singleton StorageService."""
    global _storage_service
    if _storage_service is None:
        _storage_service = StorageService()
    return _storage_service


async def close_storage_service() -> None:
    """Tear down the StorageService singleton (idempotent)."""
    global _storage_service
    if _storage_service is not None:
        logger.info("StorageService closed")
        _storage_service = None
