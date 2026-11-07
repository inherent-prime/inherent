"""Client for the host app's workspace-provisioning endpoint.

The engine cannot create workspaces itself: ownership and plan limits live in
the host app (intg-svc). This module is the one place the engine calls it:

    POST {INTG_SVC_URL}/internal/workspaces
    X-Service-Secret: $ENGINE_SERVICE_SECRET
    {clerk_user_id, name, description?}

Host responses and how they surface to the MCP caller:

    201            -> the created workspace (workspace_id, name, description, role)
    400            -> "invalid workspace details: <host message>"
    403            -> host message passes through (plan limit); the bare code
                      ``entitlement_required`` becomes a plain-language message
    404            -> the user has no Inherent account
    401 / 5xx / network / bad body -> generic "try again later"

The service secret is read from a ``SecretStr`` only at the moment the header
is built; it is never logged, and no raw upstream body is ever echoed on the
generic paths (a 5xx body may hold stack traces).
"""

from __future__ import annotations

import httpx

from src.config.settings import settings
from src.utils import get_logger

logger = get_logger(__name__)

# Longest host message we echo to a caller; keeps a misbehaving host from
# flooding an agent's context.
_MAX_HOST_MESSAGE_CHARS = 300

_TRY_AGAIN = "Error: could not create the workspace right now; try again later"
NOT_CONFIGURED = "Error: workspace creation is not available on this deployment"


class WorkspaceProvisioningError(Exception):
    """A failure whose ``message`` is safe to show to an MCP caller verbatim."""

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


def _build_client() -> httpx.AsyncClient:
    """One short-lived client per call (rare, user-initiated; no pool needed).

    Separate function so tests can swap in an ``httpx.MockTransport``.
    """
    return httpx.AsyncClient(timeout=settings.intg_svc_timeout_seconds)


def _host_message(response: httpx.Response) -> str | None:
    """The host's ``error`` string, trimmed, or None if absent/not a string."""
    try:
        error = response.json().get("error")
    except (ValueError, AttributeError):
        return None
    if isinstance(error, str) and error.strip():
        return error.strip()[:_MAX_HOST_MESSAGE_CHARS]
    return None


async def create_workspace_via_host(
    clerk_user_id: str, name: str, description: str | None = None
) -> dict:
    """Create a workspace owned by ``clerk_user_id``; return the host's payload.

    Raises ``WorkspaceProvisioningError`` (safe message) on any failure.
    """
    base_url = settings.intg_svc_url
    secret = settings.engine_service_secret
    if not base_url or secret is None or not secret.get_secret_value():
        raise WorkspaceProvisioningError(NOT_CONFIGURED)

    body: dict = {"clerk_user_id": clerk_user_id, "name": name}
    if description:
        body["description"] = description

    try:
        async with _build_client() as client:
            response = await client.post(
                f"{base_url.rstrip('/')}/internal/workspaces",
                json=body,
                headers={"X-Service-Secret": secret.get_secret_value()},
            )
    except httpx.HTTPError as exc:
        # Type only: httpx messages can embed URLs/hosts we do not need in logs.
        logger.error("create_workspace_host_unreachable", error_type=type(exc).__name__)
        raise WorkspaceProvisioningError(_TRY_AGAIN) from None

    status = response.status_code
    if status == 201:
        try:
            created = response.json()
            return {
                "workspace_id": str(created["workspace_id"]),
                "name": created.get("name", name),
                "description": created.get("description"),
                "role": created.get("role", "owner"),
            }
        except (ValueError, KeyError, AttributeError, TypeError):
            logger.error("create_workspace_bad_success_body")
            raise WorkspaceProvisioningError(_TRY_AGAIN) from None

    message = _host_message(response)
    if status == 400:
        raise WorkspaceProvisioningError(
            f"Error: invalid workspace details: {message or 'check the name and description'}"
        )
    if status == 403:
        if message in (None, "entitlement_required"):
            raise WorkspaceProvisioningError(
                "Error: your current plan does not allow creating workspaces; "
                "upgrade your plan to create one"
            )
        raise WorkspaceProvisioningError(f"Error: {message}")
    if status == 404:
        raise WorkspaceProvisioningError(
            "Error: no Inherent account was found for this sign-in; "
            "sign in to the Inherent app once, then try again"
        )

    # 401 (our secret is wrong -- an operator problem, not the caller's), 5xx,
    # and anything unexpected: log status only, tell the caller to retry.
    logger.error("create_workspace_host_error", status=status)
    raise WorkspaceProvisioningError(_TRY_AGAIN)
