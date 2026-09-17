"""Shared contract for terminal DocumentIngestionWorkflow failure (#230).

ApplicationError.type must match at:
- the workflow raise site
- /ingest?wait=true mapping in api/app.py
- sync TemporalWorkflowTrigger mapping

Keep the string in one place so those call sites cannot drift.
"""

from __future__ import annotations

# temporalio.exceptions.ApplicationError(type=...) for a failed document
# after status / DLQ / document.failed / staging cleanup.
DOCUMENT_INGESTION_FAILED_TYPE = "DocumentIngestionFailed"
