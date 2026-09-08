"""Authentication service for API key validation."""

import asyncio
from dataclasses import dataclass
from typing import Annotated, Literal

import jwt
from fastapi import Depends, Header, HTTPException, status
from jwt import PyJWKClient

from src.config import settings
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


async def get_authorized_workspace_ids(
    key_info: APIKeyInfo, database: DatabaseService
) -> list[str]:
    """Return every workspace_id ``key_info`` is authorised to act on.

    This is the SINGLE source of truth for the key-scoping rule (#138),
    shared by REST (``_resolve_workspace`` below) and MCP
    (``src/mcp_server/server.py``) so the two surfaces cannot drift.

    - A *workspace-scoped* key (``key_info.workspace_id`` set) is validated
      against ``database.user_owns_workspace_in_mongo`` — a MONGO-ONLY
      membership check (#138 blocker-2 fix) — NOT
      ``database.get_user_workspace_ids``. The first #138 cut trusted
      ``key_info.workspace_id`` unconditionally; the immediate follow-up
      "fixed" that by intersecting it with ``get_user_workspace_ids``, but
      that method UNIONS Mongo with a Postgres ``processed_documents``
      fallback (any workspace the user has EVER ingested into), so a
      workspace transferred away from the key's owner in Mongo kept being
      served whenever the owner had ever uploaded to it — the realistic
      case, since a workspace worth protecting has content. This costs one
      extra Mongo round-trip per scoped-key request, on top of REST/MCP's
      existing DB call; see ``user_owns_workspace_in_mongo`` for why it must
      NOT fall back to the union, and why a Mongo failure here RAISES rather
      than silently granting or denying (revocation must not silently stop
      being enforced during an outage — this call is NOT wrapped in
      try/except, so callers see the exception).
    - A *user-scoped* key (``workspace_id is None``) may act on every
      workspace its owning user currently owns, via
      ``database.get_user_workspace_ids`` (Mongo UNION Postgres fallback) —
      unchanged from before this fix. This is a listing convenience, not a
      binding validation: these keys have no narrower claim than the user's
      full set to begin with, so the union's "which workspaces might this
      user plausibly reach" answer is the right question here, unlike for a
      scoped key's binding above.
    """
    if key_info.workspace_id:
        # Truthy, not `is not None`: an empty-string workspace_id (no
        # issuance path produces one today) is treated as unscoped rather
        # than as a binding to "", matching _resolve_workspace's truthiness
        # checks elsewhere in this module (#138 follow-up).
        owns = await database.user_owns_workspace_in_mongo(key_info.user_id, key_info.workspace_id)
        return [key_info.workspace_id] if owns else []
    return await database.get_user_workspace_ids(key_info.user_id)


