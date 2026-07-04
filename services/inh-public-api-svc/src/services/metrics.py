"""Prometheus metrics service.

Provides counters and histograms for monitoring API performance and health.
"""

from prometheus_client import Counter, Gauge, Histogram, generate_latest

# Request metrics
REQUEST_COUNT = Counter(
    "http_requests_total",
    "Total number of HTTP requests",
    ["method", "endpoint", "status_code"],
)

REQUEST_LATENCY = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency in seconds",
    ["method", "endpoint"],
    buckets=[0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0],
)

ACTIVE_REQUESTS = Gauge(
    "http_requests_active",
    "Number of active HTTP requests",
)

# Authentication metrics
AUTH_FAILURES = Counter(
    "auth_failures_total",
    "Total number of authentication failures",
    ["reason"],
)

# Rate limiting metrics
# No per-key label: key_id is unbounded and would create one series per key
# that Prometheus never frees (#20). Track key identity in logs/audit instead.
RATE_LIMIT_EXCEEDED = Counter(
    "rate_limit_exceeded_total",
    "Total number of rate limit exceeded events",
)

# Database metrics
DATABASE_QUERY_LATENCY = Histogram(
    "database_query_duration_seconds",
    "Database query latency in seconds",
    ["query_type"],
    buckets=[0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0],
)

DATABASE_ERRORS = Counter(
    "database_errors_total",
    "Total number of database errors",
    ["error_type"],
)

# Search metrics
SEARCH_LATENCY = Histogram(
    "weaviate_search_duration_seconds",
    "Weaviate search latency in seconds",
    buckets=[0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0],
)

SEARCH_RESULTS = Histogram(
    "search_results_count",
    "Number of search results returned",
    buckets=[0, 1, 5, 10, 25, 50, 100],
)

# Labeled by mode only: workspace_id is unbounded and would leak a series per
# tenant (#20). Per-tenant search volume belongs in logs, not metric labels.
search_requests_total = Counter(
    "search_requests_total",
    "Number of /v1/search requests by mode",
    ["mode"],
)

search_context_requests_total = Counter(
    "search_context_requests_total",
    "Number of /v1/search requests with include_context=true, bucketed by k",
    ["k"],
)

search_chunks_missing_token_count_total = Counter(
    "search_chunks_missing_token_count_total",
    "Chunks with NULL token_count encountered during search total_tokens computation",
)

search_errors_total = Counter(
    "search_errors_total",
    "Search errors by mode and type",
    ["mode", "error_type"],
)

# Health check metrics
HEALTH_CHECK_STATUS = Gauge(
    "health_check_status",
    "Current health check status (1=healthy, 0.5=degraded, 0=unhealthy)",
    ["component"],
)


def record_request(method: str, endpoint: str, status_code: int) -> None:
    """Record an HTTP request."""
    REQUEST_COUNT.labels(method=method, endpoint=endpoint, status_code=str(status_code)).inc()


def observe_request_latency(method: str, endpoint: str, duration_seconds: float) -> None:
    """Observe HTTP request latency."""
    REQUEST_LATENCY.labels(method=method, endpoint=endpoint).observe(duration_seconds)


def increment_active_requests() -> None:
    """Increment active requests gauge."""
    ACTIVE_REQUESTS.inc()


def decrement_active_requests() -> None:
    """Decrement active requests gauge."""
    ACTIVE_REQUESTS.dec()


def record_auth_failure(reason: str) -> None:
    """Record an authentication failure."""
    AUTH_FAILURES.labels(reason=reason).inc()


def record_rate_limit_exceeded() -> None:
    """Record a rate limit exceeded event."""
    RATE_LIMIT_EXCEEDED.inc()


def observe_database_latency(query_type: str, duration_seconds: float) -> None:
    """Observe database query latency."""
    DATABASE_QUERY_LATENCY.labels(query_type=query_type).observe(duration_seconds)


def record_database_error(error_type: str) -> None:
    """Record a database error."""
    DATABASE_ERRORS.labels(error_type=error_type).inc()


def observe_search_latency(duration_seconds: float) -> None:
    """Observe Weaviate search latency."""
    SEARCH_LATENCY.observe(duration_seconds)


def observe_search_results(count: int) -> None:
    """Observe number of search results."""
    SEARCH_RESULTS.observe(count)


def record_search_request(mode: str) -> None:
    """Record a search request by mode."""
    search_requests_total.labels(mode=mode).inc()


def record_search_context_request(k: int) -> None:
    """Record a search request with include_context=true, bucketed by k."""
    search_context_requests_total.labels(k=str(k)).inc()


def record_search_chunks_missing_token_count(count: int) -> None:
    """Record chunks with NULL token_count encountered during total_tokens computation."""
    search_chunks_missing_token_count_total.inc(count)


def record_search_error(mode: str, error_type: str) -> None:
    """Record a search error by mode and error type."""
    search_errors_total.labels(mode=mode, error_type=error_type).inc()


def set_health_status(component: str, status: str) -> None:
    """Set health check status for a component.

    Args:
        component: Component name (e.g., "database", "weaviate").
        status: Status string ("healthy", "degraded", "unhealthy").
    """
    status_value = {"healthy": 1.0, "degraded": 0.5, "unhealthy": 0.0}.get(status, 0.0)
    HEALTH_CHECK_STATUS.labels(component=component).set(status_value)


def get_metrics() -> bytes:
    """Generate Prometheus metrics output."""
    return generate_latest()
