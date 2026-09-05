"""Safe projections for the flag-gated local admin API."""

from datetime import datetime

from pydantic import BaseModel


class AdminWorkspace(BaseModel):
    workspace_id: str
    name: str | None = None
    user_id: str
    document_count: int


class AdminAPIKey(BaseModel):
    key_id: str
    key_name: str
    key_prefix: str
    workspace_id: str | None
    user_id: str
    permissions: list[str]
    status: str
    created_at: datetime
    last_used_at: datetime | None
    expires_at: datetime | None
