"""Main entry point for inh-public-api-svc."""

import asyncio
import sys
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware

from src.api import router
from src.api.v1 import health as health_router
from src.config import settings
from src.mcp_server.http_transport import mount_mcp_http
from src.middleware import (
    AuditLoggingMiddleware,
    AuthenticationMiddleware,
    ErrorHandlerMiddleware,
    RateLimitingMiddleware,
    RequestContextMiddleware,
    SecurityHeadersMiddleware,
)
from src.middleware.error_handler import setup_exception_handlers
from src.services.database import close_database, get_database
from src.services.mq import close_mq_service
from src.services.search import close_search_service
from src.services.storage import close_storage_service
from src.utils import configure_logging, get_logger

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application lifecycle."""
    # Startup
    configure_logging(settings.log_level, json_format=settings.is_production)
    logger.info(
        "Starting inh-public-api-svc",
        mode=settings.service_mode,
        environment=settings.environment,
        version=settings.version,
    )

    # Initialize database
    await get_database()

    # Enter the Streamable HTTP MCP session manager's task-group context
    # (#220) -- StreamableHTTPSessionManager.handle_request raises
    # RuntimeError until .run() has been entered at least once, since that's
    # what creates the anyio task group requests are dispatched onto (see
    # mount_mcp_http in src/mcp_server/http_transport.py). Nested inside this
    # existing lifespan rather than a second one so /mcp shares the exact
    # same startup/shutdown ordering (DB ready first, MQ/storage/search
    # closed last) as the rest of the app.
    async with app.state.mcp_session_manager.run():
        yield

    # Shutdown
    logger.info("Shutting down inh-public-api-svc")
    await close_mq_service()
    await close_storage_service()
    await close_search_service()
    await close_database()


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(
        title="Inherent Knowledge Base API",
        description="Customer-facing API for accessing the Inherent knowledge base",
        version=settings.version,
        lifespan=lifespan,
        docs_url="/docs" if settings.is_development else None,
        redoc_url="/redoc" if settings.is_development else None,
        # The schema is unauthenticated, so leaving it on in production listed
        # every route -- including the flag-gated /v1/admin/* surface, whose
        # 404-not-403 design exists precisely so its existence is not
        # confirmable. Gate it with the docs it serves.
        openapi_url="/openapi.json" if settings.is_development else None,
    )

    # Register exception handlers for RFC 7807 responses
    setup_exception_handlers(app)

    # Middleware stack. Starlette's add_middleware PREPENDS to the stack and
    # builds it with reversed() (see BaseHTTPMiddleware / build_middleware_stack) --
    # so the LAST middleware added is the OUTERMOST one and runs first on the
    # request. Registration order below is therefore the reverse of the desired
    # request flow (#149 follow-up: a same-order-as-here-but-not-reversed stack
    # previously ran RateLimit/Audit before Auth ever set request.state, so every
    # request looked unauthenticated to both).
    # Request flow: CORS -> Security -> Context -> ErrorHandler -> Auth -> Audit -> Rate Limit -> Handler
    # Response flow: Handler -> Rate Limit -> Audit -> Auth -> ErrorHandler -> Context -> Security -> CORS
    # ErrorHandler sits INSIDE Context (so get_request_context() still resolves
    # a trace_id, #222) and OUTSIDE Auth/Audit/Rate Limit (so an exception any
    # of those three raise is still rendered as problem+json, not a bare 500).

    # 6. Rate limiting (added first = innermost; reads api_key_info from state)
    app.add_middleware(RateLimitingMiddleware)

    # 5. Audit logging (logs after response, reads api_key_info from state)
    app.add_middleware(AuditLoggingMiddleware)

    # 4. Authentication (populates request.state.api_key_info for downstream middleware)
    app.add_middleware(AuthenticationMiddleware)

    # 3.5. Error handler (#222): catches any exception RateLimiting/AuditLogging/
    # Auth/the router raise that Starlette's ExceptionMiddleware has no specific
    # handler for (i.e. not InherentAPIError/RequestValidationError/HTTPException --
    # those are handled by setup_exception_handlers() above, inside
    # ExceptionMiddleware, unaffected by this). MUST be added before
    # RequestContextMiddleware (= positioned INSIDE it, wrapping everything below)
    # so get_request_context() inside _handle_unexpected_error still sees the
    # request_id/trace_id RequestContextMiddleware set. Registering the same
    # catch-all via @app.exception_handler(Exception) instead (as this service
    # used to) silently routes it to Starlette's ServerErrorMiddleware -- the
    # true outermost layer, OUTSIDE RequestContextMiddleware -- which is exactly
    # why trace_id was null on unhandled-exception 500s (see
    # tests/integration/test_trace_id_on_5xx.py).
    app.add_middleware(ErrorHandlerMiddleware)

    # 3. Request context (correlation IDs, timing)
    app.add_middleware(RequestContextMiddleware)

    # 2. Security headers
    app.add_middleware(SecurityHeadersMiddleware)

    # 1. CORS (added last = outermost)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list,
        allow_credentials=settings.cors_allow_credentials_effective,
        allow_methods=settings.cors_allow_methods,
        allow_headers=settings.cors_allow_headers,
    )

    # Metrics endpoint
    if settings.metrics_enabled:
        from src.services.metrics import get_metrics

        @app.get(settings.metrics_path, include_in_schema=False)
        async def metrics():
            return Response(
                content=get_metrics(),
                media_type="text/plain; version=0.0.4; charset=utf-8",
            )

    # Include health check router at root level
    app.include_router(health_router.router)

    # Include API router
    app.include_router(router)

    # Mount the Streamable HTTP MCP transport at POST /mcp (#220): same
    # process/port as REST, so CORS, security headers, request context,
    # authentication, audit logging, and rate limiting above all apply to
    # /mcp by construction. The returned session manager is entered in
    # `lifespan()` above -- store it on app.state so that closure can reach
    # it (lifespan is defined before `app` exists).
    app.state.mcp_session_manager = mount_mcp_http(app)

    return app


async def run_api_server() -> None:
    """Run the REST API server."""
    app = create_app()
    config = uvicorn.Config(
        app,
        host="0.0.0.0",  # nosec B104
        port=settings.effective_api_port,
        log_level=settings.log_level.lower(),
    )
    server = uvicorn.Server(config)
    await server.serve()


async def run_mcp_server() -> None:
    """Run the MCP server."""
    from src.mcp_server.server import run_mcp_server

    await run_mcp_server()


async def run_both() -> None:
    """Run both API and MCP servers."""
    # For "both" mode, we run API server and MCP listens on stdio
    # In practice, you'd run API server and have MCP as a separate process
    await run_api_server()


async def main() -> None:
    """Main entry point."""
    configure_logging(settings.log_level, json_format=settings.is_production)

    mode = settings.service_mode

    if mode == "api":
        await run_api_server()
    elif mode == "mcp":
        await run_mcp_server()
    elif mode == "both":
        await run_both()
    else:
        logger.error(f"Unknown service mode: {mode}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
