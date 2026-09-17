"""Live RFC 9728 discovery-handshake E2E against the compose stack (#295).

PR #317's Summary claimed "CI must prove what I could not" for the discovery
handshake, but shipped no compose-marked test for it -- there was nothing in
the compose lane for CI to actually run (blocking review finding, PR #317
review #5058608817). This file is that test.

Everything in ``tests/security/test_oauth_token_validation.py`` drives the
handshake OFFLINE: an in-process app, a monkeypatched ``settings`` singleton,
no real network hop. That proves the *decision* logic (header assembly,
metadata-document shape) but never that a REAL MCP client, going over the
wire through the REAL ASGI middleware stack of a REAL running container,
actually gets a usable challenge -- a proxy stripping headers, a
misconfigured base URL, or a route registered under the wrong path would all
be invisible to the offline suite and invisible to every other test in
``test_compose_mcp.py`` (which only ever exercises OAuth-disabled -- i.e.
today's default -- ``/mcp``).

Requires the compose stack to be booted with OAuth actually enabled --
unlike the ``API_KEY`` / ``WORKSPACE_ID`` fixtures the rest of this package's
compose suite shares, this is NOT docker-compose.yml's default (OAuth stays
off by default there too, deliberately, for the same self-host reason
``settings.oauth_enabled`` defaults off in code -- see docker-compose.yml's
``OAUTH_ENABLED`` comment). ``.github/workflows/integration.yml`` opts the
CI compose job in via ``OAUTH_ENABLED=true`` / ``OAUTH_AUTHORIZATION_SERVER``
/ ``OAUTH_RESOURCE_IDENTIFIER`` job-level env, which both configures the
container (through docker-compose.yml's ``${VAR:-false}``-style passthrough)
and is read back here so the assertions below check against the SAME values
the container was actually given, not values invented independently of it.
Run locally against a stack booted the same way::

    OAUTH_ENABLED=true \\
    OAUTH_AUTHORIZATION_SERVER=https://oauth.inherent-test.example \\
    OAUTH_RESOURCE_IDENTIFIER=http://localhost:18000/mcp \\
        docker compose up -d --build --wait inh-public-api-svc
    uv run pytest tests/integration/test_compose_mcp_oauth.py -v --no-cov

Deliberately does NOT touch bearer-token verification (aud/iss/exp/signature
against a real IdP) -- that needs a real authorization server's JWKS
endpoint, which this repo does not stand up, and is already the offline
suite's job (``tests/security/test_oauth_token_validation.py``). This file's
scope is exactly the review finding's: the discovery handshake, end to end.
"""

from __future__ import annotations

import os
import re

import httpx
import pytest

pytestmark = [pytest.mark.compose, pytest.mark.integration]

API_URL = os.environ.get("PUBLIC_API_URL", "http://localhost:18000").rstrip("/")
MCP_URL = f"{API_URL}/mcp"

# Same env vars, same defaults, that .github/workflows/integration.yml's
# job-level `env:` hands the container (via docker-compose.yml's
# `${OAUTH_AUTHORIZATION_SERVER:-}` passthrough) -- read back here so the
# metadata-document assertions below check against what this run's container
# was ACTUALLY configured with, never a value hardcoded independently of it.
EXPECTED_AUTHORIZATION_SERVER = os.environ.get(
    "OAUTH_AUTHORIZATION_SERVER", "https://oauth.inherent-test.example"
)
EXPECTED_RESOURCE_IDENTIFIER = os.environ.get(
    "OAUTH_RESOURCE_IDENTIFIER", "http://localhost:18000/mcp"
)

# RFC 6750 sec 3 `WWW-Authenticate` challenge parameter grammar:
# `token68 / ( "," #auth-param )`, auth-params as `name=quoted-string`.
_RESOURCE_METADATA_RE = re.compile(r'resource_metadata="([^"]+)"')


def _require_oauth_stack(client: httpx.Client) -> None:
    """Skip (don't fail) when no healthy, OAuth-enabled stack is reachable.

    Mirrors ``test_compose_mcp.py``'s ``_require_stack`` skip-not-fail
    pattern for "no stack at all", plus a second skip for the narrower "a
    stack is up, but this run didn't opt it into OAuth" case (e.g. a plain
    local `make dev`, or a compose job that didn't set the env this file
    needs) -- distinguishing the two in the skip reason instead of letting
    the latter surface as a confusing assertion failure deep in the test
    body.
    """
    try:
        resp = client.get(f"{API_URL}/health", timeout=5)
    except httpx.HTTPError as exc:
        pytest.skip(f"public API not reachable at {API_URL}: {exc}")
    if resp.status_code != 200:
        pytest.skip(f"public API unhealthy at {API_URL}: HTTP {resp.status_code}")

    # A cheap, side-effect-free probe of whether THIS container has OAuth on:
    # the well-known route is registered (200) only when it does (#295 -- see
    # src/api/well_known.py's module docstring: "not registered-but-guarded
    # inside the handler"). A 404 here means either OAuth is off (today's
    # docker-compose.yml default) or an older image predates #295 entirely --
    # either way, the discovery handshake this file exists to prove has
    # nothing to run against.
    probe = client.get(f"{API_URL}/.well-known/oauth-protected-resource", timeout=5)
    if probe.status_code == 404:
        pytest.skip(
            "stack is up but OAuth is not enabled on it (well-known route 404s) -- "
            "run with OAUTH_ENABLED=true (see this file's module docstring), as "
            ".github/workflows/integration.yml's compose-integration job does"
        )


