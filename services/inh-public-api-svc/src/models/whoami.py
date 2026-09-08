"""Authenticated caller identity returned by REST and MCP."""

from pydantic import BaseModel


class WhoAmIResponse(BaseModel):
    """Safe identity metadata; secret key material is intentionally absent."""

    key_id: str
    key_name: str | None
    user_id: str
    workspace_id: str | None
    workspace_ids: list[str]
    permissions: list[str]
    engine_version: str
    endpoint: str
