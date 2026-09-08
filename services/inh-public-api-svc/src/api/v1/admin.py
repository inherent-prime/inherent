"""Read-only whole-stack views for local single-operator deployments (#279).

These queries are intentionally unscoped. The feature flag defaults off and
returns 404, keeping this control-plane view unavailable in SaaS.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status

from src.config import settings
from src.config.constants import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE
from src.models.admin import AdminAPIKey, AdminWorkspace
from src.models.api_key import APIKeyInfo
from src.services.auth import get_api_key_info
from src.services.database import DatabaseService, get_database


async def require_admin_api() -> None:
    """Hide the local-only surface when the startup setting is disabled."""
    if not settings.admin_api_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")


router = APIRouter(prefix="/admin", dependencies=[Depends(require_admin_api)])


@router.get("/workspaces", response_model=list[AdminWorkspace])
async def list_workspaces(
    _: Annotated[APIKeyInfo, Depends(get_api_key_info)],
    database: Annotated[DatabaseService, Depends(get_database)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1)] = DEFAULT_PAGE_SIZE,
) -> list[AdminWorkspace]:
    """List a bounded page of all local workspaces without document content."""
    limit = min(page_size, MAX_PAGE_SIZE)
    rows = await database.list_admin_workspaces(offset=(page - 1) * limit, limit=limit)
    return [AdminWorkspace.model_validate(row) for row in rows]


@router.get("/keys", response_model=list[AdminAPIKey])
async def list_keys(
    _: Annotated[APIKeyInfo, Depends(get_api_key_info)],
    database: Annotated[DatabaseService, Depends(get_database)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1)] = DEFAULT_PAGE_SIZE,
) -> list[AdminAPIKey]:
    """List a bounded page of safe API-key metadata without hashes or values."""
    limit = min(page_size, MAX_PAGE_SIZE)
    rows = await database.list_admin_keys(offset=(page - 1) * limit, limit=limit)
    return [AdminAPIKey.model_validate(row) for row in rows]