@pytest.fixture(scope="module")
def client() -> httpx.Client:
    with httpx.Client(timeout=30) as c:
        _require_oauth_stack(c)
        yield c


def test_discovery_handshake_end_to_end(client: httpx.Client) -> None:
    """The full RFC 9728 chain, against a live container over the wire:

    1. An unauthenticated request to ``/mcp`` comes back 401 carrying a
       ``WWW-Authenticate: Bearer`` challenge naming a ``resource_metadata``
       URL (MCP authorization spec: "MCP servers MUST use the HTTP header
       WWW-Authenticate ... to indicate the location of the resource server
       metadata URL").
    2. That URL, fetched with NO special client configuration (a
       spec-compliant client discovers it, it does not hardcode it), serves
       an RFC 9728 protected-resource metadata document.
    3. The document carries RFC 9728's required/contractual fields with the
       shapes issue #295 specified.
    """
    # Step 1: unauthenticated POST to /mcp -- the same request shape a real
    # MCP client's `tools/list` probe would send (headers from
    # test_compose_mcp.py's `_HTTP_MCP_HEADERS`), just with no credentials at
    # all, so the ASGI auth gate (mount_mcp_http's mcp_asgi_app) rejects it
    # before any JSON-RPC framing is even parsed.
    unauthenticated = client.post(
        MCP_URL,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    assert unauthenticated.status_code == 401, (
        f"expected 401 on unauthenticated /mcp, got {unauthenticated.status_code}: "
        f"{unauthenticated.text[:400]}"
    )

    www_authenticate = unauthenticated.headers.get("www-authenticate")
    assert www_authenticate is not None, "401 response carries no WWW-Authenticate header at all"

    # BOTH schemes advertised, never silently dropping ApiKey (design
    # constraint #1, PR #317's "Challenge values" table) -- an existing
    # API-key client reading this same 401 must still learn it can use one.
    assert "ApiKey" in www_authenticate, f"ApiKey challenge missing: {www_authenticate!r}"
    assert "Bearer" in www_authenticate, f"Bearer challenge missing: {www_authenticate!r}"

    # No credential was presented at all, so RFC 6750 Sec 3 says this
    # challenge must NOT carry an `error` parameter (that's reserved for a
    # PRESENTED-but-invalid credential) -- pinning that this handshake's
    # very first response doesn't misrepresent "you sent nothing" as
    # "you sent something wrong".
    assert (
        'error="' not in www_authenticate
    ), f"no-credential 401 should not carry an error param: {www_authenticate!r}"

    match = _RESOURCE_METADATA_RE.search(www_authenticate)
    assert match, f"no resource_metadata param in challenge: {www_authenticate!r}"
    resource_metadata_url = match.group(1)

    # Step 2: follow the discovered URL -- exactly what a spec-compliant
    # client does next, not a hardcoded guess at the well-known path.
    metadata_response = client.get(resource_metadata_url)
    assert metadata_response.status_code == 200, (
        f"discovered resource_metadata URL {resource_metadata_url!r} did not resolve: "
        f"{metadata_response.status_code} {metadata_response.text[:400]}"
    )
    document = metadata_response.json()

    # Step 3: RFC 9728 field validation.
    # `resource` is the one field RFC 9728 sec 2 marks REQUIRED outright.
    assert (
        isinstance(document.get("resource"), str) and document["resource"]
    ), f"RFC 9728 'resource' missing or empty: {document}"
    assert document["resource"] == EXPECTED_RESOURCE_IDENTIFIER, (
        f"'resource' {document['resource']!r} does not match this run's "
        f"OAUTH_RESOURCE_IDENTIFIER {EXPECTED_RESOURCE_IDENTIFIER!r}"
    )

    # `authorization_servers` is RFC 9728's recommended discovery field and
    # is exactly what issue #295's design proposes this endpoint serve --
    # a client that can't find an authorization server here has nothing to
    # start a browser sign-in flow with.
    auth_servers = document.get("authorization_servers")
    assert (
        isinstance(auth_servers, list) and auth_servers
    ), f"RFC 9728 'authorization_servers' missing or empty: {document}"
    assert (
        EXPECTED_AUTHORIZATION_SERVER in auth_servers
    ), f"{EXPECTED_AUTHORIZATION_SERVER!r} not in authorization_servers {auth_servers!r}"

    # `scopes_supported` -- deliberately the MINIMAL catalogue per #295's
    # scope-minimisation guidance (write/delete arrive via a later
    # insufficient_scope step-up, not advertised upfront).
    scopes = document.get("scopes_supported")
    assert isinstance(scopes, list) and scopes, f"RFC 9728 'scopes_supported' missing: {document}"
    assert all(isinstance(s, str) for s in scopes)
    assert "kb:read" in scopes and "kb:search" in scopes, f"unexpected scopes: {scopes!r}"

    # `bearer_methods_supported` -- issue #295's proposed document body names
    # this explicitly; RFC 9728 defines it as OPTIONAL, but once present its
    # values are constrained to the registered set, and this deployment only
    # ever accepts the token in the Authorization header.
    assert document.get("bearer_methods_supported") == [
        "header"
    ], f"unexpected bearer_methods_supported: {document.get('bearer_methods_supported')!r}"
