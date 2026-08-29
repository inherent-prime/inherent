"""OAuth 2.1 resource-server config gate (#295).

Pins the acceptance criteria that make `oauth_enabled=False` (the default)
a hard guarantee, not just a nicety (issue #295 comment, 2026-08-18): a
self-hosted deployment that never opts in must see BYTE-IDENTICAL behavior
to before #295 -- no `/.well-known/oauth-protected-resource` route at all,
and `/mcp`'s 401 carrying the exact same `WWW-Authenticate: ApiKey`
challenge it always has. With the flag on, both schemes must be advertised
together (design constraint #1 -- never silently replacing `ApiKey`).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from src.config import settings
from src.config.settings import Settings
from src.main import create_app

pytestmark = pytest.mark.unit

_HTTP_MCP_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}


def _client(app):
    with patch("src.main.get_database", new_callable=AsyncMock):
        with TestClient(app) as client:
            yield client


# --------------------------------------------------------------------------- #
# Settings defaults
# --------------------------------------------------------------------------- #


def test_oauth_disabled_by_default():
    """A fresh Settings() -- what every deployment gets without explicit
    opt-in -- has OAuth off and every dependent field unset."""
    s = Settings()
    assert s.oauth_enabled is False
    assert s.oauth_authorization_server is None
    assert s.oauth_resource_identifier is None
    assert s.oauth_scopes_supported == ["kb:read", "kb:search"]


def test_effective_jwks_url_prefers_explicit_override():
    s = Settings(
        oauth_authorization_server="https://auth.example.com",
        oauth_jwks_url="https://auth.example.com/custom/jwks",
    )
    assert s.effective_oauth_jwks_url == "https://auth.example.com/custom/jwks"


def test_effective_jwks_url_derived_from_authorization_server():
    s = Settings(oauth_authorization_server="https://auth.example.com/")
    assert s.effective_oauth_jwks_url == "https://auth.example.com/.well-known/jwks.json"


def test_effective_jwks_url_none_when_unconfigured():
    assert Settings().effective_oauth_jwks_url is None


# --------------------------------------------------------------------------- #
# /.well-known/oauth-protected-resource -- registered ONLY when enabled
# --------------------------------------------------------------------------- #


class TestProtectedResourceMetadataRoute:
    def test_absent_when_oauth_disabled(self, monkeypatch):
        """Acceptance criterion, pinned explicitly (not just via /mcp's
        401): the well-known document 404s when OAuth is off."""
        monkeypatch.setattr(settings, "oauth_enabled", False)
        app = create_app()
        for client in _client(app):
            r = client.get("/.well-known/oauth-protected-resource")
            assert r.status_code == 404

    def test_present_and_shaped_when_oauth_enabled(self, monkeypatch):
        monkeypatch.setattr(settings, "oauth_enabled", True)
        monkeypatch.setattr(settings, "oauth_authorization_server", "https://auth.inherent.sh")
        monkeypatch.setattr(settings, "oauth_resource_identifier", "https://api.inherent.sh/mcp")
        app = create_app()
        for client in _client(app):
            r = client.get("/.well-known/oauth-protected-resource")
            assert r.status_code == 200
            body = r.json()
            assert body["resource"] == "https://api.inherent.sh/mcp"
            assert body["authorization_servers"] == ["https://auth.inherent.sh"]
            # Minimal catalogue only (scope-minimisation) -- "write" is
            # deliberately absent even though PERMISSION_SCOPE_MAP maps it;
            # it arrives via an insufficient_scope step-up on the specific
            # tool instead (a JSON-RPC tools/call result, not a transport
            # 403 -- see http_transport.py's _call_tool_oauth docstring).
            assert body["scopes_supported"] == ["kb:read", "kb:search"]
            assert body["bearer_methods_supported"] == ["header"]


# --------------------------------------------------------------------------- #
# /mcp 401 -- byte-identical off, dual-scheme on
# --------------------------------------------------------------------------- #


class TestMcpChallengeGate:
    def test_401_challenge_unchanged_when_oauth_disabled(self, monkeypatch):
        """The literal pre-#295 behavior: missing credentials on /mcp get
        `WWW-Authenticate: ApiKey` and NOTHING else appended."""
        monkeypatch.setattr(settings, "oauth_enabled", False)
        app = create_app()
        for client in _client(app):
            r = client.post(
                "/mcp",
                headers=_HTTP_MCP_HEADERS,
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            )
            assert r.status_code == 401
            assert r.headers["www-authenticate"] == "ApiKey"

    def test_401_advertises_both_schemes_when_oauth_enabled(self, monkeypatch):
        monkeypatch.setattr(settings, "oauth_enabled", True)
        monkeypatch.setattr(settings, "oauth_authorization_server", "https://auth.inherent.sh")
        monkeypatch.setattr(settings, "oauth_resource_identifier", "https://api.inherent.sh/mcp")
        app = create_app()
        for client in _client(app):
            r = client.post(
                "/mcp",
                headers=_HTTP_MCP_HEADERS,
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            )
            assert r.status_code == 401
            challenge = r.headers["www-authenticate"]
            # Both schemes present -- ApiKey is never silently replaced.
            assert challenge.startswith("ApiKey, Bearer ")
            assert "resource_metadata=" in challenge
            assert "/.well-known/oauth-protected-resource" in challenge
            assert 'scope="kb:read kb:search"' in challenge
            # No credential was presented at all -- RFC 6750 Sec 3 says
            # don't include an error code in that case.
            assert "error=" not in challenge

    def test_invalid_api_key_also_advertises_both_schemes_when_enabled(self, monkeypatch):
        """An invalid X-API-Key still gets the combined challenge -- OAuth
        stays advertised as a fallback even though the caller's chosen
        scheme (ApiKey) is the one that actually failed."""
        import src.services.auth as auth_mod

        monkeypatch.setattr(settings, "oauth_enabled", True)
        monkeypatch.setattr(settings, "oauth_authorization_server", "https://auth.inherent.sh")
        monkeypatch.setattr(settings, "oauth_resource_identifier", "https://api.inherent.sh/mcp")
        app = create_app()
        db = AsyncMock()
        db.validate_api_key = AsyncMock(return_value=None)
        auth_mod._auth_service = None
        with (
            patch("src.main.get_database", new_callable=AsyncMock),
            patch("src.services.auth.get_database", new=AsyncMock(return_value=db)),
        ):
            with TestClient(app) as client:
                r = client.post(
                    "/mcp",
                    headers={**_HTTP_MCP_HEADERS, "X-API-Key": "bad"},
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                )
        auth_mod._auth_service = None
        assert r.status_code == 401
        assert r.headers["www-authenticate"].startswith("ApiKey, Bearer ")
