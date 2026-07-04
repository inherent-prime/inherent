"""Staging service for large intermediate data during Temporal workflows.

Activities write extracted text and chunks here instead of passing them
through Temporal gRPC (which has a 4MB payload limit). Each workflow run
gets its own staging rows, cleaned up on completion.
"""

import json
from datetime import UTC, datetime, timedelta
from typing import cast

import structlog
from sqlalchemy import Column, DateTime, MetaData, String, Table, Text, text
from sqlalchemy.dialects.postgresql import JSONB

from src.config.settings import Settings
from src.services.database import DatabaseService

logger = structlog.get_logger(__name__)

# Table definition (mirrors the migration)
_metadata = MetaData()

ingestion_staging = Table(
    "ingestion_staging",
    _metadata,
    Column("workflow_run_id", String, primary_key=True),
    Column("data_key", String, primary_key=True),
    Column("text_data", Text, nullable=True),
    Column("json_data", JSONB, nullable=True),
    Column("created_at", DateTime(timezone=True), default=lambda: datetime.now(UTC)),
)


class StagingService:
    """Read/write helper for the ingestion_staging table.

    Uses the same DatabaseService / SQLAlchemy engine pattern as the rest
    of the ingestion service.
    """

    def __init__(self, settings: Settings) -> None:
        self._db = DatabaseService(settings)

    def connect(self) -> None:
        self._db.connect()

    def disconnect(self) -> None:
        self._db.disconnect()

    # ------------------------------------------------------------------
    # Write helpers
    # ------------------------------------------------------------------

    def write_text(self, workflow_run_id: str, data: str) -> None:
        """Store extracted text for a workflow run."""
        with self._db.get_session() as session:
            session.execute(
                text(
                    """
                    INSERT INTO ingestion_staging (workflow_run_id, data_key, text_data, created_at)
                    VALUES (:wf_id, 'extracted_text', :data, NOW())
                    ON CONFLICT (workflow_run_id, data_key)
                    DO UPDATE SET text_data = EXCLUDED.text_data
                    """
                ),
                {"wf_id": workflow_run_id, "data": data},
            )
            session.commit()

        logger.debug(
            "Wrote extracted text to staging",
            workflow_run_id=workflow_run_id,
            text_length=len(data),
        )

    def read_text(self, workflow_run_id: str) -> str:
        """Read extracted text for a workflow run."""
        with self._db.get_session() as session:
            row = session.execute(
                text(
                    """
                    SELECT text_data FROM ingestion_staging
                    WHERE workflow_run_id = :wf_id AND data_key = 'extracted_text'
                    """
                ),
                {"wf_id": workflow_run_id},
            ).fetchone()

        if row is None:
            raise RuntimeError(
                f"No extracted text found in staging for workflow_run_id={workflow_run_id}"
            )
        if row[0] is None:
            raise RuntimeError(
                f"Staging row had null extracted text for workflow_run_id={workflow_run_id}"
            )
        return str(row[0])

    def write_chunks(self, workflow_run_id: str, chunks: list[dict]) -> None:
        """Store chunks as JSONB for a workflow run."""
        with self._db.get_session() as session:
            session.execute(
                text(
                    """
                    INSERT INTO ingestion_staging (workflow_run_id, data_key, json_data, created_at)
                    VALUES (:wf_id, 'chunks', CAST(:data AS jsonb), NOW())
                    ON CONFLICT (workflow_run_id, data_key)
                    DO UPDATE SET json_data = EXCLUDED.json_data
                    """
                ),
                {"wf_id": workflow_run_id, "data": json.dumps(chunks)},
            )
            session.commit()

        logger.debug(
            "Wrote chunks to staging",
            workflow_run_id=workflow_run_id,
            chunk_count=len(chunks),
        )

    def read_chunks(self, workflow_run_id: str) -> list[dict]:
        """Read chunks for a workflow run."""
        with self._db.get_session() as session:
            row = session.execute(
                text(
                    """
                    SELECT json_data FROM ingestion_staging
                    WHERE workflow_run_id = :wf_id AND data_key = 'chunks'
                    """
                ),
                {"wf_id": workflow_run_id},
            ).fetchone()

        if row is None:
            raise RuntimeError(f"No chunks found in staging for workflow_run_id={workflow_run_id}")
        if row[0] is None:
            raise RuntimeError(f"Staging row had null chunks for workflow_run_id={workflow_run_id}")

        value = row[0]
        if isinstance(value, str):
            # Stored as JSON text in json_data, so decode if needed.
            return cast(list[dict], json.loads(value))

        return cast(list[dict], value)

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def cleanup(self, workflow_run_id: str) -> None:
        """Delete all staging rows for a workflow run."""
        with self._db.get_session() as session:
            session.execute(
                text("DELETE FROM ingestion_staging WHERE workflow_run_id = :wf_id"),
                {"wf_id": workflow_run_id},
            )
            session.commit()

        logger.debug("Cleaned up staging", workflow_run_id=workflow_run_id)

    def cleanup_stale(self, max_age_hours: int = 1) -> int:
        """Delete staging rows older than max_age_hours. Safety net for crashed workflows.

        Returns:
            Number of rows deleted.
        """
        cutoff = datetime.now(UTC) - timedelta(hours=max_age_hours)
        with self._db.get_session() as session:
            result = session.execute(
                text("DELETE FROM ingestion_staging WHERE created_at < :cutoff"),
                {"cutoff": cutoff},
            )
            session.commit()
            deleted = int(result.rowcount or 0)

        if deleted > 0:
            logger.info(
                "Cleaned up stale staging rows", deleted=deleted, max_age_hours=max_age_hours
            )
        return deleted
