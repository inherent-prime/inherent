"""RFC 9728 OAuth 2.0 Protected Resource Metadata (#295).

Serves ``GET /.well-known/oauth-protected-resource`` so a spec-compliant MCP
client (e.g. Claude Code, per the issue's own citation) can discover the
authorization server protecting ``/mcp`` instead of guessing:

    > MCP servers MUST implement OAuth 2.0 Protected Resource Metadata
    > (RFC9728). MCP clients MUST use OAuth 2.0 Protected Resource Metadata
    > for authorization server discovery.

This router is included in ``src/main.py`` ONLY when ``settings.oauth_enabled``
is true at app-creation time -- not registered-but-guarded inside the
handler. A self-hosted deployment that never enabled OAuth must not
advertise an authorization server it does not run (issue #295 comment,
2026-08-18); the acceptance criterion is that this path 404s exactly as it
would if this module did not exist at all, which "route never registered"
gives for free, with no risk of the handler's own 404 response shape
(RFC 7807 problem+json vs. Starlette's bare default) drifting from
whatever "no route matched" already produces. See
``tests/unit/test_oauth_config_gate.py``.
"""

from fastapi import APIRouter

from src.config import settings

router = APIRouter(tags=["OAuth"])


@router.get(
    "/.well-known/oauth-protected-resource",
    summary="RFC 9728 protected-resource metadata",
    description=(
        "Advertises the authorization server and minimal scope catalogue "
        "for /mcp. Only registered when OAUTH_ENABLED=true."
    ),
    include_in_schema=False,
)
async def oauth_protected_resource_metadata() -> dict:
    """RFC 9728 protected-resource metadata document for ``/mcp``.

    ``scopes_supported`` is deliberately the MINIMAL set
    (``settings.oauth_scopes_supported``, default ``["kb:read",
    "kb:search"]``) per the spec's scope-minimisation guidance -- write
    access arrives via a 403 ``insufficient_scope`` step-up on the specific
    tool that needs it, not by advertising the full permission catalogue
    upfront (some IdPs reject an auth request naming every scope with
    ``invalid_scope``; see ``PERMISSION_SCOPE_MAP`` in
    ``src/services/auth.py``).

    This module is only included in the app when ``oauth_enabled`` is true
    (see this file's module docstring), so there is no "disabled" branch
    inside this handler to keep in sync with that gate -- if this code is
    running, OAuth is on and every one of these settings is meant to be
    populated. A misconfigured deployment (enabled without
    ``oauth_authorization_server`` / ``oauth_resource_identifier`` set)
    still gets a document -- with ``null``/empty fields -- rather than a
    500, since a broken-but-visible document is easier for an operator to
    debug than an opaque server error on a `.well-known` path clients probe
    unauthenticated.
    """
    return {
        "resource": settings.oauth_resource_identifier,
        "authorization_servers": (
            [settings.oauth_authorization_server] if settings.oauth_authorization_server else []
        ),
        "scopes_supported": settings.oauth_scopes_supported,
        "bearer_methods_supported": ["header"],
    }
