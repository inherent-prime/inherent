"""Prometheus metrics for the ingestion service.

Provides counters, histograms, and gauges for monitoring ingestion
workflows, HTTP requests, and database operations.
"""

from prometheus_client import Counter, Gauge, Histogram, generate_latest

# ── HTTP Metrics ─────────────────────────────────────────────────────

HTTP_REQUEST_COUNT = Counter(
    "http_requests_total",
    "Total number of HTTP requests",
    ["method", "endpoint", "status_code"],
)

HTTP_REQUEST_LATENCY = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency in seconds",
    ["method", "endpoint"],
    buckets=[0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0],
)

# ── Ingestion Workflow Metrics ───────────────────────────────────────

WORKFLOW_RUNS_TOTAL = Counter(
    "ingestion_workflow_runs_total",
    "Total number of ingestion workflow runs",
    ["status"],
)

WORKFLOW_DURATION = Histogram(
    "ingestion_workflow_duration_seconds",
    "Ingestion workflow duration in seconds",
    buckets=[1.0, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0, 600.0],
)

CHUNKS_CREATED_TOTAL = Counter(
    "ingestion_chunks_created_total",
    "Total number of chunks created by ingestion",
)

DOCUMENTS_PROCESSED_TOTAL = Counter(
    "ingestion_documents_processed_total",
    "Total number of documents processed",
    ["content_type"],
)

# Audit messages dropped for a permanently-invalid payload (e.g. missing
# audit_id). Retrying can't fix these, so they are dropped — but the drop must
# be observable, not silent (#18). `reason` is a small bounded set.
AUDIT_MESSAGES_DROPPED_TOTAL = Counter(
    "ingestion_audit_messages_dropped_total",
    "Audit messages dropped due to a permanently-invalid payload",
    ["reason"],
)

# Completion notifications that failed to publish. The publish is best-effort
# (a failure must not fail the document), but the drop must be observable rather
# than a silent loss that leaves the doc stuck "processing" downstream (#37).
COMPLETION_PUBLISH_FAILURES_TOTAL = Counter(
    "ingestion_completion_publish_failures_total",
    "Completion notifications that failed to publish",
)

# ── Backpressure / MQ Metrics (#18) ──────────────────────────────────

# Latency from receiving a message off the MQ to Temporal accepting the
# workflow start (i.e. the async start returns). This is the "admission"
# latency, NOT end-to-end processing time (see WORKFLOW_DURATION for that).
WORKFLOW_START_LATENCY = Histogram(
    "ingestion_workflow_start_latency_seconds",
    "Latency from MQ message receive to Temporal accepting the workflow start",
    buckets=[0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0],
)

# Number of pending (delivered-but-unACKed) messages on an MQ stream/group.
# A persistently growing value indicates the consumer is falling behind
# (backpressure building up). Set best-effort via XPENDING.
MQ_STREAM_PENDING = Gauge(
    "ingestion_mq_stream_pending",
    "Number of pending (unacknowledged) messages on the MQ stream",
    ["stream", "group"],
)

# ── Database Metrics ─────────────────────────────────────────────────

POSTGRES_QUERY_DURATION = Histogram(
    "postgres_query_duration_seconds",
    "PostgreSQL query latency in seconds",
    ["operation"],
    buckets=[0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0],
)

WEAVIATE_WRITE_DURATION = Histogram(
    "weaviate_write_duration_seconds",
    "Weaviate write latency in seconds",
    buckets=[0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0],
)

# ── Worker Metrics ───────────────────────────────────────────────────

TEMPORAL_WORKER_RUNNING = Gauge(
    "temporal_worker_running",
    "Whether the Temporal worker is currently running (1=yes, 0=no)",
)


def get_metrics() -> bytes:
    """Generate Prometheus metrics output."""
    return generate_latest()
