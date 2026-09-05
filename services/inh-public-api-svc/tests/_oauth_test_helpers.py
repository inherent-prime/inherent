"""Shared RSA keypair + JWT-minting helpers for OAuth resource-server tests
(#295).

NOT itself a test module (no `test_` prefix, so pytest's `testpaths`
collection skips it) -- imported by both
``tests/unit/test_oauth_config_gate.py`` and
``tests/security/test_oauth_token_validation.py`` so the two files' token
setup cannot quietly drift from each other. All-offline: a single RSA
keypair is generated once at import time and every helper here operates on
it directly (no HTTP, no real JWKS endpoint) -- see ``patch_jwks_client``.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

# Generated once per test process -- these tests only ever verify tokens
# signed by this exact key, so there is nothing to persist or rotate.
_PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PUBLIC_KEY = _PRIVATE_KEY.public_key()

# Deliberately distinct from any real Inherent domain -- these never leave
# the test process, but keeping them obviously-fake avoids any confusion
# with the real hosted values in settings.py's docstrings.
ISSUER = "https://auth.test.inherent.example"
RESOURCE = "https://api.test.inherent.example/mcp"


def make_token(
    *,
    aud: str | list[str] = RESOURCE,
    iss: str = ISSUER,
    sub: str | None = "oauth-user-123",
    scope: str = "kb:read kb:search",
    exp_delta_seconds: int = 3600,
    **extra_claims: Any,
) -> str:
    """Mint an RS256 JWT signed by this module's test keypair.

    Every parameter defaults to a VALID token for ``RESOURCE`` /
    ``ISSUER`` -- tests override exactly the one claim under test (e.g.
    ``aud="https://someone-elses-resource"``) rather than reconstructing
    every claim, so each test's intent is legible from its one override.
    """
    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": iss,
        "sub": sub,
        "scope": scope,
        "iat": now,
        "exp": now + exp_delta_seconds,
        **extra_claims,
    }
    if aud is not None:
        claims["aud"] = aud
    if sub is None:
        claims.pop("sub", None)
    return jwt.encode(claims, _PRIVATE_KEY, algorithm="RS256")


def patch_jwks_client(monkeypatch, auth_module) -> None:
    """Monkeypatch ``auth_module._get_jwks_client`` so ``verify_oauth_token``
    resolves the REAL public key above with no network call at all -- the
    offline equivalent of the authorization server's published JWKS
    endpoint returning this module's one key.
    """
    fake_signing_key = SimpleNamespace(key=PUBLIC_KEY)

    class _FakeJWKSClient:
        def get_signing_key_from_jwt(self, token: str) -> SimpleNamespace:
            return fake_signing_key

    monkeypatch.setattr(auth_module, "_get_jwks_client", lambda url: _FakeJWKSClient())
