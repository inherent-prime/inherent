"""Identity endpoint shared with the MCP ``whoami`` tool (#278)."""

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from src.config import settings
from src.models.api_key import APIKeyInfo
from src.models.whoami import WhoAmIResponse
from src.services.auth import get_api_key_info, get_authorized_workspace_ids
from src.services.database import DatabaseService, get_database

router = APIRouter()


async def build_whoami(
    key_info: APIKeyInfo, database: DatabaseService, endpoint: str
) -> WhoAmIResponse:
    """Build the single identity shape used by REST and MCP."""
    return WhoAmIResponse(
        key_id=key_info.key_id,
        key_name=key_info.name,
        user_id=key_info.user_id,
        workspace_id=key_info.workspace_id,
        workspace_ids=await get_authorized_workspace_ids(key_info, database),
        permissions=list(key_info.permissions),
        engine_version=settings.version,
        endpoint=endpoint.rstrip("/"),
    )


@router.get("/whoami", response_model=WhoAmIResponse)
async def whoami(
    request: Request,
    key_info: Annotated[APIKeyInfo, Depends(get_api_key_info)],
    database: Annotated[DatabaseService, Depends(get_database)],
) -> WhoAmIResponse:
    """Return the authenticated key's identity and authoritative workspace scope."""
    return await build_whoami(key_info, database, str(request.base_url))
