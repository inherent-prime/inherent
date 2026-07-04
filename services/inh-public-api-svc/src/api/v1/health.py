"""Health check endpoints."""

import asyncio
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Response
from sqlalchemy import text

from src.config import settings
from src.config.constants import (
    DATABASE_HEALTH_CHECK_TIMEOUT,
    HEALTH_STATUS_DEGRADED,
    HEALTH_STATUS_HEALTHY,
    HEALTH_STATUS_UNHEALTHY,
    WEAVIATE_HEALTH_CHECK_TIMEOUT,
)
from src.models.health import ComponentHealth, HealthResponse, LivenessResponse
from src.services import metrics
from src.services.database import get_database
from src.services.search import get_search_service
from src.utils import get_logger

router = APIRouter(tags=["Health"])
logger = get_logger(__name__)


@router.get(
    "/health",
    response_model=LivenessResponse,
    summary="Liveness probe",
    description="Simple liveness check that returns immediately. Use for Kubernetes liveness probe.",
)
async def liveness_check() -> LivenessResponse:
    """Simple liveness probe - returns healthy if the service is running."""
    return LivenessResponse(
        status="healthy",
        service=settings.service_name,
    )


@router.get(
    "/health/live",
    response_model=LivenessResponse,
    summary="Liveness probe (alternate)",
    description="Alternate liveness endpoint for Kubernetes.",
)
async def liveness_check_alt() -> LivenessResponse:
    """Alternate liveness probe endpoint."""
    return await liveness_check()


@router.get(
    "/health/ready",
    response_model=HealthResponse,
    summary="Readiness probe",
    description="Full readiness check with dependency status. Use for Kubernetes readiness probe.",
)
async def readiness_check(response: Response) -> HealthResponse:
    """Readiness probe that checks all dependencies."""
    checks: dict[str, ComponentHealth] = {}

    # Check database
    db_health = await _check_database()
    checks["database"] = db_health
    metrics.set_health_status("database", db_health.status)

    # Check Weaviate
    weaviate_health = await _check_weaviate()
    checks["weaviate"] = weaviate_health
    metrics.set_health_status("weaviate", weaviate_health.status)

    # Determine overall status
    # unhealthy if database is unhealthy (critical dependency)
    # degraded if weaviate is unhealthy but database is ok
    # healthy if all are healthy
    if checks["database"].status == HEALTH_STATUS_UNHEALTHY:
        overall_status = HEALTH_STATUS_UNHEALTHY
    elif any(c.status == HEALTH_STATUS_UNHEALTHY for c in checks.values()):
        overall_status = HEALTH_STATUS_DEGRADED
    elif any(c.status == HEALTH_STATUS_DEGRADED for c in checks.values()):
        overall_status = HEALTH_STATUS_DEGRADED
    else:
        overall_status = HEALTH_STATUS_HEALTHY

    # A readiness probe keys off the HTTP status, not the body — return 503 when
    # unhealthy so the pod is pulled from rotation instead of receiving traffic
    # it can't serve. `degraded` stays 200 (still serving) (#14).
    if overall_status == HEALTH_STATUS_UNHEALTHY:
        response.status_code = 503

    return HealthResponse(
        status=overall_status,
        timestamp=datetime.now(timezone.utc).isoformat(),
        version=settings.version,
        service=settings.service_name,
        checks=checks,
    )


async def _check_database() -> ComponentHealth:
    """Check database connectivity and latency."""
    start_time = time.time()

    try:
        db = await asyncio.wait_for(
            get_database(),
            timeout=DATABASE_HEALTH_CHECK_TIMEOUT,
        )

        # Run a simple query to verify connection
        async with db.session() as session:
            await asyncio.wait_for(
                session.execute(text("SELECT 1")),
                timeout=DATABASE_HEALTH_CHECK_TIMEOUT,
            )

        latency_ms = (time.time() - start_time) * 1000

        # Check if latency is high
        if latency_ms > 100:  # More than 100ms is concerning
            return ComponentHealth(
                status=HEALTH_STATUS_DEGRADED,
                latency_ms=round(latency_ms, 2),
                message="High latency detected",
            )

        return ComponentHealth(
            status=HEALTH_STATUS_HEALTHY,
            latency_ms=round(latency_ms, 2),
        )

    except asyncio.TimeoutError:
        return ComponentHealth(
            status=HEALTH_STATUS_UNHEALTHY,
            latency_ms=round((time.time() - start_time) * 1000, 2),
            message="Connection timeout",
        )
    except Exception as e:
        logger.warning("Database health check failed", error=str(e))
        return ComponentHealth(
            status=HEALTH_STATUS_UNHEALTHY,
            latency_ms=round((time.time() - start_time) * 1000, 2),
            message=f"Connection failed: {type(e).__name__}",
        )


async def _check_weaviate() -> ComponentHealth:
    """Check Weaviate connectivity and latency."""
    start_time = time.time()

    try:
        search_service = await asyncio.wait_for(
            get_search_service(),
            timeout=WEAVIATE_HEALTH_CHECK_TIMEOUT,
        )

        # Check if connected
        is_connected = await asyncio.wait_for(
            search_service.is_connected(),
            timeout=WEAVIATE_HEALTH_CHECK_TIMEOUT,
        )

        latency_ms = (time.time() - start_time) * 1000

        if not is_connected:
            return ComponentHealth(
                status=HEALTH_STATUS_UNHEALTHY,
                latency_ms=round(latency_ms, 2),
                message="Not connected",
            )

        # Check if latency is high
        if latency_ms > 500:  # More than 500ms is concerning for Weaviate
            return ComponentHealth(
                status=HEALTH_STATUS_DEGRADED,
                latency_ms=round(latency_ms, 2),
                message="High latency detected",
            )

        return ComponentHealth(
            status=HEALTH_STATUS_HEALTHY,
            latency_ms=round(latency_ms, 2),
        )

    except asyncio.TimeoutError:
        return ComponentHealth(
            status=HEALTH_STATUS_UNHEALTHY,
            latency_ms=round((time.time() - start_time) * 1000, 2),
            message="Connection timeout",
        )
    except Exception as e:
        logger.warning("Weaviate health check failed", error=str(e))
        return ComponentHealth(
            status=HEALTH_STATUS_DEGRADED,
            latency_ms=round((time.time() - start_time) * 1000, 2),
            message=f"Check failed: {type(e).__name__}",
        )