def describe_workspace_denial(key_info: APIKeyInfo, requested_workspace_id: str) -> str:
    """Return the rejection message for a workspace ``key_info`` is not
    authorised for — the single wording shared by REST (``_resolve_workspace``)
    and every MCP call site that rejects an out-of-scope workspace, so the two
    surfaces (and MCP's own call sites, which previously had two different
    strings of their own) never describe the same rejection three different
    ways (#138 follow-up).

    A workspace-scoped key gets an ACTIONABLE message naming its OWN bound
    workspace: that is the caller's own grant, not the owner's other
    workspaces, so revealing it leaks nothing and lets the caller immediately
    retry with the right id instead of treating the rejection as "workspace
    doesn't exist" and guessing or giving up. A user-scoped key gets the
    generic "you don't have access" message, since there is no one workspace
    to point back to — the caller must consult its own owned set.
    """
    if key_info.workspace_id:
        # Truthy, matching get_authorized_workspace_ids's and
        # _resolve_workspace's checks (#138 follow-up): a "" workspace_id
        # (no issuance path produces one) is treated as unscoped everywhere
        # in this module, consistently — not "scoped to ''" in the message
        # while every authorization site treats it as unscoped.
        return (
            f"API key is scoped to workspace '{key_info.workspace_id}' "
            f"and cannot access workspace '{requested_workspace_id}'"
        )
    return f"You don't have access to workspace '{requested_workspace_id}'"


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

    Every branch below derives its authorised set from
    ``get_authorized_workspace_ids`` — INCLUDING the scoped-key branch, which
    used to trust ``key_info.workspace_id`` directly without confirming the
    binding is still owned. That was a second, independent implementation of
    the same scoping rule living inline here instead of in the shared
    function, and the two copies had already started to drift (#138
    blocker-2/item-6 follow-up): this collapses them into one.
    """
    database = await get_database()
    authorized = await get_authorized_workspace_ids(key_info, database)

    # Workspace-scoped key: the binding wins, but only while it is still
    # owned — get_authorized_workspace_ids intersects the binding with
    # current ownership, so a stale binding (workspace deleted/transferred
    # away from the key's owner) fails closed here too, not just on MCP.
    if key_info.workspace_id:
        if header_workspace_id is not None and header_workspace_id != key_info.workspace_id:
            logger.warning(
                "workspace_access_denied",
                reason="scoped_key_header_mismatch",
                user_id=key_info.user_id,
                key_id=key_info.key_id,
                key_workspace_id=key_info.workspace_id,
                requested_workspace_id=header_workspace_id,
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=describe_workspace_denial(key_info, header_workspace_id),
            )
        if key_info.workspace_id not in authorized:
            # The header agrees with the binding (or is absent), but the
            # binding itself is stale: get_authorized_workspace_ids found the
            # key's owner no longer owns this workspace. Fail closed instead
            # of serving a workspace the owner has lost — never fall back to
            # whatever else the owner currently owns.
            logger.warning(
                "workspace_access_denied",
                reason="scoped_key_binding_not_owned",
                user_id=key_info.user_id,
                key_id=key_info.key_id,
                key_workspace_id=key_info.workspace_id,
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    f"API key is scoped to workspace '{key_info.workspace_id}', "
                    "which is no longer accessible"
                ),
            )
        return ResolvedAuth(key_info=key_info, workspace_id=key_info.workspace_id)

    # User-scoped key: a header may select any workspace the user actually owns.
    workspace_id = header_workspace_id
    if workspace_id:
        if workspace_id in authorized:
            return ResolvedAuth(key_info=key_info, workspace_id=workspace_id)
        # Log the attempted vs authorised set so support can tell "user pasted
        # the wrong id (e.g. Clerk org_id / workspace name)" apart from a real
        # ownership gap without shelling into the DB.
        logger.warning(
            "workspace_access_denied",
            reason="requested_workspace_not_in_user_set",
            user_id=key_info.user_id,
            key_id=key_info.key_id,
            requested_workspace_id=workspace_id,
            authorised_workspace_ids=authorized,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=describe_workspace_denial(key_info, workspace_id),
        )

    # No workspace from header or key — try to resolve from the authorised set.
    if required:
        if not authorized:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No workspaces found. Provide X-Workspace-Id header.",
            )
        if len(authorized) > 1:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "Multiple workspaces found. Provide X-Workspace-Id header "
                    "to specify which workspace to use."
                ),
            )
        return ResolvedAuth(key_info=key_info, workspace_id=authorized[0])

    # For read/search — use first workspace if exactly one, else None
    if len(authorized) == 1:
        return ResolvedAuth(key_info=key_info, workspace_id=authorized[0])

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


# =========================================================================== #
# OAuth 2.1 resource-server support (#295)
# =========================================================================== #
#
# `/mcp` currently answers an unauthenticated request with `WWW-Authenticate:
# ApiKey` only. RFC 9728 + the MCP authorization spec require a resource
# server to instead (or additionally) point a compliant client at an
# authorization server via a `WWW-Authenticate: Bearer` challenge naming a
# protected-resource metadata document. Everything below is inert unless
# `settings.oauth_enabled` is true (default False, see settings.py) --
# X-API-Key / `Bearer ink_...` auth is COMPLETELY UNCHANGED above this
# marker; nothing below alters `get_api_key_info`, `AuthService`, or any
# function REST routes depend on.
#
# Maps an APIKeyInfo/ToolDef permission name to the OAuth scope that grants
# it. Only "read"/"search" appear in `settings.oauth_scopes_supported`'s
# advertised catalogue (scope minimisation, issue #295) -- "write" is
# deliberately NOT advertised upfront but is still checked here, so a token
# that stepped up to it via a flow outside this repo (consent UI, IdP scope
# grant) is still honoured rather than permanently locked out.
PERMISSION_SCOPE_MAP: dict[str, str] = {
    "read": "kb:read",
    "search": "kb:search",
    "write": "kb:write",
}


class TokenValidationError(Exception):
    """A bearer token failed verification (#295).

    ``reason`` is a short machine-stable string -- NEVER the token, and
    never a raw PyJWT exception's ``str()`` (those can echo header/claim
    fragments back) -- suitable only for server-side logs. The client-facing
    side of every failure is the SAME generic
    ``WWW-Authenticate: Bearer error="invalid_token"`` challenge regardless
    of ``reason`` (see ``build_bearer_challenge``); this module does not
    hand the client a way to distinguish "expired" from "wrong audience"
    from "signature invalid" beyond what that one challenge already says.
    """

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class OAuthClaims:
    """The subset of a verified bearer token's claims this resource server
    needs. Deliberately narrow -- everything else in the token (arbitrary
    IdP-specific claims) is dropped here rather than carried forward, so
    nothing downstream can come to depend on a claim shape only one IdP
    happens to emit."""

    subject: str
    scopes: frozenset[str]


@dataclass(frozen=True)
class Principal:
    """Caller identity behind an authenticated request (#295).

    Both API-key and OAuth callers are meant to resolve into this ONE shape
    -- the refactor the issue's "Suggested refactor" section asks for --
    so a future caller never needs to learn which door the caller came
    through. **Seam for #309** (per-identity entitlements/quotas): that work
    hangs its lookup off ``principal_id`` + ``principal_type`` (e.g.
    ``get_entitlements(principal.principal_type, principal.principal_id)``),
    both of which are stable, opaque keys already. Nothing about
    entitlements or quotas is implemented here -- this dataclass is only the
    seam.

    #295 itself wires only the OAuth side of this into the live request path
    (``http_transport.py``'s ``_call_tool_oauth``); the existing API-key path
    keeps operating directly on ``APIKeyInfo`` there, UNCHANGED, so every
    already-tested API-key behavior (including #138's workspace-scoping
    rules) stays byte-for-byte identical. ``from_api_key`` exists so the
    same construction works for both identity sources today, ready for a
    later PR to route the API-key path through ``Principal`` too without
    this dataclass's shape needing to change.
    """

    principal_id: str
    principal_type: Literal["api_key", "oauth"]
    scopes: frozenset[str]

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes

    @classmethod
    def from_api_key(cls, key_info: APIKeyInfo) -> "Principal":
        return cls(
            principal_id=key_info.user_id,
            principal_type="api_key",
            scopes=frozenset(key_info.permissions),
        )

    @classmethod
    def from_oauth_claims(cls, claims: OAuthClaims) -> "Principal":
        return cls(
            principal_id=claims.subject,
            principal_type="oauth",
            scopes=claims.scopes,
        )


# Process-wide cached JWKS client (#295). PyJWKClient does its own in-memory
# caching of fetched keys, keyed by `kid`, for `settings.oauth_jwks_cache_seconds`
# -- rebuilt here only when the configured URL changes (production never
# changes it mid-process; tests do, across cases), so that cache isn't
# thrown away on every call.
_jwks_client: PyJWKClient | None = None
_jwks_client_url: str | None = None


def _get_jwks_client(jwks_url: str) -> PyJWKClient:
    global _jwks_client, _jwks_client_url
    if _jwks_client is None or _jwks_client_url != jwks_url:
        _jwks_client = PyJWKClient(
            jwks_url,
            cache_keys=True,
            lifespan=settings.oauth_jwks_cache_seconds,
        )
        _jwks_client_url = jwks_url
    return _jwks_client


async def verify_oauth_token(token: str) -> OAuthClaims:
    """Verify ``token`` against the configured authorization server (#295).

    Checks signature (via the AS's published JWKS), ``iss``, ``exp``, and --
    non-negotiably, per RFC 8707 Sec 2 and the MCP authorization spec --
    that ``aud`` contains this server's own ``oauth_resource_identifier``. A
    token for any other resource protected by the same authorization server
    is REJECTED here, not merely logged about (design constraint #4; see
    ``tests/security/test_oauth_token_validation.py::TestVerifyOAuthToken::
    test_aud_mismatch_is_rejected_not_merely_warned``).

    Raises ``TokenValidationError`` on ANY failure, including
    "not configured" (``oauth_enabled=true`` without
    ``oauth_authorization_server`` / ``oauth_resource_identifier`` /
    ``effective_oauth_jwks_url`` set) and "JWKS unreachable" -- both fail
    CLOSED (reject) rather than treating a misconfigured or momentarily
    unreachable authorization server as an open resource, per issue #295's
    2026-08-19 comment ("identity resolution... should fail closed").

    NEVER logs ``token`` -- not in this function, not via a caught
    exception's message (PyJWT's own messages can echo header/claim
    fragments), not via ``repr()`` of anything holding it. See
    ``tests/security/test_oauth_token_validation.py::TestTokenNeverLogged``.
    """
    jwks_url = settings.effective_oauth_jwks_url
    if (
        not jwks_url
        or not settings.oauth_authorization_server
        or not settings.oauth_resource_identifier
    ):
        raise TokenValidationError("oauth_not_configured")

    try:
        # PyJWKClient's fetch is a blocking network call (urllib under the
        # hood, not this service's usual httpx client) -- off-loaded to a
        # worker thread so it never blocks the event loop every other /mcp
        # request shares.
        signing_key = await asyncio.to_thread(
            _get_jwks_client(jwks_url).get_signing_key_from_jwt, token
        )
    except Exception:  # noqa: BLE001 - any JWKS/network failure fails closed (see docstring)
        raise TokenValidationError("jwks_unavailable") from None

    try:
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256", "ES256"],
            issuer=settings.oauth_authorization_server,
            audience=settings.oauth_resource_identifier,
            options={"require": ["exp", "iss", "aud", "sub"]},
        )
    except jwt.ExpiredSignatureError:
        # Expired must stay 401, never 403 -- clients key their silent-
        # refresh path on 401 (acceptance criteria). TokenValidationError
        # always maps to 401 at the call site, so this is automatic here;
        # the distinct reason is for server-side logs only.
        raise TokenValidationError("token_expired") from None
    except jwt.InvalidAudienceError:
        # The non-negotiable check (design constraint #4 / RFC 8707 Sec 2).
        raise TokenValidationError("invalid_audience") from None
    except jwt.PyJWTError:
        raise TokenValidationError("invalid_token") from None

    scope_claim = claims.get("scope", "")
    scopes = frozenset(scope_claim.split()) if isinstance(scope_claim, str) else frozenset()
    subject = claims.get("sub")
    if not subject:
        raise TokenValidationError("invalid_token")

    return OAuthClaims(subject=subject, scopes=scopes)


def build_bearer_challenge(resource_metadata_url: str, *, error: str | None = None) -> str:
    """Build a single RFC 6750 ``Bearer`` challenge naming this resource's
    RFC 9728 metadata document (#295).

    ``resource_metadata_url`` is the caller's job to build
    (``http_transport.py``, from the live request's own scheme+host) --
    this module has no request object and must stay deployment-agnostic.
    ``error`` is omitted for "no credential presented at all" (RFC 6750
    Sec 3: don't include an error code when the request carried no auth
    information) and set to ``"invalid_token"`` when a bearer token WAS
    presented but failed verification.
    """
    scope = " ".join(settings.oauth_scopes_supported)
    params = [f'resource_metadata="{resource_metadata_url}"']
    if error:
        params.append(f'error="{error}"')
    params.append(f'scope="{scope}"')
    return "Bearer " + ", ".join(params)


def build_www_authenticate(resource_metadata_url: str, *, error: str | None = None) -> str:
    """Combined `WWW-Authenticate` value for `/mcp`'s 401s (#295, design
    constraint #1): ALWAYS advertises ``ApiKey`` alongside ``Bearer`` --
    never silently replacing one scheme with the other, so an existing
    API-key client and a new OAuth client both learn what they can do from
    the same response.

    Only ever called when ``settings.oauth_enabled`` is true -- see the
    call sites in ``mcp_server/http_transport.py``. When OAuth is disabled,
    `/mcp`'s 401s carry the single, unmodified ``ApiKey`` challenge they
    always have.
    """
    return f"ApiKey, {build_bearer_challenge(resource_metadata_url, error=error)}"
