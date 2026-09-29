"""OAuth 2.1 bearer-token verification regression tests (#295).

Offline: every token is signed by ``tests._oauth_test_helpers``' own RSA
keypair and the JWKS lookup is monkeypatched to that key directly (see
``patch_jwks_client``) -- no network call, no real authorization server.

Covers the two design constraints the issue calls out as load-bearing
(2026-08-19 comment):

1. `aud` validation must not be bypassable -- a token minted for a
   different resource is REJECTED, not warned about.
2. Nothing here ever logs or echoes the raw token, in any form (log line,
   error body, exception message, `repr()`).

Plus the acceptance criteria this repo owns: expired -> 401 (never 403),
invalid signature -> rejected, insufficient scope -> the spec's
`insufficient_scope` shape, and API-key auth is completely unaffected by
any of this even with OAuth enabled.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

import src.services.auth as auth_mod
from src.config import settings
from src.main import create_app
from src.models.api_key import APIKeyInfo
from src.services.auth import Principal, TokenValidationError, verify_oauth_token
from tests._oauth_test_helpers import ISSUER, RESOURCE, make_token, patch_jwks_client

pytestmark = pytest.mark.security

_HTTP_MCP_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}


@pytest.fixture(autouse=True)
def _oauth_settings(monkeypatch):
    """Every test in this file runs with OAuth enabled and pointed at the
    test keypair's issuer/resource -- the config-gate itself (default off,
    byte-identical) is covered separately in
    tests/unit/test_oauth_config_gate.py."""
    monkeypatch.setattr(settings, "oauth_enabled", True)
    monkeypatch.setattr(settings, "oauth_authorization_server", ISSUER)
    monkeypatch.setattr(settings, "oauth_resource_identifier", RESOURCE)
    patch_jwks_client(monkeypatch, auth_mod)
    yield


# --------------------------------------------------------------------------- #
# verify_oauth_token -- unit level
# --------------------------------------------------------------------------- #


class TestVerifyOAuthToken:
    async def test_valid_token_is_accepted(self):
        token = make_token(scope="kb:read kb:search")
        claims = await verify_oauth_token(token)
        assert claims.subject == "oauth-user-123"
        assert claims.scopes == {"kb:read", "kb:search"}

    async def test_aud_mismatch_is_rejected_not_merely_warned(self):
        """THE non-negotiable check (design constraint #4, RFC 8707 Sec 2):
        a token minted for a different resource protected by the SAME
        authorization server must fail verification outright."""
        token = make_token(aud="https://someone-elses-resource.example/mcp")
        with pytest.raises(TokenValidationError) as exc_info:
            await verify_oauth_token(token)
        assert exc_info.value.reason == "invalid_audience"

    async def test_missing_aud_is_rejected(self):
        token = make_token(aud=None)
        with pytest.raises(TokenValidationError):
            await verify_oauth_token(token)

    async def test_expired_token_is_rejected(self):
        token = make_token(exp_delta_seconds=-3600)
        with pytest.raises(TokenValidationError) as exc_info:
            await verify_oauth_token(token)
        assert exc_info.value.reason == "token_expired"

    async def test_wrong_issuer_is_rejected(self):
        token = make_token(iss="https://not-the-configured-issuer.example")
        with pytest.raises(TokenValidationError):
            await verify_oauth_token(token)

    async def test_missing_subject_is_rejected(self):
        token = make_token(sub=None)
        with pytest.raises(TokenValidationError):
            await verify_oauth_token(token)

    async def test_tampered_signature_is_rejected(self):
        token = make_token()
        tampered = token[:-4] + ("AAAA" if not token.endswith("AAAA") else "BBBB")
        with pytest.raises(TokenValidationError):
            await verify_oauth_token(tampered)

    async def test_oauth_enabled_without_resource_identifier_fails_closed(self, monkeypatch):
        """Misconfiguration (enabled without the required settings) must
        reject, never silently accept -- 'not configured' is not 'not
        required'."""
        monkeypatch.setattr(settings, "oauth_resource_identifier", None)
        with pytest.raises(TokenValidationError) as exc_info:
            await verify_oauth_token(make_token())
        assert exc_info.value.reason == "oauth_not_configured"

    async def test_jwks_fetch_failure_fails_closed(self, monkeypatch):
        """An unreachable authorization server must reject the token, not
        accept it, per issue #295's 2026-08-19 comment ('fail closed... a
        Mongo failure here RAISES rather than silently granting')."""

        class _BrokenJWKSClient:
            def get_signing_key_from_jwt(self, token: str):
                raise ConnectionError("jwks endpoint unreachable")

        monkeypatch.setattr(auth_mod, "_get_jwks_client", lambda url: _BrokenJWKSClient())
        with pytest.raises(TokenValidationError) as exc_info:
            await verify_oauth_token(make_token())
        assert exc_info.value.reason == "jwks_unavailable"


# --------------------------------------------------------------------------- #
# Principal
# --------------------------------------------------------------------------- #


class TestPrincipal:
    async def test_from_api_key_and_from_oauth_claims_share_one_shape(self):
        """The seam #309 hangs entitlement lookups off (#295 design
        constraint #5): both identity sources resolve into the SAME
        dataclass shape, distinguished only by principal_type."""
        key_info = APIKeyInfo(
            key_id="key-1",
            user_id="user-1",
            workspace_id="ws-1",
            permissions=["read", "search"],
            rate_limit=100,
        )
        from_key = Principal.from_api_key(key_info)
        assert from_key.principal_type == "api_key"
        assert from_key.principal_id == "user-1"
        assert from_key.has_scope("read")

        claims = __import__("src.services.auth", fromlist=["OAuthClaims"]).OAuthClaims(
            subject="oauth-user-123", scopes=frozenset({"kb:read"})
        )
        from_oauth = await Principal.from_oauth_claims(claims)
        assert from_oauth.principal_type == "oauth"
        assert from_oauth.principal_id == "oauth-user-123"
        assert from_oauth.has_scope("kb:read")
        assert type(from_key) is type(from_oauth)


# --------------------------------------------------------------------------- #
# Full stack: real /mcp, real ASGI auth gate, real call_tool dispatch
# --------------------------------------------------------------------------- #


def _app_client():
    app = create_app()
    with patch("src.main.get_database", new_callable=AsyncMock):
        with TestClient(app) as client:
            yield client


@pytest.fixture
def client():
    yield from _app_client()


class TestOAuthConnectionGate:
    def test_valid_token_is_admitted_past_the_401_gate(self, client: TestClient):
        """A verifiable, correctly-audienced token gets past connection-level
        auth -- tools/list succeeds (no identity resolution needed for
        listing)."""
        token = make_token()
        r = client.post(
            "/mcp",
            headers={**_HTTP_MCP_HEADERS, "Authorization": f"Bearer {token}"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )
        assert r.status_code == 200

    def test_aud_mismatch_rejected_with_401_over_http(self, client: TestClient):
        token = make_token(aud="https://someone-elses-resource.example/mcp")
        r = client.post(
            "/mcp",
            headers={**_HTTP_MCP_HEADERS, "Authorization": f"Bearer {token}"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )
        assert r.status_code == 401
        assert 'error="invalid_token"' in r.headers["www-authenticate"]

    def test_expired_token_rejected_with_401_never_403(self, client: TestClient):
        """Clients key their silent-refresh path on 401 -- an expired token
        must never surface as 403."""
        token = make_token(exp_delta_seconds=-60)
        r = client.post(
            "/mcp",
            headers={**_HTTP_MCP_HEADERS, "Authorization": f"Bearer {token}"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )
        assert r.status_code == 401

    def test_api_key_still_works_with_oauth_enabled(self, monkeypatch):
        """Design constraint #1: X-API-Key keeps working, unchanged, even
        with oauth_enabled=true."""
        app = create_app()
        key = APIKeyInfo(
            key_id="key-1",
            user_id="user-1",
            workspace_id=None,
            permissions=["read", "search"],
            rate_limit=100,
        )
        db = AsyncMock()
        db.validate_api_key = AsyncMock(return_value=key)
        auth_mod._auth_service = None
        with (
            patch("src.main.get_database", new_callable=AsyncMock),
            patch("src.services.auth.get_database", new=AsyncMock(return_value=db)),
        ):
            with TestClient(app) as test_client:
                r = test_client.post(
                    "/mcp",
                    headers={**_HTTP_MCP_HEADERS, "X-API-Key": "ink_still_works"},
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                )
        auth_mod._auth_service = None
        assert r.status_code == 200

    def test_ink_prefixed_bearer_token_still_uses_api_key_path(self, monkeypatch):
        """`Authorization: Bearer ink_...` is credential-SHAPE api-key, even
        with OAuth enabled (design constraint #5) -- it must never be handed
        to the OAuth verifier."""
        app = create_app()
        key = APIKeyInfo(
            key_id="key-1",
            user_id="user-1",
            workspace_id=None,
            permissions=["read", "search"],
            rate_limit=100,
        )
        db = AsyncMock()
        db.validate_api_key = AsyncMock(return_value=key)
        auth_mod._auth_service = None
        with (
            patch("src.main.get_database", new_callable=AsyncMock),
            patch("src.services.auth.get_database", new=AsyncMock(return_value=db)),
            patch.object(
                auth_mod,
                "verify_oauth_token",
                side_effect=AssertionError("must not be called for ink_ bearer tokens"),
            ),
        ):
            with TestClient(app) as test_client:
                r = test_client.post(
                    "/mcp",
                    headers={**_HTTP_MCP_HEADERS, "Authorization": "Bearer ink_still_an_api_key"},
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                )
        auth_mod._auth_service = None
        assert r.status_code == 200


class TestInsufficientScope:
    def test_missing_scope_returns_insufficient_scope_shape(self, client: TestClient):
        """A valid token that lacks the scope a tool needs gets the spec's
        `insufficient_scope` shape: a branchable error_class plus the scope
        a client would need to request next."""
        token = make_token(scope="kb:read")  # no kb:search
        r = client.post(
            "/mcp",
            headers={**_HTTP_MCP_HEADERS, "Authorization": f"Bearer {token}"},
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "search_documents", "arguments": {"query": "x"}},
            },
        )
        assert r.status_code == 200  # JSON-RPC/tool-level error, not a transport 401/403
        body = r.json()["result"]
        assert body["isError"] is True
        assert body["structuredContent"]["error"] == "insufficient_scope"
        assert body["structuredContent"]["scope"] == "kb:search"

    def test_insufficient_scope_is_not_a_transport_403(self, client: TestClient):
        """Pins the #295 review's blocking finding: the AC's literal wording
        ("returns 403 with error=insufficient_scope and a scope parameter")
        cannot be implemented at the transport layer for `tools/call` -- see
        `_call_tool_oauth`'s docstring (http_transport.py) for the full
        reasoning (the Streamable HTTP transport, mounted
        `json_response=True`, always answers a parsed JSON-RPC request with
        HTTP 200; only connection-level rejection -- before the body is even
        parsed -- can carry a real HTTP status, see
        `TestOAuthConnectionGate::test_aud_mismatch_rejected_with_401_over_http`
        for that path's genuine 401 + `WWW-Authenticate` challenge above).

        So the AC's INTENT (a machine-readable insufficient_scope signal
        naming the missing scope) is delivered as a JSON-RPC tool result
        instead of an HTTP challenge -- this test pins BOTH halves of that
        deviation explicitly: the status is 200, not 403, and there is no
        `WWW-Authenticate` header on this response (there is no HTTP-level
        challenge to carry one), so nothing about this response shape can be
        mistaken for the transport-level 401 challenge tested elsewhere in
        this file.
        """
        token = make_token(scope="kb:read")  # no kb:search
        r = client.post(
            "/mcp",
            headers={**_HTTP_MCP_HEADERS, "Authorization": f"Bearer {token}"},
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "search_documents", "arguments": {"query": "x"}},
            },
        )
        assert r.status_code == 200
        assert r.status_code != 403
        assert "www-authenticate" not in {h.lower() for h in r.headers}
        body = r.json()["result"]
        assert body["isError"] is True
        assert body["structuredContent"] == {
            "error_class": "authorization_failed",
            "error": "insufficient_scope",
            "scope": "kb:search",
        }

    def test_sufficient_scope_passes_the_scope_gate(self, client: TestClient):
        """A token WITH the required scope clears the scope check -- #295
        stops at authentication, so this comes back as a clearly-labeled
        'not yet implemented' rejection (identity resolution is out of this
        issue's scope, see Principal's docstring), not insufficient_scope
        and not a silent 200 with fabricated data."""
        token = make_token(scope="kb:read kb:search")
        r = client.post(
            "/mcp",
            headers={**_HTTP_MCP_HEADERS, "Authorization": f"Bearer {token}"},
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "search_documents", "arguments": {"query": "x"}},
            },
        )
        assert r.status_code == 200
        body = r.json()["result"]
        assert body["isError"] is True
        assert body["structuredContent"].get("error") != "insufficient_scope"


