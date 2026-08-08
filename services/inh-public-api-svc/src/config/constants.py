"""Constants for the public API service."""

from typing import Final, Literal

from inh_contracts.file_types import all_mime_types

# API version
API_VERSION: Final[str] = "v1"

# Default Postgres database name — single-sourced so the local DATABASE_URL
# default and the Cloud SQL database default can't drift apart (they name
# the same database).
DEFAULT_DATABASE_NAME: Final[str] = "knowledge_base"

# Default rate limit for keys without an explicit limit (see ApiKey.rate_limit)
DEFAULT_RATE_LIMIT: Final[int] = 100

# Rate limit window in seconds
RATE_LIMIT_WINDOW_SECONDS: Final[int] = 60

# RFC 7807 Error type URLs
ERROR_BASE_URL: Final[str] = "https://api.inherent.systems/errors"

ERROR_TYPES: Final[dict[str, str]] = {
    "authentication_failed": f"{ERROR_BASE_URL}/authentication-failed",
    "authorization_failed": f"{ERROR_BASE_URL}/authorization-failed",
    "rate_limit_exceeded": f"{ERROR_BASE_URL}/rate-limit-exceeded",
    "resource_not_found": f"{ERROR_BASE_URL}/resource-not-found",
    "validation_error": f"{ERROR_BASE_URL}/validation-error",
    "service_unavailable": f"{ERROR_BASE_URL}/service-unavailable",
    "internal_error": f"{ERROR_BASE_URL}/internal-error",
    "bad_request": f"{ERROR_BASE_URL}/bad-request",
}

# Error titles (human-readable)
ERROR_TITLES: Final[dict[str, str]] = {
    "authentication_failed": "Authentication Failed",
    "authorization_failed": "Authorization Failed",
    "rate_limit_exceeded": "Rate Limit Exceeded",
    "resource_not_found": "Resource Not Found",
    "validation_error": "Validation Error",
    "service_unavailable": "Service Unavailable",
    "internal_error": "Internal Server Error",
    "bad_request": "Bad Request",
}

# Security headers
SECURITY_HEADERS: Final[dict[str, str]] = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "X-XSS-Protection": "1; mode=block",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
    "Cache-Control": "no-store, no-cache, must-revalidate",
    "Pragma": "no-cache",
}

# HSTS header (only for production)
HSTS_HEADER: Final[str] = "max-age=31536000; includeSubDomains"

# Request ID header names
REQUEST_ID_HEADER: Final[str] = "X-Request-ID"
CORRELATION_ID_HEADER: Final[str] = "X-Correlation-ID"

# Rate limit response headers
RATE_LIMIT_HEADERS: Final[dict[str, str]] = {
    "limit": "X-RateLimit-Limit",
    "remaining": "X-RateLimit-Remaining",
    "reset": "X-RateLimit-Reset",
    "retry_after": "Retry-After",
}

# Health check statuses
HealthStatus = Literal["healthy", "degraded", "unhealthy"]
HEALTH_STATUS_HEALTHY: Final[HealthStatus] = "healthy"
HEALTH_STATUS_DEGRADED: Final[HealthStatus] = "degraded"
HEALTH_STATUS_UNHEALTHY: Final[HealthStatus] = "unhealthy"

# Upload limits
MAX_UPLOAD_SIZE_BYTES: Final[int] = 50 * 1024 * 1024  # 50 MB

# Derived from the single file-type registry (#117) --
# services/inh-contracts/src/inh_contracts/file_types.py. Do NOT hand-edit
# this list: it used to be its own hand-maintained copy that could (and did)
# drift from the MCP surface's own copy and from extraction's dispatch table.
# Add/remove a supported type by editing FILE_TYPE_REGISTRY instead.
ALLOWED_MIME_TYPES: Final[list[str]] = all_mime_types()

# Search constraints
MAX_SEARCH_QUERY_LENGTH: Final[int] = 1000
MAX_SEARCH_RESULTS: Final[int] = 100
DEFAULT_SEARCH_RESULTS: Final[int] = 10
MIN_SEARCH_SCORE: Final[float] = 0.0

# Pagination
MAX_PAGE_SIZE: Final[int] = 100
DEFAULT_PAGE_SIZE: Final[int] = 20

# Health-check timeouts (#203): moved to Settings
# (database_health_check_timeout_seconds / weaviate_health_check_timeout_seconds)
# so the operator-facing env vars actually reach the health endpoints. These
# constants used to shadow that Settings field completely -- api/v1/health.py
# read them instead, so the setting had zero call sites and setting it did
# nothing. Do not reintroduce a hardcoded timeout constant here; add a new
# Settings field instead.
