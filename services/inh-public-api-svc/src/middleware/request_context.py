"""Request context middleware for correlation IDs and request tracking.

This middleware:
1. Generates or extracts X-Request-ID for correlation
2. Records request start time for latency tracking
3. Binds context to structlog for all subsequent logs
4. Adds request ID to response headers
"""

import re
import time
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

import structlog
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from src.config import settings
from src.config.constants import CORRELATION_ID_HEADER, REQUEST_ID_HEADER

# Context variable for storing request context across async calls
_request_context: ContextVar["RequestContext | None"] = ContextVar("request_context", default=None)


@dataclass
class RequestContext:
    """Request context data available throughout the request lifecycle."""

    request_id: str
    correlation_id: str
    start_time: float = field(default_factory=time.time)
    method: str = ""
    path: str = ""
    client_ip: str | None = None
    user_agent: str | None = None
    user_id: str | None = None
    workspace_id: str | None = None
    key_id: str | None = None

    @property
    def duration_ms(self) -> float:
        """Calculate request duration in milliseconds."""
        return (time.time() - self.start_time) * 1000

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for logging."""
        return {
            "request_id": self.request_id,
            "correlation_id": self.correlation_id,
            "method": self.method,
            "path": self.path,
            "client_ip": self.client_ip,
            "user_id": self.user_id,
            "workspace_id": self.workspace_id,
            "key_id": self.key_id,
        }


def get_request_context() -> RequestContext | None:
    """Get the current request context from context variables."""
    return _request_context.get()


def set_request_context(context: RequestContext) -> None:
    """Set the current request context."""
    _request_context.set(context)


def update_request_context(
    user_id: str | None = None,
    workspace_id: str | None = None,
    key_id: str | None = None,
) -> None:
    """Update the current request context with authentication info.

    Call this after authentication to add user context to logs.
    """
    ctx = get_request_context()
    if ctx:
        if user_id is not None:
            ctx.user_id = user_id
        if workspace_id is not None:
            ctx.workspace_id = workspace_id
        if key_id is not None:
            ctx.key_id = key_id
        # Re-bind to structlog
        _bind_context_to_structlog(ctx)


def _generate_request_id() -> str:
    """Generate a new request ID."""
    return str(uuid.uuid4())


_REQUEST_ID_MAX_LEN = 128
_REQUEST_ID_ALLOWED = re.compile(r"[^A-Za-z0-9._-]")


def _sanitize_request_id(value: str) -> str:
    """Strip control chars / markup and cap length on a client-supplied id.

    A client-supplied X-Request-ID is untrusted: unsanitized it can inject
    newlines/markup into logs and audit records (#16).
    """
    return _REQUEST_ID_ALLOWED.sub("", value)[:_REQUEST_ID_MAX_LEN]


def _get_client_ip(request: Request) -> str | None:
    """Extract the client IP, trusting forwarded headers only from a proxy.

    X-Forwarded-For / X-Real-IP are trusted only when the *direct peer*
    (``request.client.host``) is in ``settings.trusted_proxies``; otherwise any
    client could forge its audited IP (#16). Default trusted set is empty.
    """
    peer = request.client.host if request.client else None

    if peer and peer in settings.trusted_proxies:
        forwarded_for = request.headers.get("X-Forwarded-For")
        if forwarded_for:
            # Client-closest untrusted IP is the first entry.
            return forwarded_for.split(",")[0].strip()
        real_ip = request.headers.get("X-Real-IP")
        if real_ip:
            return real_ip

    return peer


def _bind_context_to_structlog(context: RequestContext) -> None:
    """Bind request context to structlog for all subsequent logs."""
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(
        request_id=context.request_id,
        correlation_id=context.correlation_id,
        method=context.method,
        path=context.path,
    )
    if context.user_id:
        structlog.contextvars.bind_contextvars(user_id=context.user_id)
    if context.workspace_id:
        structlog.contextvars.bind_contextvars(workspace_id=context.workspace_id)
    if context.key_id:
        structlog.contextvars.bind_contextvars(key_id=context.key_id)


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Middleware that establishes request context for tracing and logging."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        # Extract or generate request ID. A client-supplied id is sanitized
        # (untrusted) and regenerated if it sanitizes to empty (#16).
        raw_request_id = request.headers.get(REQUEST_ID_HEADER)
        request_id = _sanitize_request_id(raw_request_id) if raw_request_id else ""
        if not request_id:
            request_id = _generate_request_id()

        # Extract or use request ID as correlation ID (also sanitized).
        raw_correlation_id = request.headers.get(CORRELATION_ID_HEADER)
        correlation_id = _sanitize_request_id(raw_correlation_id) if raw_correlation_id else ""
        if not correlation_id:
            correlation_id = request_id

        # Create context
        context = RequestContext(
            request_id=request_id,
            correlation_id=correlation_id,
            start_time=time.time(),
            method=request.method,
            path=request.url.path,
            client_ip=_get_client_ip(request),
            user_agent=request.headers.get("User-Agent"),
        )

        # Set context variable
        set_request_context(context)

        # Bind to structlog
        _bind_context_to_structlog(context)

        # Store on request state for access in route handlers
        request.state.request_context = context

        # Process request
        response = await call_next(request)

        # Add request ID to response headers
        response.headers[REQUEST_ID_HEADER] = request_id
        if correlation_id != request_id:
            response.headers[CORRELATION_ID_HEADER] = correlation_id

        return response