# --------------------------------------------------------------------------- #
# Never log or echo the token (design constraint #3)
# --------------------------------------------------------------------------- #


class TestTokenNeverLogged:
    def test_invalid_token_never_appears_in_captured_output(self, client: TestClient, capsys):
        secret_token_material = make_token(aud="https://wrong-resource.example/mcp")
        r = client.post(
            "/mcp",
            headers={**_HTTP_MCP_HEADERS, "Authorization": f"Bearer {secret_token_material}"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )
        assert r.status_code == 401
        # The response body/headers themselves must not echo it either.
        assert secret_token_material not in r.text
        assert secret_token_material not in r.headers.get("www-authenticate", "")
        captured = capsys.readouterr()
        assert secret_token_material not in captured.out
        assert secret_token_material not in captured.err

    async def test_token_validation_error_never_carries_the_token(self):
        """Even a caught TokenValidationError's own message/attrs must not
        contain the token -- covers logging that reads `str(exc)` or
        `repr(exc)` rather than the response body."""
        token = make_token(exp_delta_seconds=-60)
        with pytest.raises(TokenValidationError) as exc_info:
            await verify_oauth_token(token)
        assert token not in str(exc_info.value)
        assert token not in repr(exc_info.value)


# --------------------------------------------------------------------------- #
# OAuth caller -> Inherent user identity link (inherent#392 follow-up)
# --------------------------------------------------------------------------- #
# #295 shipped OAuth authentication with no way to ever execute a tool --
# every `tools/call` came back "not yet available" regardless of scope. That
# is unusable for #392's actual point (claude.ai's custom connectors, which
# ALWAYS connect via OAuth), so this adds a minimal, generic, config-first
# identity link: OAUTH_USER_ID_CLAIM (a claim on the token itself) checked
# before OAUTH_SUBJECT_USERS (a static operator mapping), both defaulting to
# "no link" so an unconfigured deployment is unaffected.
class TestOAuthIdentityResolution:
    async def test_no_config_resolves_to_no_identity(self):
        """Byte-for-byte today's (pre-follow-up) behaviour: with neither
        setting configured, resolved_user_id is None."""
        from src.services.auth import OAuthClaims, resolve_oauth_user

        claims = OAuthClaims(subject="oauth-user-123", scopes=frozenset({"kb:read"}), raw={})
        assert await resolve_oauth_user(claims) is None

    async def test_user_id_claim_resolves_directly(self, monkeypatch):
        from src.services.auth import OAuthClaims, resolve_oauth_user

        monkeypatch.setattr(settings, "oauth_user_id_claim", "inherent_user_id")
        claims = OAuthClaims(
            subject="oauth-user-123",
            scopes=frozenset({"kb:read"}),
            raw={"inherent_user_id": "user-42"},
        )
        assert await resolve_oauth_user(claims) == "user-42"

    async def test_user_id_claim_absent_from_token_falls_through(self, monkeypatch):
        """OAUTH_USER_ID_CLAIM is set but THIS token doesn't carry it --
        falls through to OAUTH_SUBJECT_USERS, not an error."""
        from src.services.auth import OAuthClaims, resolve_oauth_user

        monkeypatch.setattr(settings, "oauth_user_id_claim", "inherent_user_id")
        monkeypatch.setattr(settings, "_oauth_subject_users", {"oauth-user-123": "user-fallback"})
        claims = OAuthClaims(subject="oauth-user-123", scopes=frozenset(), raw={})
        assert await resolve_oauth_user(claims) == "user-fallback"

    async def test_subject_users_mapping_resolves(self, monkeypatch):
        from src.services.auth import OAuthClaims, resolve_oauth_user

        monkeypatch.setattr(settings, "_oauth_subject_users", {"oauth-user-123": "user-99"})
        claims = OAuthClaims(subject="oauth-user-123", scopes=frozenset(), raw={})
        assert await resolve_oauth_user(claims) == "user-99"

    async def test_unmapped_subject_resolves_to_none(self, monkeypatch):
        from src.services.auth import OAuthClaims, resolve_oauth_user

        monkeypatch.setattr(settings, "_oauth_subject_users", {"someone-else": "user-99"})
        claims = OAuthClaims(subject="oauth-user-123", scopes=frozenset(), raw={})
        assert await resolve_oauth_user(claims) is None

    async def test_claim_wins_over_mapping_when_both_configured(self, monkeypatch):
        from src.services.auth import OAuthClaims, resolve_oauth_user

        monkeypatch.setattr(settings, "oauth_user_id_claim", "inherent_user_id")
        monkeypatch.setattr(
            settings, "_oauth_subject_users", {"oauth-user-123": "user-from-mapping"}
        )
        claims = OAuthClaims(
            subject="oauth-user-123",
            scopes=frozenset(),
            raw={"inherent_user_id": "user-from-claim"},
        )
        assert await resolve_oauth_user(claims) == "user-from-claim"

    async def test_principal_from_oauth_claims_carries_resolved_user_id(self, monkeypatch):
        from src.services.auth import OAuthClaims

        monkeypatch.setattr(settings, "_oauth_subject_users", {"oauth-user-123": "user-99"})
        claims = OAuthClaims(subject="oauth-user-123", scopes=frozenset({"kb:read"}), raw={})
        principal = await Principal.from_oauth_claims(claims)
        assert principal.resolved_user_id == "user-99"

    def test_principal_from_api_key_resolved_user_id_is_its_own_user_id(self):
        """The API-key path needs no lookup at all -- identity IS the key's
        own user_id."""
        key_info = APIKeyInfo(
            key_id="key-1",
            user_id="user-1",
            workspace_id="ws-1",
            permissions=["read"],
            rate_limit=100,
        )
        assert Principal.from_api_key(key_info).resolved_user_id == "user-1"


class TestPermissionsFromScopes:
    def test_full_scopes_yield_full_permissions(self):
        from src.services.auth import permissions_from_scopes

        assert permissions_from_scopes(frozenset({"kb:read", "kb:search", "kb:write"})) == [
            "read",
            "search",
            "write",
        ]

    def test_read_only_scope_excludes_write(self):
        """The exact defense the coordinator's review asked to pin: a
        read-only token's derived permissions never include 'write'."""
        from src.services.auth import permissions_from_scopes

        perms = permissions_from_scopes(frozenset({"kb:read"}))
        assert "write" not in perms
        assert perms == ["read"]

    def test_no_scopes_yield_no_permissions(self):
        from src.services.auth import permissions_from_scopes

        assert permissions_from_scopes(frozenset()) == []


# --------------------------------------------------------------------------- #
# OAuth caller executing a tool end to end (inherent#392 follow-up)
# --------------------------------------------------------------------------- #
class TestOAuthToolExecution:
    @pytest.fixture(autouse=True)
    def _identity_link(self, monkeypatch):
        """Every test in this class runs with a configured, matching
        OAUTH_SUBJECT_USERS entry -- the claim path is covered separately
        above and, end to end, by TestOAuthIdentityResolution."""
        monkeypatch.setattr(settings, "_oauth_subject_users", {"oauth-user-123": "user-42"})
        yield

    def test_resolved_identity_can_call_a_read_tool(self, client: TestClient):
        """A token with 'search' scope and a resolved identity can now
        actually run search_documents -- previously always
        'not yet available' regardless of scope."""
        import src.mcp_server.server as mcp_server_mod

        token = make_token(scope="kb:read kb:search")
        db = AsyncMock()
        db.get_user_workspace_ids = AsyncMock(return_value=["ws-1"])
        db.get_documents_multi_workspace = AsyncMock(return_value=([], 0))
        with patch.object(mcp_server_mod, "get_database", AsyncMock(return_value=db)):
            r = client.post(
                "/mcp",
                headers={**_HTTP_MCP_HEADERS, "Authorization": f"Bearer {token}"},
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "list_documents", "arguments": {}},
                },
            )
        assert r.status_code == 200
        result = r.json()["result"]
        assert result["isError"] is False
        assert "user-42" not in result["content"][0]["text"]  # never leaks the raw identity link

    def test_read_only_token_cannot_call_a_write_tool(self, client: TestClient):
        """Scope gate still runs first: a token with no 'kb:write' scope is
        rejected as insufficient_scope, identity resolution notwithstanding."""
        token = make_token(scope="kb:read kb:search")  # no kb:write
        r = client.post(
            "/mcp",
            headers={**_HTTP_MCP_HEADERS, "Authorization": f"Bearer {token}"},
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "delete_document", "arguments": {"document_id": "doc-1"}},
            },
        )
        assert r.status_code == 200
        result = r.json()["result"]
        assert result["isError"] is True
        assert result["structuredContent"]["error"] == "insufficient_scope"

    def test_unmapped_subject_still_gets_a_clear_identity_rejection(
        self, monkeypatch, client: TestClient
    ):
        """No identity link matches this token's subject -- distinct from
        insufficient_scope: the token IS authorized, there is simply no
        Inherent user to run it against."""
        monkeypatch.setattr(settings, "_oauth_subject_users", {})  # no mapping at all
        token = make_token(scope="kb:read kb:search")
        r = client.post(
            "/mcp",
            headers={**_HTTP_MCP_HEADERS, "Authorization": f"Bearer {token}"},
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "list_documents", "arguments": {}},
            },
        )
        assert r.status_code == 200
        result = r.json()["result"]
        assert result["isError"] is True
        assert result["structuredContent"].get("error") != "insufficient_scope"
        assert "no Inherent identity is linked" in result["content"][0]["text"]

    def test_tools_list_over_oauth_is_unaffected_by_this_change_when_unmapped(
        self, monkeypatch, client: TestClient
    ):
        monkeypatch.setattr(settings, "_oauth_subject_users", {})
        token = make_token()
        r = client.post(
            "/mcp",
            headers={**_HTTP_MCP_HEADERS, "Authorization": f"Bearer {token}"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )
        assert r.status_code == 200
