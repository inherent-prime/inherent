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
    def test_from_api_key_and_from_oauth_claims_share_one_shape(self):
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
        from_oauth = Principal.from_oauth_claims(claims)
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
