"""Authentication service for API key validation."""

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status

from src.models.api_key import APIKeyInfo
from src.services.database import DatabaseService, get_database
from src.utils import get_logger

logger = get_logger(__name__)


class AuthService:
    """Service for API key authentication."""

    def __init__(self, database: DatabaseService):
        self.database = database

    async def validate_api_key(self, api_key: str) -> APIKeyInfo | None:
        """Validate an API key and return key info if valid."""
        if not api_key:
            return None

        # Clean up the key (remove "Bearer " prefix if present)
        if api_key.startswith("Bearer "):
            api_key = api_key[7:]

        return await self.database.validate_api_key(api_key)

    async def require_api_key(
        self,
        api_key: str,
        required_permission: str | None = None,
    ) -> APIKeyInfo:
        """Validate API key and raise if invalid."""
        key_info = await self.validate_api_key(api_key)

        if not key_info:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired API key",
                headers={"WWW-Authenticate": "ApiKey"},
            )

        if key_info.is_expired():
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="API key has expired",
                headers={"WWW-Authenticate": "ApiKey"},
            )

        if required_permission and not key_info.has_permission(required_permission):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"API key does not have '{required_permission}' permission",
            )

        return key_info


# Singleton
_auth_service: AuthService | None = None


async def get_auth_service() -> AuthService:
    """Get the auth service instance."""
    global _auth_service
    if _auth_service is None:
        database = await get_database()
        _auth_service = AuthService(database)
    return _auth_service


@dataclass
class ResolvedAuth:
    """API key info with a resolved workspace_id."""

    key_info: APIKeyInfo
    workspace_id: str | None


# FastAPI dependencies
async def get_api_key_info(
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
    authorization: Annotated[str | None, Header()] = None,
) -> APIKeyInfo:
    """FastAPI dependency to validate API key from headers."""
    # Try X-API-Key header first, then Authorization header
    api_key = x_api_key
    if not api_key and authorization:
        if authorization.startswith("Bearer "):
            api_key = authorization[7:]
        else:
            api_key = authorization

    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API key required. Provide X-API-Key header or Authorization: Bearer <key>",
            headers={"WWW-Authenticate": "ApiKey"},
        )

    auth_service = await get_auth_service()
    return await auth_service.require_api_key(api_key)


async def get_search_permission(
    key_info: Annotated[APIKeyInfo, Depends(get_api_key_info)],
) -> APIKeyInfo:
    """Require search permission."""
    if not key_info.has_permission("search"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="API key does not have 'search' permission",
        )
    return key_info


async def get_read_permission(
    key_info: Annotated[APIKeyInfo, Depends(get_api_key_info)],
) -> APIKeyInfo:
    """Require read permission."""
    if not key_info.has_permission("read"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="API key does not have 'read' permission",
        )
    return key_info


async def get_write_permission(
    key_info: Annotated[APIKeyInfo, Depends(get_api_key_info)],
) -> APIKeyInfo:
    """Require write permission."""
    if not key_info.has_permission("write"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="API key does not have 'write' permission",
        )
    return key_info


async def _resolve_workspace(
    key_info: APIKeyInfo,
    header_workspace_id: str | None,
    *,
    required: bool = False,
) -> ResolvedAuth:
    """Resolve workspace_id from header or API key, with access validation.

    A *workspace-scoped* key (``key_info.workspace_id`` set) is bound to exactly
    that workspace and may never act on a different one — even one its owning
    user also owns. Honouring an ``X-Workspace-Id`` header that differs from the
    key's binding would collapse the key's scope to "any workspace the user
    owns", defeating the point of issuing a scoped key. Only *user-scoped* keys
    (``workspace_id is None``) may select among the user's workspaces via header.
    """
    # Workspace-scoped key: the binding wins. Reject a header that disagrees.
    if key_info.workspace_id is not None:
        if header_workspace_id is not None and header_workspace_id != key_info.workspace_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    f"API key is scoped to workspace '{key_info.workspace_id}' "
                    f"and cannot access workspace '{header_workspace_id}'"
                ),
            )
        return ResolvedAuth(key_info=key_info, workspace_id=key_info.workspace_id)

    # User-scoped key: a header may select any workspace the user actually owns.
    workspace_id = header_workspace_id
    if workspace_id:
        database = await get_database()
        user_workspaces = await database.get_user_workspace_ids(key_info.user_id)
        if workspace_id in user_workspaces:
            return ResolvedAuth(key_info=key_info, workspace_id=workspace_id)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"You don't have access to workspace '{workspace_id}'",
        )

    # No workspace from header or key — try to resolve from user's workspaces
    database = await get_database()
    user_workspaces = await database.get_user_workspace_ids(key_info.user_id)

    if required:
        if not user_workspaces:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No workspaces found. Provide X-Workspace-Id header.",
            )
        if len(user_workspaces) > 1:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "Multiple workspaces found. Provide X-Workspace-Id header "
                    "to specify which workspace to use."
                ),
            )
        return ResolvedAuth(key_info=key_info, workspace_id=user_workspaces[0])

    # For read/search — use first workspace if exactly one, else None
    if len(user_workspaces) == 1:
        return ResolvedAuth(key_info=key_info, workspace_id=user_workspaces[0])

    return ResolvedAuth(key_info=key_info, workspace_id=None)


async def resolve_workspace_write(
    key_info: Annotated[APIKeyInfo, Depends(get_write_permission)],
    x_workspace_id: Annotated[str | None, Header(alias="X-Workspace-Id")] = None,
) -> ResolvedAuth:
    """Resolve workspace for write operations (workspace required)."""
    return await _resolve_workspace(key_info, x_workspace_id, required=True)


async def resolve_workspace_read(
    key_info: Annotated[APIKeyInfo, Depends(get_read_permission)],
    x_workspace_id: Annotated[str | None, Header(alias="X-Workspace-Id")] = None,
) -> ResolvedAuth:
    """Resolve workspace for read operations (workspace optional for single-workspace users)."""
    return await _resolve_workspace(key_info, x_workspace_id, required=False)


async def resolve_workspace_search(
    key_info: Annotated[APIKeyInfo, Depends(get_search_permission)],
    x_workspace_id: Annotated[str | None, Header(alias="X-Workspace-Id")] = None,
) -> ResolvedAuth:
    """Resolve workspace for search operations."""
    return await _resolve_workspace(key_info, x_workspace_id, required=False)
