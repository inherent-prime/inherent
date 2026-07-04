"""Global error handler middleware.

This middleware catches all exceptions and returns RFC 7807 Problem Details
responses. It ensures consistent error formatting and prevents exposing
sensitive information in production.
"""

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from src.config import settings
from src.core.exceptions import InherentAPIError
from src.core.problem_details import create_problem_detail, from_exception
from src.middleware.request_context import get_request_context
from src.services import metrics
from src.utils import get_logger

logger = get_logger(__name__)

# Map raw HTTP status codes to problem-detail error keys so a plain
# ``fastapi.HTTPException(404/403/...)`` renders as problem+json, consistent with
# InherentAPIError, instead of FastAPI's default ``{"detail": ...}`` (#12).
_STATUS_ERROR_KEYS = {
    400: "bad_request",
    401: "authentication_failed",
    403: "authorization_failed",
    404: "resource_not_found",
    422: "validation_error",
    429: "rate_limit_exceeded",
    503: "service_unavailable",
}


class ErrorHandlerMiddleware(BaseHTTPMiddleware):
    """Middleware that catches exceptions and returns Problem Details responses."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        try:
            return await call_next(request)
        except InherentAPIError as exc:
            return self._handle_api_error(request, exc)
        except RequestValidationError as exc:
            return self._handle_validation_error(request, exc)
        except Exception as exc:
            return self._handle_unexpected_error(request, exc)

    def _handle_api_error(self, request: Request, exc: InherentAPIError) -> JSONResponse:
        """Handle custom API exceptions."""
        ctx = get_request_context()
        trace_id = ctx.request_id if ctx else None

        # Log at appropriate level
        if exc.status_code >= 500:
            logger.error(
                "API error",
                error_type=exc.error_key,
                status_code=exc.status_code,
                detail=exc.detail,
                exc_info=True,
            )
            metrics.record_database_error(exc.error_key)
        else:
            logger.warning(
                "Client error",
                error_type=exc.error_key,
                status_code=exc.status_code,
                detail=exc.detail,
            )

        # Track auth failures
        if exc.status_code == 401:
            metrics.record_auth_failure(exc.error_key)

        return JSONResponse(
            status_code=exc.status_code,
            content=from_exception(exc, instance=request.url.path, trace_id=trace_id),
            media_type="application/problem+json",
        )

    def _handle_validation_error(
        self, request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """Handle FastAPI validation errors."""
        ctx = get_request_context()
        trace_id = ctx.request_id if ctx else None

        # Convert Pydantic errors to our format
        errors = []
        for error in exc.errors():
            errors.append(
                {
                    "loc": list(error.get("loc", [])),
                    "msg": error.get("msg", ""),
                    "type": error.get("type", ""),
                }
            )

        logger.warning(
            "Validation error",
            path=request.url.path,
            errors=errors,
        )

        return JSONResponse(
            status_code=422,
            content=create_problem_detail(
                error_key="validation_error",
                status=422,
                detail="Request validation failed.",
                instance=request.url.path,
                trace_id=trace_id,
                extensions={"errors": errors},
            ),
            media_type="application/problem+json",
        )

    def _handle_unexpected_error(self, request: Request, exc: Exception) -> JSONResponse:
        """Handle unexpected exceptions."""
        ctx = get_request_context()
        trace_id = ctx.request_id if ctx else None

        # Log full exception
        logger.exception(
            "Unexpected error",
            path=request.url.path,
            error_type=type(exc).__name__,
        )

        metrics.record_database_error("unexpected_error")

        # In production, don't expose exception details
        if settings.is_production:
            detail = "An unexpected error occurred. Please try again later."
        else:
            detail = f"{type(exc).__name__}: {exc!s}"

        return JSONResponse(
            status_code=500,
            content=create_problem_detail(
                error_key="internal_error",
                status=500,
                detail=detail,
                instance=request.url.path,
                trace_id=trace_id,
            ),
            media_type="application/problem+json",
        )


def setup_exception_handlers(app):
    """Set up exception handlers for FastAPI app.

    This is an alternative to the middleware approach for cases where
    you want exception handlers at the FastAPI level.
    """
    from fastapi import FastAPI

    if not isinstance(app, FastAPI):
        return

    @app.exception_handler(InherentAPIError)
    async def api_error_handler(request: Request, exc: InherentAPIError) -> JSONResponse:
        ctx = get_request_context()
        trace_id = ctx.request_id if ctx else None

        if exc.status_code >= 500:
            logger.error("API error", error_type=exc.error_key, exc_info=True)
        else:
            logger.warning("Client error", error_type=exc.error_key)

        return JSONResponse(
            status_code=exc.status_code,
            content=from_exception(exc, instance=request.url.path, trace_id=trace_id),
            media_type="application/problem+json",
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        ctx = get_request_context()
        trace_id = ctx.request_id if ctx else None

        errors = [
            {"loc": list(e.get("loc", [])), "msg": e.get("msg", ""), "type": e.get("type", "")}
            for e in exc.errors()
        ]

        return JSONResponse(
            status_code=422,
            content=create_problem_detail(
                error_key="validation_error",
                status=422,
                detail="Request validation failed.",
                instance=request.url.path,
                trace_id=trace_id,
                extensions={"errors": errors},
            ),
            media_type="application/problem+json",
        )

    from starlette.exceptions import HTTPException as StarletteHTTPException

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        ctx = get_request_context()
        trace_id = ctx.request_id if ctx else None

        error_key = _STATUS_ERROR_KEYS.get(exc.status_code, "internal_error")
        detail = exc.detail if isinstance(exc.detail, str) else "Request failed."

        if exc.status_code >= 500:
            logger.error("HTTP error", status_code=exc.status_code, exc_info=True)
        else:
            logger.warning("Client HTTP error", status_code=exc.status_code)

        response = JSONResponse(
            status_code=exc.status_code,
            content=create_problem_detail(
                error_key=error_key,
                status=exc.status_code,
                detail=detail,
                instance=request.url.path,
                trace_id=trace_id,
            ),
            media_type="application/problem+json",
        )
        # Preserve auth challenge headers (e.g. WWW-Authenticate) if present.
        if exc.headers:
            for k, v in exc.headers.items():
                response.headers[k] = v
        return response

    @app.exception_handler(Exception)
    async def generic_error_handler(request: Request, exc: Exception) -> JSONResponse:
        ctx = get_request_context()
        trace_id = ctx.request_id if ctx else None

        logger.exception("Unexpected error", path=request.url.path)

        detail = (
            "An unexpected error occurred."
            if settings.is_production
            else f"{type(exc).__name__}: {exc!s}"
        )

        return JSONResponse(
            status_code=500,
            content=create_problem_detail(
                error_key="internal_error",
                status=500,
                detail=detail,
                instance=request.url.path,
                trace_id=trace_id,
            ),
            media_type="application/problem+json",
        )
