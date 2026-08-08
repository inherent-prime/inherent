"""Readiness probe must reflect dependency health in the HTTP status (#14).

A Kubernetes readiness probe keys off the HTTP status, not the body. Returning
200 while the body says "unhealthy" keeps a broken pod in rotation.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import Response

from src.api.v1 import health as health_mod
from src.config import settings
from src.models.health import ComponentHealth

pytestmark = pytest.mark.asyncio


def _patch_checks(db_status: str, weaviate_status: str):
    return (
        patch.object(
            health_mod, "_check_database", AsyncMock(return_value=ComponentHealth(status=db_status))
        ),
        patch.object(
            health_mod,
            "_check_weaviate",
            AsyncMock(return_value=ComponentHealth(status=weaviate_status)),
        ),
    )


async def test_readiness_503_when_database_unhealthy():
    db_p, wv_p = _patch_checks("unhealthy", "healthy")
    with db_p, wv_p:
        response = Response()
        body = await health_mod.readiness_check(response)
    assert response.status_code == 503
    assert body.status == "unhealthy"


async def test_readiness_200_when_all_healthy():
    db_p, wv_p = _patch_checks("healthy", "healthy")
    with db_p, wv_p:
        response = Response()
        body = await health_mod.readiness_check(response)
    assert response.status_code == 200
    assert body.status == "healthy"


async def test_readiness_200_when_degraded():
    # Weaviate down but DB ok -> degraded: still serving, so stay in rotation (200).
    db_p, wv_p = _patch_checks("healthy", "unhealthy")
    with db_p, wv_p:
        response = Response()
        body = await health_mod.readiness_check(response)
    assert response.status_code == 200
    assert body.status == "degraded"


async def test_weaviate_check_passes_configured_timeout_to_is_connected():
    """#203: the configured Weaviate timeout must reach the actual network call.

    Wrapping ``search_service.is_connected()`` in
    ``asyncio.wait_for(..., timeout=settings.weaviate_health_check_timeout_seconds)``
    is not sufficient on its own -- ``is_connected`` previously hardcoded its
    OWN 5.0s timeout on the inner httpx request, so the outer wait_for's
    (potentially larger, operator-configured) timeout never had a chance to
    matter: the inner call would already have failed first. This asserts the
    value in Settings is the exact value ``is_connected`` is called with, so
    a future edit that stops threading it through fails loudly here instead
    of silently reintroducing the #203 defect at this deeper layer.
    """
    mock_search_service = AsyncMock()
    mock_search_service.is_connected = AsyncMock(return_value=True)

    with patch.object(
        health_mod, "get_search_service", AsyncMock(return_value=mock_search_service)
    ):
        await health_mod._check_weaviate()

    mock_search_service.is_connected.assert_awaited_once_with(
        timeout=settings.weaviate_health_check_timeout_seconds
    )
