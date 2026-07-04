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
from src.middleware import (
    AuditLoggingMiddleware,
    AuthenticationMiddleware,
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
    )

    # Register exception handlers for RFC 7807 responses
    setup_exception_handlers(app)

    # Middleware stack (order matters - first added = outermost)
    # Request flow: CORS -> Security -> Context -> Auth -> Audit -> Rate Limit -> Handler
    # Response flow: Handler -> Rate Limit -> Audit -> Auth -> Context -> Security -> CORS

    # 1. CORS (outermost)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list,
        allow_credentials=settings.cors_allow_credentials_effective,
        allow_methods=settings.cors_allow_methods,
        allow_headers=settings.cors_allow_headers,
    )

    # 2. Security headers
    app.add_middleware(SecurityHeadersMiddleware)

    # 3. Request context (correlation IDs, timing)
    app.add_middleware(RequestContextMiddleware)

    # 4. Authentication (populates request.state.api_key_info for downstream middleware)
    app.add_middleware(AuthenticationMiddleware)

    # 5. Audit logging (logs after response, reads api_key_info from state)
    app.add_middleware(AuditLoggingMiddleware)

    # 6. Rate limiting (reads api_key_info from state)
    app.add_middleware(RateLimitingMiddleware)

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
