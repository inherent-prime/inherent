"""API Key models for authentication."""

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field


class APIKeyInfo(BaseModel):
    """Information about a validated API key."""

    key_id: str
    name: str | None = None
    user_id: str
    workspace_id: str | None = None  # Null for user-scoped keys (works across all workspaces)
    permissions: list[Literal["read", "search", "write"]] = Field(default=["read", "search"])
    rate_limit: int = 100
    expires_at: datetime | None = None
    status: Literal["active", "revoked"] = "active"

    def has_permission(self, permission: str) -> bool:
        """Check if the key has a specific permission."""
        return permission in self.permissions

    def is_expired(self) -> bool:
        """Check if the key has expired."""
        if self.expires_at is None:
            return False
        return datetime.now(timezone.utc) > self.expires_at
