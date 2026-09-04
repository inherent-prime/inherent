# MCP OAuth Resource Server — Implementation Plan (OSS engine, `inh-public-api-svc`)

> **For agentic workers:** REQUIRED SUB-SKILL: use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A beta user pastes `https://api.inherent.sh/mcp` into Claude Code / Cursor / Claude Desktop, clicks **Authenticate**, signs in via Clerk, and returns to a connected client that can search their knowledge base. This repo's job in that flow is exactly one role: **`inh-public-api-svc` becomes a pure OAuth 2.1 resource server** — it validates the presented token, resolves the identity behind it, enforces that identity's entitlements, and never mints a token.

**Architecture:**

```
Claude Code / Cursor / Claude Desktop
  ① POST /mcp, no credential
  ▼
inh-public-api-svc  →  401 + WWW-Authenticate: Bearer
                           resource_metadata="https://api.inherent.sh/.well-known/oauth-protected-resource"
  ② GET that RFC 9728 document (served by THIS repo, Task 2)
       { resource, authorization_servers: ["https://clerk.inherent.sh"], scopes_supported: [], bearer_methods_supported: ["header"] }
  ▼
clerk.inherent.sh   →  RFC 8414 discovery → /authorize (PKCE S256) → browser sign-in → /token
  ▼
  ③ POST /mcp  Authorization: Bearer <JWT>
  ▼
inh-public-api-svc
   Task 3  validate: JWKS signature · iss · exp/nbf · AUDIENCE (see Global Constraints)
   Task 4  resolve: POST {intg-svc}/internal/identity/resolve  → user_id, workspaces, plan, limits, usage   [60 s cache, FAIL CLOSED]
   Task 1  wrap:    Principal { key_info, auth_method, limits, ... }  ← the single shape every tool handler sees
   Task 8  enforce: calls_per_minute · writes_per_day · calls_per_month · max_documents
   Task 9  meter:   publish usage async; a metering failure never fails a call
   ▼
_TOOLS dispatch — unchanged handlers, unchanged #138 workspace rule
```

The authorization server is **not** in this repo and never will be. Clerk (`https://clerk.inherent.sh`) is it. There is no `/authorize`, no `/token`, no consent UI, no refresh rotation here.

**Tech Stack:** Python 3.11 · FastAPI + Starlette · `mcp>=1.1.2,<2` low-level `Server` · pydantic-settings · asyncpg/SQLAlchemy (Postgres) · motor (Mongo, read-only) · **new: `pyjwt[crypto]`** · pytest 9 (`asyncio_mode = "auto"`, `pythonpath = ["src"]`) · ruff/black/mypy/bandit

**Spec:** `inherent-systems/prime` → `project/dev/docs/MCP-BETA-OAUTH-DESIGN.md` (2026-08-19, with the 2026-08-20 **CORRECTION** and **SUPERSEDED** blocks binding)
**Epic:** `inherent-systems/prime#273` (Phases 1–2 of §8)
**Sibling plan (prime repo, do not duplicate):** `.claude/docs/superpowers/plans/2026-08-20-mcp-beta-oauth.md`

---

## Scope

| In (this plan, this repo) | Out |
|---|---|
| Tasks 1–4 — OAuth resource server: Principal spine, RFC 9728 metadata + Bearer challenge, JWT validation, identity resolution (**#295**) | The authorization server itself — Clerk. No `/authorize`, `/token`, consent UI, DCR endpoint, or refresh rotation lands here. |
| Task 5 — `list_workspaces` tool (**#297**) | `POST /internal/identity/resolve` — **built** in the prime repo (sibling plan Task 3). This plan only *calls* it. |
| Tasks 6–7 — `event_id` on MCP searches; `report_feedback` usable over HTTP (**#296**, split — see Task 7) | The plan/quota *model* (`memory_writes_per_day`, `mcp_calls_per_minute` values) — prime repo, sibling Task 2. **No plan names or tier values belong in this repo** (#309). |
| Tasks 8–9 — per-identity entitlement + quota enforcement, async metering (**#309**) | `/connect` consent page, dashboard connect UX, Clerk DCR toggle — prime repo, sibling Tasks 4–5. |
| Task 10 — compose-lane discovery handshake contract tests + docs sweep | Conversation ingestion / `remember` tool (#306–#308), tool behaviour annotations (#308) — separate plan. |
| | Phase 0 prod engine bump to v0.6.0 — prime repo, sibling Task 1. **Nothing in this plan is verifiable in production until it lands**, but everything here is verifiable locally without it. |

---

## Global Constraints

Read these before writing a line. Several of them invalidate text that is still sitting in the GitHub issues.

1. **Clerk supports NO custom scopes.** `kb:read` / `kb:search` do not exist and cannot be created. Clerk's `scopes_supported` is a fixed set (`openid, profile, email, public_metadata, private_metadata, offline_access, user:org:read`). **This overrides issue #295's proposed body and its `scope="kb:read kb:search"` challenge parameter, and overrides spec §4.5's original text** (see the SUPERSEDED block). Authorization is **by identity plus plan**, never by scope strings. Any step that reads a custom scope claim, advertises one in `scopes_supported`, or returns `403 insufficient_scope` for a missing scope is wrong. Serve `"scopes_supported": []` and emit no `scope` parameter on the challenge — advertising a scope the client will then request gets it an `invalid_scope` from Clerk.

2. **The audience check is an explicit design decision with a named fallback.** Clerk advertises **no** `resource_indicators_supported` (RFC 8707), so a Clerk-minted token's `aud` may not name `https://api.inherent.sh/mcp`. The MCP spec is nonetheless normative that a resource server MUST only accept tokens minted for it. Therefore Task 3 ships a **three-mode audience ladder**, selected by `oauth_audience_mode`, every mode failing closed when its own required config is missing:

   | Mode | Check | Strength |
   |---|---|---|
   | `resource_indicator` (**default**) | `aud` (or the `resource` claim) contains `settings.oauth_resource_identifier` | **Strong** — the spec-compliant check. Works unchanged the day Clerk gains RFC 8707. |
   | `client_id_allowlist` (**named fallback**) | `azp` / `client_id` claim ∈ `settings.oauth_allowed_client_ids` (empty list = reject everything) | **Weaker.** Proves the token was minted for a client *we registered*, not for *our resource*. A second resource protected by the same Clerk instance and the same client would be indistinguishable. |
   | `introspection` (last resort) | `POST {oauth_introspection_endpoint}` (Clerk's `/oauth/token_info`), then the same `client_id` check on the response | **Weakest, and costs a network round trip per MCP call.** Introspection proves the token is valid *for Clerk*, which is not the question being asked. |

   **The decision to record:** ship `resource_indicator` as the default and as the only mode enabled in config by default. Switch prod to `client_id_allowlist` **only** after a human completes one real authorization-code flow, decodes the resulting access token, and confirms `aud` carries nothing usable — and record that decoded (redacted) claim set in #295 as the evidence. Never widen this silently. A token that merely verifies against Clerk's JWKS is **not** accepted by any mode.

3. **Dynamic Client Registration is OFF on the live Clerk instance.** `registration_endpoint` is absent from both discovery documents (`POST /oauth/register` returns 422, not 404 — implemented but instance-flag-gated). No MCP client can register until a human toggles it. **Every test in this plan must therefore run against locally-signed synthetic tokens with a mocked/injected JWKS. No test may require, fetch, or assert against a real Clerk-issued token.** Task 3 Step 1 builds the RSA-keypair + JWKS test factory precisely so this constraint is structural rather than a rule people remember.

4. **Fail closed, always.** If a token cannot be validated, or an identity cannot be resolved, **reject**. Never fall back to a permissive plan, a default workspace, an unlimited limit set, or a cached identity that has aged out. When intg-svc is unreachable and the identity is not already in the 60 s cache, the request is refused — API-key auth is entirely unaffected by that outage.

5. **`oauth_enabled` defaults to `false`, and with it off `/mcp` behaves *exactly* as today.** Byte-identical `WWW-Authenticate: ApiKey` 401, and `GET /.well-known/oauth-protected-resource` **404s**. A self-hosted stack must never advertise an authorization server it does not run. API keys are **not** being removed: `X-API-Key` and `Authorization: Bearer ink_…` stay first-class on REST, on `/mcp`, and in the stdio `api_key` schema argument — permanently, for CI and self-hosters.

6. **Every existing test stays green *unedited*.** Specifically `tests/security/test_auth_regression.py`, `tests/unit/test_auth_service.py`, `tests/test_api_key_auth.py`, and all of `tests/contract/test_mcp_http_transport.py` — including its `_call_http_tool` helper, which sets `http_transport._current_key_info` directly. This is why Task 1 **adds** a `_current_principal` contextvar beside `_current_key_info` rather than renaming it. If a task requires editing one of those files, the design is wrong; change the design.

7. **Secrets never appear in logs, error bodies, metric labels, or responses.** Not the raw JWT, not `identity_resolver_secret`, not an API key. Log a token by its `sub` and `jti` only. Reject reasons in a 401 body must be generic (`"Invalid or expired token"`); the specific reason goes to a structured log field and a metric label drawn from a **closed set** of strings.

8. **TDD, no exceptions.** Every task writes a failing test first, runs it, watches it fail for the *right* reason, then implements. Real commands, this repo's real runner:
   ```bash
   cd services/inh-public-api-svc && uv run pytest tests/unit/test_x.py    # single spec
   cd services/inh-public-api-svc && uv run pytest                          # full offline suite (-m 'not compose' via addopts)
   make lint && make format-check && make type-check && make security-check  # repo root
   make test-integration                                                     # compose lane; needs a running stack
   ```
   Never pass a bare `-m <marker>` on the command line: it *replaces* the service's `-m 'not compose'` `addopts` default rather than intersecting with it (#209/#286). Use the `make` targets for marked lanes.

9. **Branch and PR discipline.** Work on `feat/mcp-oauth-resource-server` off `main`. Check `git branch --show-current` before every stage. **Never commit to `main` or `dev` directly.** One PR against `main` (this repo's rule, AGENTS.md), or one PR per task if the reviewer prefers — but never a direct push.

10. **Docs + CHANGELOG in the same commit as the behaviour.** Every task that changes API surface, configuration, or the tool surface updates `docs/reference/mcp-tools.md` and/or `docs/reference/configuration.md`, and adds a one-line `[Unreleased]` entry in `CHANGELOG.md` under a Keep a Changelog heading ending in `(#issue)`. The `Docs` CI check must stay green.

---

## File Structure

### New files

| Path | Purpose |
|---|---|
| `services/inh-public-api-svc/src/services/principal.py` | `Principal` + `Limits` dataclasses — the one shape both credential types resolve into. |
| `services/inh-public-api-svc/src/services/oauth/__init__.py` | Package marker; re-exports the public entry points. |
| `services/inh-public-api-svc/src/services/oauth/metadata.py` | Builds the RFC 9728 protected-resource document and the `WWW-Authenticate: Bearer` challenge string. Pure functions. |
| `services/inh-public-api-svc/src/services/oauth/jwks.py` | Fetches and caches the authorization server's JWKS; resolves a `kid` to a verification key. |
| `services/inh-public-api-svc/src/services/oauth/token_validator.py` | Signature / `iss` / `exp` / `nbf` / **audience-ladder** validation. Returns validated claims or raises a typed rejection. |
| `services/inh-public-api-svc/src/services/oauth/identity_client.py` | HTTP client for `POST {intg-svc}/internal/identity/resolve`, plus the 60 s TTL cache. Fails closed. |
| `services/inh-public-api-svc/src/services/entitlements.py` | Per-identity quota decisions. Pure decision logic + the limiter calls; no plan names, no tier values. |
| `services/inh-public-api-svc/src/services/metering.py` | Async usage publisher. Fire-and-forget; never raises into the serving path. |
| `services/inh-public-api-svc/src/api/well_known.py` | Root-level router serving `GET /.well-known/oauth-protected-resource` (404 when `oauth_enabled` is false). |
| `services/inh-ingestion-svc/scripts/migrations/018_eval_events_fanout.sql` | Widens `eval_query_events` PK to `(event_id, workspace_id)` so one fan-out event id can span workspaces (Task 7 only). |
| `services/inh-public-api-svc/tests/oauth_helpers.py` | RSA keypair + JWKS + locally-signed-token factory shared by every OAuth spec. **The reason no test needs Clerk.** |
| `services/inh-public-api-svc/tests/unit/test_principal.py` | Task 1 |
| `services/inh-public-api-svc/tests/unit/test_oauth_metadata.py` | Task 2 |
| `services/inh-public-api-svc/tests/unit/test_oauth_jwks.py` | Task 3 |
| `services/inh-public-api-svc/tests/unit/test_oauth_token_validator.py` | Task 3 — including one spec class per audience mode |
| `services/inh-public-api-svc/tests/unit/test_identity_client.py` | Task 4 |
| `services/inh-public-api-svc/tests/unit/test_entitlements.py` | Task 8 |
| `services/inh-public-api-svc/tests/unit/test_metering.py` | Task 9 |
| `services/inh-public-api-svc/tests/contract/test_mcp_oauth_challenge.py` | Tasks 2 + 4 — the flag-off/flag-on 401 and discovery contract |
| `services/inh-public-api-svc/tests/contract/test_list_workspaces.py` | Task 5 |
| `services/inh-public-api-svc/tests/contract/test_mcp_event_capture.py` | Tasks 6 + 7 |
| `services/inh-public-api-svc/tests/security/test_oauth_token_boundaries.py` | Tasks 3 + 4 — foreign audience, wrong issuer, expired, unresolvable identity, cross-tenant |
| `services/inh-public-api-svc/tests/integration/test_compose_mcp_oauth.py` | Task 10 — `compose`-marked end-to-end discovery handshake |

### Modified files

| Path | Change |
|---|---|
| `services/inh-public-api-svc/pyproject.toml` | Add `pyjwt[crypto]>=2.9.0` to `[project.dependencies]`. |
| `services/inh-public-api-svc/src/config/settings.py` | The `oauth_*` / `identity_resolver_*` block (Task 2), plus `oauth_upgrade_url` (Task 8). |
| `services/inh-public-api-svc/src/mcp_server/http_transport.py` | `_current_principal` contextvar (Task 1); credential-shape dispatch in `mcp_asgi_app` (Tasks 2–4); quota gate in `call_tool` (Task 8); metering hook (Task 9). |
| `services/inh-public-api-svc/src/mcp_server/server.py` | `_handle_list_workspaces` + registry entry (Task 5); `event_id` in `_run_search` / `_handle_search` (Task 6); flip `report_feedback` to `http_exposed=True` (Task 6). |
| `services/inh-public-api-svc/src/services/auth.py` | `build_bearer_challenge()` used by the 401 raise path — **behaviour unchanged when `oauth_enabled` is false** (Task 2). |
| `services/inh-public-api-svc/src/services/database.py` | `get_workspace_names()` + `get_workspace_document_counts()` (Task 5); `count_workspace_documents()` for `max_documents` (Task 8); fan-out event insert (Task 7). |
| `services/inh-public-api-svc/src/services/eval_capture.py` | `record_query_event` gains a multi-workspace form (Task 7). |
| `services/inh-public-api-svc/src/services/metrics.py` | `oauth_token_rejected_total{reason}`, `identity_resolution_failed_total{reason}`, `mcp_quota_exceeded_total{limit}`, `metering_publish_failed_total`. |
| `services/inh-public-api-svc/src/main.py` | Include the well-known router (Task 2). |
| `services/inh-public-api-svc/tests/integration/test_compose_mcp.py` | Remove the strict `xfail` on `test_http_report_feedback_closes_loop` (Task 6). **Removed, not relaxed.** |
| `docs/reference/mcp-tools.md`, `docs/reference/configuration.md`, `CHANGELOG.md` | Per-task, per Global Constraint 10. |

---

## Task 1: `Principal` — one shape for both credential types (#295)

The spine. Issue #295's own "suggested refactor" section asks for exactly this: `_TOOLS` handlers and `get_authorized_workspace_ids` must never learn which door the caller came through. Nothing downstream (Tasks 4, 5, 8, 9) can be built until this exists. **This task changes no behaviour** — it is a pure widening, and the whole existing suite must pass unedited.

**Files:**
- Create: `services/inh-public-api-svc/src/services/principal.py`
- Modify: `services/inh-public-api-svc/src/mcp_server/http_transport.py`
- Test: `services/inh-public-api-svc/tests/unit/test_principal.py` (create)

**Interfaces:**
- **Produces** `Principal` and `Limits`, consumed by Tasks 4, 5, 8, 9.
- **Produces** `http_transport._current_principal: ContextVar[Principal | None]`, set by the ASGI gate (Task 4) and read by `call_tool`.
- **Consumes** nothing new.

**Design decision — additive, not a rename.** `_current_key_info` stays exactly where it is with exactly its current type. `call_tool` reads `_current_principal` first and, when that is unset, synthesises one via `Principal.from_api_key(_current_key_info.get())`. `tests/contract/test_mcp_http_transport.py`'s `_call_http_tool` helper sets only `_current_key_info`, so this keeps that entire file green **unedited** (Global Constraint 6), and the fallback is genuinely load-bearing rather than dead defensive code.

**Design decision — an OAuth principal is *user-scoped*, not workspace-bound.** Its synthesised `APIKeyInfo` carries `workspace_id=None` and `user_id=<the intg-svc user id from identity resolution>`. Consequences, all deliberate:
- `get_authorized_workspace_ids` stays the **single** source of truth for the authorization set (#138), doing its normal Mongo-ownership lookup for a user-scoped principal. The identity payload's `workspaces` array is *not* a second authorization source — two of those would drift, and the Mongo one is canonical.
- Binding `workspace_id` to the token's default workspace would permanently prevent a multi-workspace user from reaching their other workspaces, contradicting spec §4.4 (`list_workspaces` + an optional `workspace_id` argument is how multi-workspace users are served).
- `default_workspace_id` therefore rides on `Principal` as **advisory** data, used only to disambiguate `upload_document`'s single-workspace requirement. See "Known gaps, deliberate".

- [ ] **Step 1: Write the failing test**

Create `services/inh-public-api-svc/tests/unit/test_principal.py`:

```python
"""Principal — the single shape API-key and OAuth callers both resolve into (#295)."""

from __future__ import annotations

import pytest

from src.models.api_key import APIKeyInfo
from src.services.principal import Limits, Principal

pytestmark = [pytest.mark.unit]


def _key(**kw) -> APIKeyInfo:
    return APIKeyInfo(
        key_id="key-1", user_id="user-1", workspace_id=kw.get("workspace_id"),
        permissions=kw.get("permissions", ["read", "search"]),
        rate_limit=100, status="active",
    )


class TestFromApiKey:
    def test_wraps_key_info_unchanged(self):
        ki = _key(workspace_id="ws-a")
        p = Principal.from_api_key(ki)
        assert p.key_info is ki
        assert p.auth_method == "api_key"
        assert p.subject is None

    def test_api_key_principal_has_no_limits(self):
        """Absent = unlimited (#309). Self-hosted default behaviour is unchanged."""
        p = Principal.from_api_key(_key())
        assert p.limits == Limits()
        assert p.limits.calls_per_minute is None
        assert p.limits.writes_per_day is None
        assert p.limits.calls_per_month is None
        assert p.limits.max_documents is None

    def test_permission_check_delegates_to_key_info(self):
        p = Principal.from_api_key(_key(permissions=["read"]))
        assert p.key_info.has_permission("read") is True
        assert p.key_info.has_permission("write") is False


class TestFromOauth:
    def test_oauth_principal_is_user_scoped(self):
        """workspace_id MUST be None so get_authorized_workspace_ids (#138)
        stays the single authorization source and multi-workspace users are
        not locked to one workspace (spec 4.4)."""
        p = Principal.from_oauth(
            subject="user_2abc", user_id="65f0…", default_workspace_id="ws-a",
            limits=Limits(calls_per_minute=10), upgrade_url="https://app.inherent.sh/billing",
        )
        assert p.auth_method == "oauth"
        assert p.key_info.workspace_id is None
        assert p.key_info.user_id == "65f0…"
        assert p.default_workspace_id == "ws-a"
        assert p.limits.calls_per_minute == 10

    def test_oauth_principal_carries_full_permission_set(self):
        """Authorization is by identity + plan, never by scope (spec 4.5
        SUPERSEDED). There are no scope strings to narrow the permission set
        with, so the principal carries all three and the plan gates the rest."""
        p = Principal.from_oauth(subject="user_2abc", user_id="u1")
        for perm in ("read", "search", "write"):
            assert p.key_info.has_permission(perm)

    def test_key_id_is_stable_and_leaks_no_token(self):
        p = Principal.from_oauth(subject="user_2abc", user_id="u1")
        assert p.key_info.key_id == "oauth:user_2abc"
        assert "eyJ" not in p.key_info.key_id


class TestQuotaIdentity:
    def test_quota_key_differs_per_subject(self):
        a = Principal.from_oauth(subject="user_a", user_id="u1")
        b = Principal.from_oauth(subject="user_b", user_id="u2")
        assert a.quota_identity() != b.quota_identity()

    def test_api_key_quota_key_is_the_key_id(self):
        assert Principal.from_api_key(_key()).quota_identity() == "api_key:key-1"
```

- [ ] **Step 2: Run it and watch it fail**

```bash
cd services/inh-public-api-svc && uv run pytest tests/unit/test_principal.py
```

Expected: `ModuleNotFoundError: No module named 'src.services.principal'`. Not an assertion failure — a missing module. If you see anything else, the test file itself is wrong.

- [ ] **Step 3: Implement `Principal`**

Create `services/inh-public-api-svc/src/services/principal.py`:

```python
"""The single authenticated-caller shape (#295).

Two credentials reach this service: a long-lived API key (Postgres
``api_keys``) and an OAuth 2.1 access token minted by Clerk. Every tool
handler in ``src/mcp_server/server.py`` and every authorization helper in
``src/services/auth.py`` must behave identically for both -- #138's
workspace-scoping rule is the property that must not fork, and a second
authorization code path is exactly how it would.

So both resolve into ONE ``Principal``, which carries an ``APIKeyInfo`` as
its authorization view. Handlers keep their existing signatures; nothing
downstream learns which door the caller came through.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from src.models.api_key import APIKeyInfo

AuthMethod = Literal["api_key", "oauth"]


@dataclass(frozen=True)
class Limits:
    """Per-identity quota ceilings. ``None`` means unlimited (#309).

    Deliberately carries NO plan names and NO tier values: those live in the
    deployment that resolves identity (intg-svc for the hosted stack), never
    here. A self-hosted deployment that resolves identity from a local table
    supplies its own numbers, or none at all -- and none at all must mean
    "unchanged, unlimited behaviour", which is why every field defaults None.
    """

    calls_per_month: int | None = None
    writes_per_day: int | None = None
    calls_per_minute: int | None = None
    max_documents: int | None = None


@dataclass(frozen=True)
class Principal:
    """An authenticated caller, however it authenticated."""

    key_info: APIKeyInfo
    auth_method: AuthMethod
    # OAuth `sub` (Clerk user id). None for API keys. NEVER the raw token.
    subject: str | None = None
    # Advisory only: disambiguates upload_document's single-workspace
    # requirement. It is NOT an authorization binding -- see from_oauth.
    default_workspace_id: str | None = None
    limits: Limits = field(default_factory=Limits)
    upgrade_url: str | None = None
    # Usage counters as of the last identity resolution, for month-scale
    # quotas the engine cannot count locally.
    usage: dict[str, int] = field(default_factory=dict)

    @classmethod
    def from_api_key(cls, key_info: APIKeyInfo) -> "Principal":
        """Wrap a validated API key. No limits: absent = unlimited, so a
        self-hosted stack's behaviour is byte-identical to before #309."""
        return cls(key_info=key_info, auth_method="api_key")

    @classmethod
    def from_oauth(
        cls,
        *,
        subject: str,
        user_id: str,
        default_workspace_id: str | None = None,
        limits: Limits | None = None,
        upgrade_url: str | None = None,
        usage: dict[str, int] | None = None,
    ) -> "Principal":
        """Build a principal from a validated token + a resolved identity.

        ``workspace_id=None`` (user-scoped) is load-bearing, not an omission:

        - ``get_authorized_workspace_ids`` (src/services/auth.py) stays the
          SINGLE source of truth for the authorization set (#138). The
          identity payload's ``workspaces`` array is informational; two
          authorization sources would drift and one of them would be wrong.
        - Binding this to the token's default workspace would permanently
          prevent a multi-workspace user from reaching their other
          workspaces, which spec 4.4 explicitly serves via list_workspaces
          (#297) plus an optional workspace_id argument.

        All three permissions are granted because Clerk supports no custom
        scopes (spec 4.5 SUPERSEDED) -- there is no scope string to narrow
        with. What the caller may actually do is decided by plan and quota
        (#309), not by a permission subset invented here.
        """
        return cls(
            key_info=APIKeyInfo(
                key_id=f"oauth:{subject}",
                user_id=user_id,
                workspace_id=None,
                permissions=["read", "search", "write"],
                status="active",
            ),
            auth_method="oauth",
            subject=subject,
            default_workspace_id=default_workspace_id,
            limits=limits or Limits(),
            upgrade_url=upgrade_url,
            usage=usage or {},
        )

    def quota_identity(self) -> str:
        """Stable bucket key for rate/quota accounting (#309).

        Namespaced by auth method so an API key and an OAuth session for the
        same human never share a bucket -- they have different limit sets.
        """
        if self.auth_method == "oauth":
            return f"oauth:{self.subject}"
        return f"api_key:{self.key_info.key_id}"
```

- [ ] **Step 4: Run the test — expect green**

```bash
cd services/inh-public-api-svc && uv run pytest tests/unit/test_principal.py
```

Expected: 8 passed.

- [ ] **Step 5: Add `_current_principal` to the transport, additively**

In `src/mcp_server/http_transport.py`, beside the existing `_current_key_info` (leave that declaration untouched):

```python
# The Principal the ASGI gate resolved, for callers that came through the
# OAuth door (#295). Kept ALONGSIDE _current_key_info rather than replacing
# it: `tests/contract/test_mcp_http_transport.py`'s _call_http_tool helper
# sets _current_key_info directly, and #295 requires every existing test to
# stay green UNEDITED. The fallback in call_tool below is therefore a real
# code path, not defensive padding.
_current_principal: ContextVar[Principal | None] = ContextVar(
    "mcp_http_principal", default=None
)
```

In `call_tool`, replace the opening key-info lookup with:

```python
        principal = _current_principal.get()
        if principal is None:
            key_info = _current_key_info.get()
            if key_info is None:  # pragma: no cover - the ASGI gate always sets one
                logger.error("MCP HTTP call_tool invoked with no authenticated caller")
                return _error_result(
                    "Error: authentication context missing", FAILURE_CLASS_AUTHENTICATION
                )
            principal = Principal.from_api_key(key_info)
        key_info = principal.key_info
```

Everything below that line is unchanged — `key_info` still names the same object it always did, so the permission check and `tool.handler(key_info, arguments)` are byte-identical.

In `mcp_asgi_app`, set both contextvars so the two never disagree:

```python
        principal = Principal.from_api_key(key_info)
        key_token = _current_key_info.set(key_info)
        principal_token = _current_principal.set(principal)
        try:
            await session_manager.handle_request(scope, receive, send)
        finally:
            _current_principal.reset(principal_token)
            _current_key_info.reset(key_token)
```

- [ ] **Step 6: Prove nothing regressed**

```bash
cd services/inh-public-api-svc && uv run pytest
```

Expected: the full offline suite green, **with zero test files edited**. Confirm that explicitly:

```bash
git status --porcelain services/inh-public-api-svc/tests/
```

Expected output: only `?? tests/unit/test_principal.py`. Any `M` on an existing test file means the design broke Global Constraint 6 — revert and fix the source, not the test.

- [ ] **Step 7: Lint, typecheck, commit**

```bash
make lint && make format-check && make type-check
git checkout -b feat/mcp-oauth-resource-server
git add services/inh-public-api-svc/src/services/principal.py \
        services/inh-public-api-svc/src/mcp_server/http_transport.py \
        services/inh-public-api-svc/tests/unit/test_principal.py
git commit -m "refactor(mcp): introduce Principal so auth paths cannot fork (#295)"
```

No CHANGELOG entry: this task changes no behaviour and no surface.

---

## Task 2: RFC 9728 metadata + `WWW-Authenticate: Bearer` challenge, config-gated (#295)

**Files:**
- Create: `services/inh-public-api-svc/src/services/oauth/__init__.py`, `src/services/oauth/metadata.py`, `src/api/well_known.py`
- Modify: `src/config/settings.py`, `src/main.py`, `src/services/auth.py`
- Test: `tests/unit/test_oauth_metadata.py`, `tests/contract/test_mcp_oauth_challenge.py` (create)

**Interfaces:**
- **Consumes:** nothing from Task 1 (this half is independent of the Principal), but is ordered here because Task 3 needs its settings block.
- **Produces:** `settings.oauth_*` and `settings.identity_resolver_*`, consumed by Tasks 3, 4, 8. `build_protected_resource_metadata()` and `build_bearer_challenge()`, consumed by Task 10's compose test.

- [ ] **Step 1: Write the failing metadata test**

Create `services/inh-public-api-svc/tests/unit/test_oauth_metadata.py`. Pin the three things that are easiest to get quietly wrong:

```python
"""RFC 9728 protected-resource metadata + the Bearer challenge (#295)."""

from __future__ import annotations

import pytest

from src.config.settings import Settings
from src.services.oauth.metadata import (
    build_bearer_challenge,
    build_protected_resource_metadata,
    resource_metadata_url,
)

pytestmark = [pytest.mark.unit]


def _on(**over) -> Settings:
    base = dict(
        oauth_enabled=True,
        oauth_issuer="https://clerk.inherent.sh",
        oauth_resource_identifier="https://api.inherent.sh/mcp",
        oauth_authorization_servers=["https://clerk.inherent.sh"],
    )
    base.update(over)
    return Settings(**base)  # type: ignore[arg-type]


class TestMetadataDocument:
    def test_shape_matches_rfc_9728(self):
        doc = build_protected_resource_metadata(_on())
        assert doc == {
            "resource": "https://api.inherent.sh/mcp",
            "authorization_servers": ["https://clerk.inherent.sh"],
            "scopes_supported": [],
            "bearer_methods_supported": ["header"],
        }

    def test_scopes_supported_is_always_empty(self):
        """Clerk supports no custom scopes (spec 4.5 SUPERSEDED). Advertising
        kb:read / kb:search -- as issue #295's original body proposed -- would
        make the client request a scope Clerk rejects with invalid_scope."""
        doc = build_protected_resource_metadata(_on())
        assert doc["scopes_supported"] == []
        assert "kb:read" not in str(doc)
        assert "kb:search" not in str(doc)


class TestChallenge:
    def test_bearer_challenge_names_the_metadata_document(self):
        c = build_bearer_challenge(_on())
        assert c.startswith("Bearer ")
        assert (
            'resource_metadata="https://api.inherent.sh/.well-known/oauth-protected-resource"'
            in c
        )

    def test_challenge_carries_no_scope_parameter(self):
        assert "scope=" not in build_bearer_challenge(_on())

    def test_challenge_is_apikey_when_oauth_disabled(self):
        """Byte-identical to today's 401 for a self-hosted stack."""
        assert build_bearer_challenge(Settings(oauth_enabled=False)) == "ApiKey"  # type: ignore[arg-type]


class TestFailClosedConfig:
    @pytest.mark.parametrize("missing", ["oauth_resource_identifier", "oauth_authorization_servers"])
    def test_enabled_without_required_config_raises(self, missing):
        """A half-configured stack must refuse to serve a metadata document
        rather than publish an empty or partial one that clients would cache."""
        over = {missing: "" if missing.endswith("identifier") else []}
        with pytest.raises(ValueError):
            build_protected_resource_metadata(_on(**over))


def test_resource_metadata_url_is_derived_from_the_resource_identifier():
    assert (
        resource_metadata_url(_on())
        == "https://api.inherent.sh/.well-known/oauth-protected-resource"
    )
```

- [ ] **Step 2: Run it and watch it fail**

```bash
cd services/inh-public-api-svc && uv run pytest tests/unit/test_oauth_metadata.py
```

Expected: `ModuleNotFoundError` for `src.services.oauth.metadata` — plus, once you stub the module, `ValidationError`/`TypeError` on the unknown `oauth_*` Settings fields. Both are the right kind of failure.

- [ ] **Step 3: Add the settings block**

In `src/config/settings.py`, after the `api_key_header_name` line:

```python
    # ---------------------------------------------------------------- #
    # OAuth 2.1 resource server (#295). OFF BY DEFAULT, deliberately.
    #
    # With oauth_enabled=False this service behaves exactly as it did
    # before #295: /mcp answers an unauthenticated request with
    # `WWW-Authenticate: ApiKey`, and
    # GET /.well-known/oauth-protected-resource 404s. A self-hosted stack
    # must never advertise an authorization server it does not run.
    #
    # API keys are NOT deprecated by any of this. X-API-Key and
    # `Authorization: Bearer ink_...` remain first-class on REST and /mcp
    # permanently, for CI and self-hosters.
    # ---------------------------------------------------------------- #
    oauth_enabled: bool = Field(default=False, alias="OAUTH_ENABLED")
    oauth_issuer: str = Field(
        default="",
        alias="OAUTH_ISSUER",
        description="Expected `iss` claim, e.g. https://clerk.inherent.sh",
    )
    oauth_jwks_uri: str = Field(default="", alias="OAUTH_JWKS_URI")
    oauth_resource_identifier: str = Field(
        default="",
        alias="OAUTH_RESOURCE_IDENTIFIER",
        description="This resource server's identity, e.g. https://api.inherent.sh/mcp",
    )
    oauth_authorization_servers: list[str] = Field(
        default=[], alias="OAUTH_AUTHORIZATION_SERVERS"
    )
    # See docs/reference/configuration.md and the plan's Global Constraint 2
    # for why this is a knob and not a constant: Clerk advertises no RFC 8707
    # resource-indicator support, so `aud` may not name us. Every mode fails
    # CLOSED when its own required config is absent.
    oauth_audience_mode: Literal[
        "resource_indicator", "client_id_allowlist", "introspection"
    ] = Field(default="resource_indicator", alias="OAUTH_AUDIENCE_MODE")
    oauth_allowed_client_ids: list[str] = Field(default=[], alias="OAUTH_ALLOWED_CLIENT_IDS")
    oauth_introspection_endpoint: str = Field(default="", alias="OAUTH_INTROSPECTION_ENDPOINT")
    oauth_jwks_cache_seconds: int = Field(default=600, ge=1, alias="OAUTH_JWKS_CACHE_SECONDS")
    oauth_clock_skew_seconds: int = Field(default=60, ge=0, alias="OAUTH_CLOCK_SKEW_SECONDS")
    oauth_upgrade_url: str = Field(default="", alias="OAUTH_UPGRADE_URL")

    # Identity resolution (#295 / spec 4.3). In-VPC only; never proxied.
    identity_resolver_url: str = Field(default="", alias="IDENTITY_RESOLVER_URL")
    identity_resolver_secret: str = Field(default="", alias="IDENTITY_RESOLVER_SECRET")
    identity_resolver_timeout_seconds: float = Field(
        default=3.0, gt=0, alias="IDENTITY_RESOLVER_TIMEOUT_SECONDS"
    )
    identity_cache_ttl_seconds: int = Field(default=60, ge=0, alias="IDENTITY_CACHE_TTL_SECONDS")
```

- [ ] **Step 4: Implement `metadata.py`**

```python
"""RFC 9728 protected-resource metadata and the 401 challenge (#295)."""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

from src.config.settings import Settings

_WELL_KNOWN_PATH = "/.well-known/oauth-protected-resource"


def resource_metadata_url(settings: Settings) -> str:
    """The absolute URL of this resource's RFC 9728 document.

    Derived from oauth_resource_identifier rather than configured separately,
    so the two can never disagree -- a client that follows the challenge and
    lands on a document naming a DIFFERENT `resource` is required to abort.
    """
    parts = urlsplit(settings.oauth_resource_identifier)
    return urlunsplit((parts.scheme, parts.netloc, _WELL_KNOWN_PATH, "", ""))


def build_protected_resource_metadata(settings: Settings) -> dict:
    """The RFC 9728 document served at GET /.well-known/oauth-protected-resource.

    `scopes_supported` is ALWAYS []. Clerk supports no custom scopes (spec
    4.5 SUPERSEDED, verified 2026-08-20): its scope set is fixed and contains
    no kb:read/kb:search and no way to add one. Advertising a scope here
    makes conformant clients request it and collect an `invalid_scope` from
    Clerk -- a broken connect flow, caused by advertising a capability we do
    not have. Authorization is by identity plus plan (#309), not by scope.
    """
    if not settings.oauth_resource_identifier:
        raise ValueError("oauth_resource_identifier is required when oauth_enabled is true")
    if not settings.oauth_authorization_servers:
        raise ValueError("oauth_authorization_servers is required when oauth_enabled is true")
    return {
        "resource": settings.oauth_resource_identifier,
        "authorization_servers": list(settings.oauth_authorization_servers),
        "scopes_supported": [],
        "bearer_methods_supported": ["header"],
    }


def build_bearer_challenge(settings: Settings) -> str:
    """The `WWW-Authenticate` value for an unauthenticated request.

    With OAuth off this returns the literal "ApiKey" this service has always
    returned -- byte-identical, so a self-hosted stack sees no change (#295
    acceptance criterion 1).

    No `scope` parameter, deliberately, for the same reason
    `scopes_supported` is empty above. Issue #295's original body proposed
    `scope="kb:read kb:search"`; that was written before the 2026-08-20
    verification against the live Clerk instance and is superseded.
    """
    if not settings.oauth_enabled:
        return "ApiKey"
    return f'Bearer resource_metadata="{resource_metadata_url(settings)}"'
```

- [ ] **Step 5: Serve the document**

Create `src/api/well_known.py` — a plain `APIRouter` mounted at root, next to `health_router`:

```python
"""RFC 9728 discovery endpoint (#295).

Mounted at ROOT, not under /v1: RFC 9728 fixes the path at
/.well-known/oauth-protected-resource and MCP clients probe it literally.
"""

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import JSONResponse

from src.config import settings
from src.services.oauth.metadata import build_protected_resource_metadata

router = APIRouter(tags=["oauth"])


@router.get("/.well-known/oauth-protected-resource", include_in_schema=False)
async def protected_resource_metadata() -> JSONResponse:
    # 404, not 501/503: a self-hosted stack with OAuth off must be
    # indistinguishable from one that never had the feature, so a probing
    # client falls straight through to API-key auth (#295 criterion 1).
    if not settings.oauth_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")
    return JSONResponse(
        content=build_protected_resource_metadata(settings),
        # Cacheable: the document is static per deployment, and MCP clients
        # fetch it on every reconnect.
        headers={"Cache-Control": "public, max-age=3600"},
    )
```

In `src/main.py`, beside the existing health include:

```python
    app.include_router(well_known_router.router)
```

Add `/.well-known/oauth-protected-resource` to `EXEMPT_PATHS` in `src/middleware/authentication.py` — discovery is by definition unauthenticated, and resolving a key for it is pure waste.

- [ ] **Step 6: Emit the challenge from the 401 path**

In `src/services/auth.py`, replace the three hardcoded `headers={"WWW-Authenticate": "ApiKey"}` literals (lines ~44, ~51, ~102) with a call to the shared builder:

```python
from src.config import settings as _settings
from src.services.oauth.metadata import build_bearer_challenge

def _challenge_headers() -> dict[str, str]:
    """One place the WWW-Authenticate value is decided, so REST and /mcp
    cannot advertise different authorization servers (#295)."""
    return {"WWW-Authenticate": build_bearer_challenge(_settings)}
```

`src/middleware/error_handler.py:231-234` already copies `exc.headers` onto the RFC 7807 response, so no change is needed there — verify that with a test rather than by reading, in the next step.

- [ ] **Step 7: Write the failing contract test**

Create `services/inh-public-api-svc/tests/contract/test_mcp_oauth_challenge.py`. Model it on the existing `_app_client()` / `TestTestClient` pattern in `tests/contract/test_mcp_http_transport.py` (which runs ASGI lifespan — required for the `/mcp` session manager):

```python
pytestmark = [pytest.mark.contract]

class TestOauthDisabledIsUnchanged:
    def test_well_known_404s(self, client): ...
    def test_mcp_401_challenge_is_apikey(self, client):
        r = client.post("/mcp", headers=_HTTP_MCP_HEADERS, json=_TOOLS_LIST)
        assert r.status_code == 401
        assert r.headers["www-authenticate"] == "ApiKey"

class TestOauthEnabled:
    def test_well_known_serves_rfc_9728(self, oauth_client): ...
    def test_well_known_needs_no_credential(self, oauth_client): ...
    def test_mcp_401_challenge_is_bearer_with_resource_metadata(self, oauth_client):
        r = oauth_client.post("/mcp", headers=_HTTP_MCP_HEADERS, json=_TOOLS_LIST)
        assert r.status_code == 401
        wa = r.headers["www-authenticate"]
        assert wa.startswith("Bearer ")
        assert "resource_metadata=" in wa
        assert "scope=" not in wa
    def test_challenge_url_resolves_to_the_served_document(self, oauth_client):
        """Follow the challenge exactly as a client would, and assert the
        document's `resource` matches. A mismatch here is the single most
        common RFC 9728 misconfiguration and is invisible in unit tests."""
    def test_api_key_still_works_with_oauth_enabled(self, oauth_client): ...
    def test_401_body_names_no_secret(self, oauth_client): ...
```

Toggle settings via a fixture that patches `src.config.settings` attributes and clears `get_settings.cache_clear()`, then rebuilds the app — `settings` is a module-level singleton behind `@lru_cache`, so a plain `monkeypatch.setenv` after import will not take.

- [ ] **Step 8: Run, implement to green, then sweep**

```bash
cd services/inh-public-api-svc && uv run pytest tests/unit/test_oauth_metadata.py tests/contract/test_mcp_oauth_challenge.py
cd services/inh-public-api-svc && uv run pytest
make lint && make format-check && make type-check && make security-check
```

- [ ] **Step 9: Docs + CHANGELOG + commit**

Add an **OAuth resource server** section to `docs/reference/configuration.md` documenting every new setting, that the default is off, and the audience-mode table from Global Constraint 2 verbatim. Add a "Both auth modes" section to `docs/reference/mcp-tools.md`. CHANGELOG under `### Added`:

> - **`/mcp` can now advertise an OAuth authorization server (#295).** With `OAUTH_ENABLED=true` an unauthenticated `POST /mcp` answers `401` with a `WWW-Authenticate: Bearer` challenge naming an RFC 9728 protected-resource document at `/.well-known/oauth-protected-resource`, which MCP clients follow to run a browser sign-in. Off by default: with the flag unset the 401 challenge is the unchanged `ApiKey` and the well-known document 404s, so a self-hosted stack never advertises an authorization server it does not run. `scopes_supported` is deliberately empty — the authorization server (Clerk) supports no custom scopes, so authorization is by identity and plan, not by scope strings.

---

## Task 3: JWT validation — JWKS, `iss`/`exp`, and the audience ladder (#295)

The single riskiest task in this plan. It is also the one that must never be loosened under schedule pressure: a wrong answer here is a cross-tenant breach.

**Files:**
- Create: `src/services/oauth/jwks.py`, `src/services/oauth/token_validator.py`, `tests/oauth_helpers.py`
- Modify: `pyproject.toml`, `src/services/metrics.py`
- Test: `tests/unit/test_oauth_jwks.py`, `tests/unit/test_oauth_token_validator.py`, `tests/security/test_oauth_token_boundaries.py` (create)

**Interfaces:**
- **Consumes:** `settings.oauth_*` from Task 2.
- **Produces:** `async def validate_access_token(raw: str) -> TokenClaims` and `class TokenRejected(Exception)` with a closed-set `reason`, consumed by Task 4.
- **Produces:** `tests/oauth_helpers.py` — `rsa_keypair()`, `jwks_for(public_key)`, `mint_token(**claims)` — consumed by Tasks 4, 8, 10.

- [ ] **Step 1: Build the token factory FIRST — this is what makes Global Constraint 3 structural**

DCR is off on the live Clerk instance, so no real token can be obtained. Every test signs its own.

```bash
cd services/inh-public-api-svc && uv add 'pyjwt[crypto]>=2.9.0'
```

Create `services/inh-public-api-svc/tests/oauth_helpers.py`:

```python
"""Locally-signed OAuth tokens + a synthetic JWKS (#295).

Dynamic Client Registration is OFF on the live Clerk instance
(`registration_endpoint` absent from discovery, verified 2026-08-20), so no
MCP client can register and no real Clerk access token can be obtained.
Every OAuth test in this repo therefore mints its own RS256 token against a
keypair generated here and serves the matching JWKS through an injected
fetcher. Nothing in the test suite reaches Clerk, and nothing needs to.
"""

from __future__ import annotations

import base64
import time
import uuid

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

TEST_KID = "test-key-1"


def rsa_keypair() -> tuple[rsa.RSAPrivateKey, rsa.RSAPublicKey]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key, key.public_key()


def _b64u(n: int) -> str:
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def jwks_for(public_key: rsa.RSAPublicKey, *, kid: str = TEST_KID) -> dict:
    numbers = public_key.public_numbers()
    return {
        "keys": [
            {
                "kty": "RSA", "use": "sig", "alg": "RS256", "kid": kid,
                "n": _b64u(numbers.n), "e": _b64u(numbers.e),
            }
        ]
    }


def mint_token(
    private_key: rsa.RSAPrivateKey,
    *,
    kid: str = TEST_KID,
    issuer: str = "https://clerk.inherent.sh",
    subject: str = "user_2test",
    audience=None,
    expires_in: int = 3600,
    algorithm: str = "RS256",
    **extra,
) -> str:
    now = int(time.time())
    claims = {
        "iss": issuer, "sub": subject, "iat": now, "nbf": now,
        "exp": now + expires_in, "jti": uuid.uuid4().hex,
    }
    if audience is not None:
        claims["aud"] = audience
    claims.update(extra)
    return jwt.encode(claims, private_key, algorithm=algorithm, headers={"kid": kid})
```

- [ ] **Step 2: Write the failing JWKS test**

Create `tests/unit/test_oauth_jwks.py`. Cover, one test each:
1. resolves a signing key by `kid`;
2. an unknown `kid` triggers exactly **one** refetch, then raises (no unbounded refetch loop — that is a self-inflicted DoS against the authorization server);
3. the document is cached for `oauth_jwks_cache_seconds` and not refetched inside the window;
4. a fetch failure with a **warm** cache serves the cached keys (availability) but with a **cold** cache raises (fail closed);
5. the fetcher is injectable, so no test performs real network I/O.

- [ ] **Step 3: Run it and watch it fail; implement `jwks.py`**

```bash
cd services/inh-public-api-svc && uv run pytest tests/unit/test_oauth_jwks.py
```

Implement a module-level singleton with an `httpx.AsyncClient`, an injectable `_fetch` seam (`set_jwks_fetcher()` for tests), a monotonic-clock TTL, and a `_last_refetch_at` guard so an unknown `kid` cannot refetch more than once per (say) 10 s.

- [ ] **Step 4: Write the failing validator test — one spec class per audience mode**

Create `tests/unit/test_oauth_token_validator.py`:

```python
class TestSignatureAndAlgorithm:
    async def test_valid_token_returns_claims(self): ...
    async def test_token_signed_by_a_foreign_key_is_rejected(self): ...
    async def test_alg_none_is_rejected(self):
        """The classic JWT bypass. Algorithms are allow-listed to RS256, so a
        token whose header says alg=none never reaches signature checking."""
    async def test_hs256_token_signed_with_the_public_key_is_rejected(self):
        """The algorithm-confusion attack: never let a caller choose the
        family. Also allow-listed away."""

class TestIssuerAndExpiry:
    async def test_wrong_issuer_is_rejected(self): ...
    async def test_expired_token_is_rejected_with_reason_expired(self):
        """#295: an expired token returns 401, NEVER 403 -- clients key their
        silent-refresh path on 401."""
    async def test_not_before_in_the_future_is_rejected(self): ...
    async def test_clock_skew_tolerance_is_honoured(self): ...

class TestAudienceResourceIndicatorMode:
    async def test_aud_containing_the_resource_identifier_is_accepted(self): ...
    async def test_aud_naming_a_different_resource_is_rejected(self):
        """MCP spec: a server MUST only accept tokens issued for it. A token
        valid for any other Clerk-protected resource must be refused."""
    async def test_missing_aud_is_rejected(self):
        """Clerk advertises no RFC 8707 support, so this is the LIKELY real
        shape. It must fail closed here and be handled by switching mode
        deliberately -- never by relaxing this check."""
    async def test_resource_claim_is_accepted_as_an_alternative_to_aud(self): ...

class TestAudienceClientIdAllowlistMode:
    async def test_allowlisted_azp_is_accepted(self): ...
    async def test_unlisted_azp_is_rejected(self): ...
    async def test_empty_allowlist_rejects_everything(self):
        """Fail closed: an unconfigured allowlist must never mean 'allow'."""
    async def test_token_with_no_azp_or_client_id_is_rejected(self): ...

class TestAudienceIntrospectionMode:
    async def test_active_true_plus_allowlisted_client_is_accepted(self): ...
    async def test_active_false_is_rejected(self): ...
    async def test_introspection_transport_failure_is_rejected(self):
        """Fail closed, not open, when the authorization server is down."""
    async def test_introspection_never_logs_the_raw_token(self, caplog): ...

class TestRejectionHygiene:
    async def test_rejection_reasons_come_from_a_closed_set(self): ...
    async def test_rejection_message_contains_no_token_material(self): ...
```

- [ ] **Step 5: Run it and watch it fail; implement `token_validator.py`**

Key implementation points, all of them load-bearing:

```python
_ALLOWED_ALGORITHMS = ["RS256"]          # allow-list, never read the header's alg

class TokenRejected(Exception):
    """Reason is drawn from a CLOSED set so it is safe as a metric label and
    as a structured log field. It is NEVER put in the 401 response body --
    the body says only "Invalid or expired token"."""
    REASONS = frozenset({
        "malformed", "unknown_kid", "bad_signature", "expired", "not_yet_valid",
        "wrong_issuer", "wrong_audience", "unlisted_client", "introspection_failed",
        "misconfigured",
    })
```

`jwt.decode(..., algorithms=_ALLOWED_ALGORITHMS, issuer=settings.oauth_issuer, options={"verify_aud": False}, leeway=settings.oauth_clock_skew_seconds)` — audience verification is done by us afterwards, because PyJWT's built-in check cannot express the three-mode ladder. Record `metrics.record_oauth_token_rejected(reason=...)` on every rejection path.

- [ ] **Step 6: Write the security-boundary spec**

Create `tests/security/test_oauth_token_boundaries.py` (`pytestmark = [pytest.mark.security]`). These are the tests a reviewer reads first:

```python
async def test_token_for_a_different_resource_cannot_call_any_tool(): ...
async def test_token_from_a_different_issuer_cannot_call_any_tool(): ...
async def test_expired_token_gets_401_not_403(): ...
async def test_no_rejection_path_leaks_the_raw_token_into_logs(caplog): ...
async def test_disabling_oauth_makes_a_valid_jwt_worthless(self):
    """With the flag off, a perfectly valid Clerk token is just an invalid
    API key -- 401, exactly as today. Proves the gate is a real gate."""
```

- [ ] **Step 7: Green, sweep, docs, commit**

```bash
cd services/inh-public-api-svc && uv run pytest tests/unit/test_oauth_jwks.py tests/unit/test_oauth_token_validator.py tests/security/test_oauth_token_boundaries.py
cd services/inh-public-api-svc && uv run pytest
make lint && make format-check && make type-check && make security-check
```

Record the audience decision in `docs/reference/configuration.md` **and** as a comment in #295, including the mode shipped, why, and what evidence would justify changing it. CHANGELOG under `### Security`.

---

## Task 4: Identity resolution + credential-shape dispatch, fail-closed (#295)

Where the OAuth door actually opens. Everything before this was scaffolding.

**Files:**
- Create: `src/services/oauth/identity_client.py`, `tests/unit/test_identity_client.py`
- Modify: `src/mcp_server/http_transport.py`, `src/services/metrics.py`, `tests/contract/test_mcp_oauth_challenge.py`, `tests/security/test_oauth_token_boundaries.py`

**Interfaces:**
- **Consumes:** `Principal.from_oauth` (Task 1), `validate_access_token` (Task 3), `settings.identity_resolver_*` (Task 2).
- **Consumes** the prime repo's contract, matched **exactly** (sibling plan Task 3, built):

  ```
  POST {identity_resolver_url}/internal/identity/resolve
  Header: X-Service-Secret: <identity_resolver_secret>
  Body:   { "clerk_user_id": "user_…", "email": "a@example.com" }   # email OPTIONAL but load-bearing
  200 →   { "user_id", "default_workspace_id", "workspaces": [{id,name}],
            "plan", "limits": { api_calls_per_month, memory_writes_per_day,
                                mcp_calls_per_minute, max_documents },
            "usage": { api_calls_this_month, memory_writes_today } }
  401 →   wrong/absent secret        404 →   unknown subject
  ```

  Two details from the sibling plan that must be honoured:
  - **Pass `email` whenever the token carries an email claim.** The resolver's three-step lookup (`clerk_id` → `login_metadata.clerk_id` → `email`) is the only path that finds users created before `clerk_id` was persisted — which the sibling plan states is *every existing customer*. Omitting it silently 404s the entire existing user base.
  - **`-1` means unlimited** in every prime-side limit field. Map `-1 → None` on the way into `Limits`; do not propagate `-1` into an arithmetic comparison.
- **Produces** a `Principal` on `_current_principal`, consumed by Tasks 5, 8, 9.

- [ ] **Step 1: Write the failing identity-client test**

Create `tests/unit/test_identity_client.py`, driving an injected `httpx.MockTransport`:

```python
class TestContract:
    async def test_posts_clerk_user_id_and_email_with_the_service_secret(self): ...
    async def test_omits_email_when_the_token_carries_no_email_claim(self): ...
    async def test_maps_minus_one_limits_to_none(self):
        """`-1` is the prime repo's unlimited sentinel. Leaking it into Limits
        would make `usage >= limit` compare against -1 and deny everything."""
    async def test_maps_prime_field_names_onto_engine_limit_names(self):
        """api_calls_per_month->calls_per_month, memory_writes_per_day->
        writes_per_day, mcp_calls_per_minute->calls_per_minute. The engine
        holds no plan names or tier values (#309)."""

class TestFailClosed:
    async def test_404_returns_none(self): ...
    async def test_401_returns_none_and_is_never_treated_as_success(self): ...
    async def test_timeout_with_a_cold_cache_returns_none(self): ...
    async def test_5xx_with_a_cold_cache_returns_none(self): ...
    async def test_unset_resolver_url_returns_none_without_a_network_call(self): ...

class TestCache:
    async def test_second_call_inside_the_ttl_makes_no_http_request(self): ...
    async def test_entry_expires_after_identity_cache_ttl_seconds(self): ...
    async def test_warm_cache_survives_a_resolver_outage_until_ttl(self):
        """Spec 4.3: cached identities keep being served during an intg-svc
        outage; uncached ones fail closed. Both halves are tested."""
    async def test_cache_is_keyed_by_subject_so_two_users_never_share_an_entry(self): ...

class TestSecrets:
    async def test_the_service_secret_never_appears_in_logs(self, caplog): ...
```

- [ ] **Step 2: Run, watch it fail, implement `identity_client.py`**

```bash
cd services/inh-public-api-svc && uv run pytest tests/unit/test_identity_client.py
```

Sketch of the mapping — write this exactly, it is the seam most likely to be got wrong:

```python
def _limit(value) -> int | None:
    """`-1` is the prime repo's unlimited sentinel; None is ours (#309)."""
    if value is None or value == -1:
        return None
    return int(value)


def _to_limits(payload: dict) -> Limits:
    raw = payload.get("limits") or {}
    return Limits(
        calls_per_month=_limit(raw.get("api_calls_per_month")),
        writes_per_day=_limit(raw.get("memory_writes_per_day")),
        calls_per_minute=_limit(raw.get("mcp_calls_per_minute")),
        max_documents=_limit(raw.get("max_documents")),
    )
```

Cache: an in-process dict keyed by `sub`, storing `(resolved_at_monotonic, payload)`, TTL `settings.identity_cache_ttl_seconds`. On a resolver failure, serve an entry that is still inside its TTL; otherwise return `None`. Record `metrics.record_identity_resolution_failed(reason=...)` on every failure — a degraded resolver that silently fails closed looks exactly like "no users are connecting", which is the kind of outage that lasts a week.

- [ ] **Step 3: Wire credential-shape dispatch into the ASGI gate**

In `mcp_asgi_app`, before the existing `get_api_key_info` call:

```python
        raw = _bearer_value(request)  # None, or the value after "Bearer "
        if settings.oauth_enabled and _looks_like_jwt(raw):
            principal = await _resolve_oauth_principal(raw)   # raises 401 on any failure
        else:
            # Unchanged path. X-API-Key, `Bearer ink_...`, and anything not
            # JWT-shaped all land here exactly as they did before #295.
            key_info = await get_api_key_info(
                x_api_key=request.headers.get("x-api-key"),
                authorization=request.headers.get("authorization"),
            )
            principal = Principal.from_api_key(key_info)
```

```python
def _looks_like_jwt(value: str | None) -> bool:
    """Shape test only -- never a trust decision.

    `X-API-Key` is never routed here (an API key is an API key regardless of
    what it looks like), and a `Bearer ink_...` value is excluded explicitly
    so an issued key can never be misrouted into the OAuth path. Everything
    that is neither falls through to the API-key path, which is what keeps
    OAUTH_ENABLED=false byte-identical to today.
    """
    return bool(value) and not value.startswith("ink_") and value.count(".") == 2
```

`_resolve_oauth_principal` fails closed at every step, and its 401 carries the Bearer challenge from Task 2:

```python
async def _resolve_oauth_principal(raw: str) -> Principal:
    try:
        claims = await validate_access_token(raw)
    except TokenRejected as rejected:
        # Reason to the log and the metric; NEVER to the response body.
        logger.warning("oauth_token_rejected", reason=rejected.reason)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": build_bearer_challenge(settings)},
        ) from None

    identity = await resolve_identity(claims.subject, email=claims.email)
    if identity is None:
        # FAIL CLOSED (spec 9.4). Never a default plan, never a default
        # workspace: falling open here is a cross-tenant breach, and the
        # cost of failing closed is one user seeing "please try again".
        logger.warning("identity_unresolved", subject=claims.subject)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": build_bearer_challenge(settings)},
        )

    return Principal.from_oauth(
        subject=claims.subject,
        user_id=identity["user_id"],
        default_workspace_id=identity.get("default_workspace_id"),
        limits=_to_limits(identity),
        upgrade_url=settings.oauth_upgrade_url or None,
        usage=identity.get("usage") or {},
    )
```

Then set **both** contextvars as Task 1 established.

- [ ] **Step 4: Extend the contract and security specs**

Add to `tests/contract/test_mcp_oauth_challenge.py`:
- a valid JWT + a stubbed resolver drives a full `initialize` → `tools/list` → `tools/call` round trip over the real mounted app;
- `X-API-Key` still works on the same app with OAuth enabled;
- `Bearer ink_…` still routes to the API-key path even when OAuth is enabled.

Add to `tests/security/test_oauth_token_boundaries.py`:
- a valid token whose subject the resolver 404s gets **401**, and no tool runs;
- resolver timeout with a cold cache gets **401**, and no tool runs;
- two subjects resolving to different `user_id`s cannot see each other's workspaces — assert against `get_authorized_workspace_ids`, not against the identity payload, since that is the boundary that actually holds.

- [ ] **Step 5: Green, sweep, docs, commit**

```bash
cd services/inh-public-api-svc && uv run pytest
make lint && make format-check && make type-check && make security-check
git status --porcelain services/inh-public-api-svc/tests/   # still no M on the pre-existing files
```

CHANGELOG under `### Added`. Document in `docs/reference/mcp-tools.md` that with OAuth enabled a browser sign-in replaces the API key, that keys still work, and that identity resolution fails closed.

---

## Task 5: `list_workspaces` (#297)

Independent of OAuth in mechanism, but placed here because it is the tool that makes a multi-workspace OAuth user usable (spec §4.4) and it should be written once, against the Principal.

**Files:**
- Modify: `src/mcp_server/server.py`, `src/services/database.py`, `docs/reference/mcp-tools.md`
- Test: `tests/contract/test_list_workspaces.py` (create)

**Interfaces:**
- **Consumes:** `get_authorized_workspace_ids` (unchanged), `Principal` (Task 1).
- **Produces:** an 11th HTTP tool. Update the tool-count tables in `docs/reference/mcp-tools.md` ("10, not 14" → "11, not 15") and the `HTTP_EXPOSED_TOOLS` set in `tests/contract/test_mcp_http_transport.py`. **This is the one pre-existing test file this plan edits**, and only by adding a name to a set — it is a registry-drift guard doing exactly its job, not a weakened assertion. Note it in the PR description.

- [ ] **Step 1: Write the failing contract test**

Create `tests/contract/test_list_workspaces.py`:

```python
class TestAdvertisement:
    async def test_is_advertised_on_http(self): ...
    async def test_is_advertised_on_stdio_with_api_key_in_schema(self): ...
    async def test_http_schema_strips_api_key(self): ...
    async def test_requires_read_permission(self):
        """A key without 'read' is denied BEFORE the handler runs."""

class TestScoping:
    async def test_workspace_scoped_key_sees_exactly_one_entry(self):
        """#138 invariant: the scoped key's OWN workspace, never the owner's
        full set. This is the acceptance criterion the issue calls out."""
    async def test_user_scoped_key_sees_every_owned_workspace(self): ...
    async def test_is_scoped_binding_is_true_only_for_a_scoped_key(self): ...
    async def test_returns_an_empty_list_not_an_error_when_none_are_authorized(self): ...

class TestPayload:
    async def test_each_entry_carries_id_name_and_document_count(self): ...
    async def test_name_is_null_when_mongo_is_unavailable(self):
        """Names are a nicety from the control plane; ids and counts come
        from stores this service owns. A Mongo blip degrades the label, it
        does not fail the call -- and it records the existing
        workspace_ownership_lookup_degraded_total metric so the degradation
        is visible."""
```

- [ ] **Step 2: Run it and watch it fail**

```bash
cd services/inh-public-api-svc && uv run pytest tests/contract/test_list_workspaces.py
```

- [ ] **Step 3: Add the two database helpers**

In `src/services/database.py`:
- `get_workspace_document_counts(workspace_ids: list[str]) -> dict[str, int]` — **one** `SELECT workspace_id, COUNT(*) … WHERE workspace_id = ANY(:ids) GROUP BY workspace_id` query. Never N calls to `get_documents`; a user with 20 workspaces would otherwise pay 20 round trips for one tool call.
- `get_workspace_names(workspace_ids: list[str]) -> dict[str, str]` — read-only Mongo lookup on the `workspaces` collection, handling both `str` and `ObjectId` `_id` shapes (mirror the `$or` idiom already in `get_user_workspace_ids`). Swallow failures, return `{}`, and call `record_workspace_ownership_lookup_degraded(source="mongo")`.

- [ ] **Step 4: Implement the handler and the registry entry**

```python
async def _handle_list_workspaces(key_info: APIKeyInfo, arguments: dict) -> list[TextContent]:
    """List the workspaces this caller may act on (#297).

    Backed by get_authorized_workspace_ids -- the SAME #138 rule every other
    tool uses -- so a workspace-scoped key sees exactly its one bound
    workspace and this tool leaks nothing a search would not already reveal
    through `workspaces_searched`.

    `is_scoped_binding` exists so an agent can TELL THE USER why it can only
    see one workspace, instead of silently appearing to have partial data
    and inventing an explanation.
    """
    database = await get_database()
    workspace_ids = await get_authorized_workspace_ids(key_info, database)
    if not workspace_ids:
        return _structured("No workspaces are authorized for this credential.", {"workspaces": []})

    counts = await database.get_workspace_document_counts(workspace_ids)
    names = await database.get_workspace_names(workspace_ids)
    scoped = bool(key_info.workspace_id)

    payload = [
        {
            "workspace_id": ws,
            "name": names.get(ws),
            "document_count": counts.get(ws, 0),
            "is_scoped_binding": scoped,
        }
        for ws in sorted(workspace_ids)
    ]
    summary = "\n".join(
        f"- {row['workspace_id']}"
        + (f" ({row['name']})" if row["name"] else "")
        + f" — {row['document_count']} documents"
        for row in payload
    )
    return _structured(f"{len(payload)} authorized workspace(s):\n{summary}", {"workspaces": payload})
```

Registry entry — the description must say **when** to call it, per spec §5:

```python
    "list_workspaces": ToolDef(
        description="List the workspaces this credential can act on, with each "
        "workspace's id, name and document count. Call this FIRST whenever a task "
        "needs a specific workspace_id — for upload_document or "
        "get_retrieval_health, or to target a search — instead of guessing an id. "
        "Requires 'read' permission.",
        input_schema={
            "type": "object",
            "properties": {"api_key": {"type": "string", "description": "Your Inherent API key"}},
            "required": ["api_key"],
        },
        permission="read",
        handler=_handle_list_workspaces,
    ),
```

- [ ] **Step 5: Green, sweep, docs, commit**

```bash
cd services/inh-public-api-svc && uv run pytest tests/contract/ && uv run pytest
make lint && make format-check && make type-check
```

`docs/reference/mcp-tools.md`: add the tool, bump both tool-count tables, and note that the surface difference is now "11 on HTTP, 15 on stdio". CHANGELOG under `### Added` ending `(#297)`.

---

## Task 6: `event_id` on single-workspace MCP searches + `report_feedback` on HTTP (#296, part 1 of 2)

**#296 is split across Tasks 6 and 7, deliberately.** The issue's preferred **Option A** — one `event_id` shared by one row per workspace queried — is **impossible against the current schema**: `eval_query_events.event_id` is a `PRIMARY KEY` (`015_evals.sql:20`) and `insert_eval_event` writes `ON CONFLICT (event_id) DO NOTHING`, so the second workspace's row is silently dropped. Option A therefore needs a migration, which is its own task with its own blast radius.

Task 6 captures every search that resolves to **exactly one** workspace — which is every workspace-scoped key, and every single-workspace user, i.e. essentially the entire beta cohort. Task 7 adds genuine multi-workspace fan-out capture behind migration 018. State this split in #296 as a comment before starting, so the issue's Option-A acceptance criterion is not read as unmet.

**Files:**
- Modify: `src/mcp_server/server.py`, `docs/reference/mcp-tools.md`, `CHANGELOG.md`, `tests/integration/test_compose_mcp.py`
- Test: `tests/contract/test_mcp_event_capture.py` (create)

**Interfaces:**
- **Consumes:** `new_event_id`, `capture_enabled`, `record_query_event`, `purge_expired_events` from `src/services/eval_capture.py` — all unchanged.
- **Produces:** `event_id` in `search_documents`' structured payload, consumed by `report_feedback` and by Task 7.

- [ ] **Step 1: Write the failing contract test**

Create `tests/contract/test_mcp_event_capture.py`:

```python
class TestSingleWorkspaceCapture:
    async def test_search_with_an_explicit_workspace_id_returns_an_event_id(self): ...
    async def test_fan_out_over_exactly_one_workspace_returns_an_event_id(self):
        """A workspace-scoped key, or a single-workspace user, omits
        workspace_id and still gets capture -- this is the beta majority."""
    async def test_the_recorded_row_carries_the_query_and_result_chunk_ids(self): ...
    async def test_stdio_and_http_behave_identically(self): ...

class TestCaptureIsBestEffort:
    async def test_capture_failure_still_returns_a_successful_search(self): ...
    async def test_no_event_id_is_advertised_when_the_row_is_not_durable(self):
        """#240's contract: a dangling id is worse than no id."""
    async def test_workspace_optout_suppresses_capture_and_the_event_id(self): ...

class TestFanOutIsExplicitlyUncaptured:
    async def test_multi_workspace_fan_out_advertises_no_event_id(self):
        """Deliberate, and pinned so it is a visible gap rather than a silent
        one. Task 7 / migration 018 closes it."""

class TestFeedbackRoundTrip:
    async def test_report_feedback_is_advertised_on_http(self): ...
    async def test_an_event_id_from_an_mcp_search_resolves_in_report_feedback(self): ...
```

- [ ] **Step 2: Run it and watch it fail**

```bash
cd services/inh-public-api-svc && uv run pytest tests/contract/test_mcp_event_capture.py
```

- [ ] **Step 3: Widen `_run_search`'s return, then capture in `_handle_search`**

Change `_run_search`'s signature to `-> tuple[list, list[str], str | None, SearchResponse | None]`, returning the single workspace's `SearchResponse` when `len(workspace_ids) == 1` and `None` otherwise. It is needed because `record_query_event` takes the `SearchRequest`/`SearchResponse` pair, and reconstructing one from the merged tuples would fabricate `search_mode`, `processing_time_ms` and `quality_verdict`. Update the two other callers (`_handle_get_citations`, and `search_memory` via `_handle_search`) to unpack the extra element.

In `_handle_search`, mirroring `src/api/v1/search.py:378` exactly — same await-don't-defer reasoning (#240):

```python
    # Evals v1 capture on the MCP surface (#296). Awaited, not deferred, for
    # the same reason REST awaits it: the response carries the id, and an id
    # the caller cannot resolve on its very next round trip is worse than no
    # id at all (#240). record_query_event never raises, so capture still
    # cannot fail a search.
    #
    # Single-workspace only, for now. eval_query_events.event_id is a PRIMARY
    # KEY (015_evals.sql), so one id cannot span the N rows a fan-out would
    # need; migration 018 (#296 part 2) widens it. A fan-out over exactly one
    # workspace -- every scoped key, every single-workspace user -- IS
    # captured here.
    event_id: str | None = None
    if len(workspace_ids) == 1 and response is not None and capture_enabled(workspace_ids[0]):
        candidate = new_event_id()
        if await record_query_event(
            event_id=candidate,
            workspace_id=workspace_ids[0],
            user_id=key_info.user_id,
            request=request,
            response=response,
        ):
            event_id = candidate
```

Add `event_id` to the structured payload **only when it is non-`None`** — never advertise a key whose value is null, which an agent reads as "capture happened and produced nothing".

- [ ] **Step 4: Expose `report_feedback` on HTTP**

Flip `report_feedback`'s `http_exposed=False` to `True` and replace its exclusion comment with the reason it is now included:

```python
        # HTTP-exposed since #296: search responses now carry an event_id, so
        # the feedback loop this tool exists for is closed on HTTP. It was
        # excluded by #220 only because the issue's "10, not 13" list predated
        # evals v1 -- never because it was unsafe on this transport.
```

Add `"report_feedback"` to `HTTP_EXPOSED_TOOLS` and remove it from `HTTP_EXCLUDED_TOOLS` in `tests/contract/test_mcp_http_transport.py` (the second and last edit to that file — again, a registry guard doing its job). Update its module docstring, which names `report_feedback` among the exclusions.

- [ ] **Step 5: Remove the strict xfail — removed, not relaxed**

In `tests/integration/test_compose_mcp.py`, delete the `@pytest.mark.xfail(..., strict=True, raises=...)` decorator on `test_http_report_feedback_closes_loop` and the bespoke exception class that existed only to scope it (~lines 495–525). Then, against a running stack:

```bash
make test-integration
```

Expected: `test_http_report_feedback_closes_loop` **passes**. It is a `compose` test, so the offline suite will not exercise it — running it is not optional.

- [ ] **Step 6: Sweep, docs, commit**

```bash
cd services/inh-public-api-svc && uv run pytest
make lint && make format-check && make type-check
```

`docs/reference/mcp-tools.md`: document `event_id` on search responses, the single-workspace scope of capture, and `report_feedback`'s move to HTTP. CHANGELOG under `### Fixed`, closing `(#296, #241)` — #241 is the same defect, reopened.

---

## Task 7: Multi-workspace fan-out capture — migration 018 (#296, part 2 of 2)

The half of #296 that needs a schema change. Kept separate because a PK widening on a live table is not something to smuggle into a feature commit.

**Files:**
- Create: `services/inh-ingestion-svc/scripts/migrations/018_eval_events_fanout.sql`
- Modify: `src/services/database.py`, `src/services/eval_capture.py`, `src/mcp_server/server.py`, `src/services/eval_feedback.py`
- Test: extend `tests/contract/test_mcp_event_capture.py`, `tests/unit/test_eval_capture.py`

**Interfaces:**
- **Consumes:** Task 6's capture call site.
- **Produces:** an `event_id` on fan-out searches; `get_eval_event` resolving an id that spans workspaces.

- [ ] **Step 1: Write the migration**

```sql
-- Migration 018: one capture event may span several workspaces (#296).
--
-- A fan-out MCP search (workspace_id omitted) queries every workspace the
-- caller is authorized for and returns ONE merged result list. The agent
-- judged that one list, so one event_id over the whole fan-out is the honest
-- unit of feedback -- but event_id was the PRIMARY KEY, so only the first
-- workspace's row survived the ON CONFLICT DO NOTHING and every other
-- workspace was silently dropped.
--
-- The key widens to (event_id, workspace_id). eval_feedback keeps event_id
-- as its PK on purpose: one verdict per fan-out, not one per workspace.
-- Idempotent: safe to re-run.
ALTER TABLE eval_query_events DROP CONSTRAINT IF EXISTS eval_query_events_pkey;
ALTER TABLE eval_query_events ADD PRIMARY KEY (event_id, workspace_id);
CREATE INDEX IF NOT EXISTS ix_eval_events_event_id ON eval_query_events (event_id);
```

- [ ] **Step 2: Write the failing tests first**

Extend `tests/contract/test_mcp_event_capture.py`:
- a fan-out over 3 workspaces writes 3 rows sharing one `event_id`;
- `report_feedback` on that id resolves and applies to the fan-out as a whole;
- a per-workspace `capture_enabled` opt-out excludes **only** that workspace's row, and the remaining rows still share the id (the issue's explicit acceptance criterion);
- if every workspace in a fan-out has opted out, **no** `event_id` is advertised;
- `get_eval_event` scoped to a caller who owns only one of the three workspaces returns only what they own — no cross-workspace existence oracle.

- [ ] **Step 3: Implement**

- `database.insert_eval_events(*, event_id, rows: list[...])` — one multi-row `INSERT … ON CONFLICT (event_id, workspace_id) DO NOTHING`, one transaction. Keep `insert_eval_event` as a single-row wrapper so nothing else has to change.
- `eval_capture.record_query_events(...)` — the multi-workspace form; returns `True` only when **at least one** row is durable.
- `database.get_eval_event` — still scoped by `workspace_id = ANY(:workspace_ids)`; return the first matching row plus the full `workspace_ids` set the event spans, so `submit_feedback` can promote correctly.
- `eval_feedback.submit_feedback` — decide and **document** whether a fan-out verdict promotes one case per workspace or one for the workspace that produced the top-scoring result. Recommendation: promote for the workspace that produced the top result, since that is the evidence the agent actually judged; write the reasoning into the docstring.

- [ ] **Step 4: Run the migration locally, then the compose lane**

```bash
make down && make up          # applies migrations from scripts/migrations
make test-integration
```

Expected: existing evals tests unchanged and green; the new fan-out cases pass.

- [ ] **Step 5: Sweep, docs, commit**

CHANGELOG under `### Fixed` with an explicit migration note (`018_eval_events_fanout.sql`), because operators need to know a PK changed.

---

## Task 8: Per-identity entitlements and quotas in the dispatcher (#309)

Needs the token-validation spine (Tasks 1–4) to exist: enforcement reads limits **from the resolved identity**, and nothing resolves an identity until Task 4.

**Files:**
- Create: `src/services/entitlements.py`, `tests/unit/test_entitlements.py`
- Modify: `src/mcp_server/http_transport.py`, `src/services/database.py`, `src/services/metrics.py`

**Interfaces:**
- **Consumes:** `Principal.limits` / `Principal.usage` / `Principal.quota_identity()` (Tasks 1, 4); `get_rate_limiter()` from `src/core/rate_limiter.py`; `ToolDef.permission` as the read/write discriminator.
- **Produces:** `async def check_quota(principal, tool_name, tool) -> QuotaDenial | None`, called from `call_tool`.

**Design decisions to hold:**
- **No plan names, no tier values in this repo** (#309, verbatim). `entitlements.py` reads numbers off `Principal.limits` and nothing else. A grep for `"free"`, `"pro"`, `"enterprise"` in `services/inh-public-api-svc/src/` must return nothing.
- **Absent = unlimited.** An API-key principal has `Limits()` with every field `None`, so a self-hosted stack's behaviour is byte-identical to before this task. That is the acceptance criterion "an identity with no limits present is unlimited".
- **`writes_per_day` keys off `tool.permission == "write"`** — the registry is already the single source of truth for which tools write, so no second list is created that could drift.
- **`calls_per_month` is enforced from the resolved identity's `usage`, not counted locally.** The engine has no month-scale counter and inventing one would disagree with billing. The 60 s identity cache is the enforcement lag, and that is acceptable and documented.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_entitlements.py`:

```python
class TestUnlimitedByDefault:
    async def test_principal_with_no_limits_is_never_denied(self): ...
    async def test_api_key_principal_is_never_denied(self): ...

class TestCallsPerMinute:
    async def test_denies_the_n_plus_first_call_in_a_window(self): ...
    async def test_succeeds_again_in_the_next_window(self): ...
    async def test_two_subjects_do_not_share_a_bucket(self): ...

class TestWritesPerDay:
    async def test_a_write_tool_consumes_the_write_budget(self): ...
    async def test_a_read_tool_does_not(self): ...
    async def test_reads_still_succeed_once_writes_are_exhausted(self):
        """#309 acceptance, verbatim."""
    async def test_write_tools_are_identified_from_ToolDef.permission(self): ...

class TestCallsPerMonth:
    async def test_denies_when_resolved_usage_has_reached_the_ceiling(self): ...
    async def test_allows_when_usage_is_below_the_ceiling(self): ...

class TestMaxDocuments:
    async def test_denies_a_write_tool_at_the_document_cap(self): ...
    async def test_does_not_query_the_document_count_for_read_tools(self):
        """Cost discipline: no extra round trip on the hot read path."""

class TestDenialPayload:
    async def test_names_the_limit_the_reset_time_and_the_upgrade_url(self): ...
    async def test_is_branchable_not_prose_only(self):
        """#216's contract: agents branch on structuredContent, not text."""
    async def test_omits_upgrade_url_when_the_operator_configured_none(self): ...
    async def test_carries_no_plan_name(self):
        """The engine holds no tier vocabulary. The upgrade URL is the only
        thing it says about commercial state."""
```

- [ ] **Step 2: Run, watch it fail, implement**

Add to `http_transport.py`:

```python
FAILURE_CLASS_QUOTA_EXCEEDED = "quota_exceeded"
```

Denial result shape:

```python
CallToolResult(
    content=[TextContent(
        type="text",
        text=f"Error: quota exceeded — {denial.limit} ({denial.limit_value}). "
             f"Resets in {denial.reset_in_seconds}s."
             + (f" Raise it at {denial.upgrade_url}" if denial.upgrade_url else ""),
    )],
    structuredContent={
        "error_class": FAILURE_CLASS_QUOTA_EXCEEDED,
        "limit": denial.limit,              # calls_per_minute | writes_per_day | calls_per_month | max_documents
        "limit_value": denial.limit_value,
        "reset_at": denial.reset_at,        # epoch seconds
        "upgrade_url": denial.upgrade_url,  # None when unconfigured
    },
    isError=True,
)
```

In `call_tool`, place the quota check **after** the permission check and **before** `tool.handler` — an unauthorized call must not consume budget, and a denied call must not reach the search or embedding path:

```python
        denial = await check_quota(principal, name, tool)
        if denial is not None:
            metrics.record_mcp_quota_exceeded(limit=denial.limit)
            return _quota_error_result(denial)
```

`calls_per_minute` and `writes_per_day` both use the existing `TokenBucketRateLimiter` (`get_rate_limiter()`) with distinct key prefixes and window sizes (`60` and `86400`) — reuse, not a second limiter, so a Redis-backed deployment gets distributed quotas for free.

- [ ] **Step 3: Prove the self-hosted default is unchanged**

```bash
cd services/inh-public-api-svc && uv run pytest
grep -rniE '"(free|pro|enterprise)"' services/inh-public-api-svc/src/
```

Expected: full suite green, and **no** grep hits. A hit means plan vocabulary leaked into the engine.

- [ ] **Step 4: Sweep, docs, commit**

Document the four limits, the `quota_exceeded` structured payload, and "absent = unlimited" in `docs/reference/mcp-tools.md`. CHANGELOG under `### Added` ending `(#309)`.

---

## Task 9: Async usage metering (#309)

**Files:**
- Create: `src/services/metering.py`, `tests/unit/test_metering.py`
- Modify: `src/mcp_server/http_transport.py`, `src/services/metrics.py`

**Interfaces:**
- **Consumes:** `Principal`, the `_TOOLS` registry, `get_mq_service()`.
- **Produces:** MQ messages on a new topic (`core.usage.mcp.v1`), consumed by the prime repo's metering path (sibling plan #279 — not in this plan).

- [ ] **Step 1: Write the failing test**

```python
class TestFireAndForget:
    async def test_a_publish_failure_does_not_raise(self): ...
    async def test_a_publish_failure_does_not_change_the_tool_result(self): ...
    async def test_a_slow_sink_does_not_delay_the_tool_result(self):
        """#309 acceptance: 'a metering failure must never fail or delay a
        tool call'. Asserted against a sink that sleeps far longer than the
        call, not against wall-clock timing of a fast one."""
    async def test_disabling_the_sink_is_a_no_op(self): ...

class TestPayload:
    async def test_carries_subject_tool_name_and_timestamp(self): ...
    async def test_carries_no_query_text_and_no_token(self):
        """Metering is accounting, not audit. Audit already publishes query
        text on its own topic with its own retention; duplicating it here
        would widen exposure for no gain."""
    async def test_api_key_calls_are_metered_under_the_key_id(self): ...
```

- [ ] **Step 2: Run, watch it fail, implement**

Mirror `src/services/audit_publisher.py`'s established shape: build the event, publish inside a `try/except`, log a warning and increment `metering_publish_failed_total` on failure, never re-raise. Schedule it with `asyncio.create_task` and hold a reference in a module-level set so it is not garbage-collected mid-flight — the classic fire-and-forget bug.

Call it from `call_tool` **after** the result is produced, on both the success and the `isError` paths. A denied call is still usage worth counting.

- [ ] **Step 3: Sweep, docs, commit**

---

## Task 10: Compose-lane discovery handshake + docs sweep (#295)

Closes #295's last acceptance criterion: "contract tests in the compose lane alongside `test_compose_mcp.py` cover the discovery handshake end to end". Everything up to here is offline; this proves the pieces fit against a booted stack.

**Files:**
- Create: `tests/integration/test_compose_mcp_oauth.py`
- Modify: `docs/reference/mcp-tools.md`, `docs/reference/configuration.md`, `CHANGELOG.md`

- [ ] **Step 1: Write the compose test**

`pytestmark = [pytest.mark.compose, pytest.mark.integration]`. It still uses locally-signed tokens (Global Constraint 3) — what "end to end" buys here is the **real** ASGI app, real middleware stack, real nginx-shaped routing, real Postgres/Mongo, not a real Clerk.

```python
class TestDiscoveryHandshake:
    def test_unauthenticated_mcp_401_names_the_metadata_document(self, client): ...
    def test_following_the_challenge_url_yields_a_valid_rfc_9728_document(self, client):
        """The whole point: parse the WWW-Authenticate header, extract
        resource_metadata, GET it, and assert its `resource` matches the
        server that issued the challenge -- exactly what an MCP client does."""
    def test_the_document_lists_an_authorization_server_that_serves_rfc_8414(self, client):
        """Skipped unless a live authorization server is reachable, so the
        lane stays hermetic by default. When it does run it is the only
        check that would catch a typo'd issuer hostname."""
    def test_a_locally_signed_token_completes_initialize_tools_list_tools_call(self, client): ...
    def test_a_token_for_a_foreign_resource_is_refused_by_the_running_stack(self, client): ...
    def test_api_key_auth_still_works_against_the_same_running_stack(self, client): ...
```

- [ ] **Step 2: Run it**

```bash
make up
make test-integration
```

Expected: the new file's tests execute (not skip — `run-compose-suite.sh` checks the JUnit report for zero-executed, #209) and pass.

- [ ] **Step 3: Docs sweep**

Re-read `docs/reference/mcp-tools.md` end to end and reconcile every count and claim: 11 HTTP tools / 15 stdio; `report_feedback` on HTTP; `list_workspaces` present; `event_id` on search responses; both auth modes; the config gate; the `quota_exceeded` error class. Re-read `docs/reference/configuration.md` for the full `oauth_*` / `identity_resolver_*` block including the audience-mode table.

- [ ] **Step 4: Final gate, then open the PR**

```bash
make check          # validate lint format-check type-check security-check test
make test-integration
git status --porcelain services/inh-public-api-svc/tests/
```

Expected on that last command: modifications to **exactly two** pre-existing test files — `test_mcp_http_transport.py` (two set-membership edits, Tasks 5 and 6) and `test_compose_mcp.py` (xfail removal, Task 6). Anything else violates Global Constraint 6.

Open one PR against `main`. Body answers the three AGENTS.md questions — what is happening, why it was required, how it impacts the end customer — then the technical detail, then an explicit statement of the audience-mode decision and the DCR blocker.

---

## Self-review against the spec

| Spec section | Covered by |
|---|---|
| §4.1 credential dispatch (4 shapes) | Task 4 Step 3 (`_looks_like_jwt`); Task 2 (the none-credential challenge) |
| §4.2 connect flow ①–② (401 → RFC 9728) | Task 2 |
| §4.2 connect flow ⑤ (JWKS · iss · exp · aud) | Task 3 |
| §4.2 **CORRECTION** — issuer is `clerk.inherent.sh`, not `auth.inherent.sh` | Task 2 settings; issue #295's body still says `auth.inherent.sh` and is superseded |
| §4.2 open risk — audience check vs. absent RFC 8707 | **Global Constraint 2** + Task 3's three mode-spec classes |
| §4.3 identity resolution (incl. the optional-but-load-bearing `email`) | Task 4 |
| §4.3 60 s cache; fail closed when intg-svc is unreachable | Task 4 Step 2 (`TestCache`, `TestFailClosed`) |
| §4.4 workspace binding; never asked to paste an id | Task 1 (user-scoped principal + advisory `default_workspace_id`); Task 5 (`list_workspaces`) |
| §4.5 **SUPERSEDED** — no custom scopes; authorize on identity + plan | Global Constraint 1; Task 2 (`scopes_supported: []`, no `scope=`); Task 8 |
| §5 tool surface — `search_documents` mints an `event_id` | Tasks 6, 7 |
| §5 tool surface — `list_workspaces` is new | Task 5 |
| §5 — every description says **when** to call the tool | Task 5 Step 4 |
| §7 entitlements + quotas, engine-side, from the cached identity | Task 8 |
| §7 metering is async and never blocks a tool call | Task 9 |
| §10 — unauthenticated `POST /mcp` → 401 + `WWW-Authenticate: Bearer` | Task 2 Step 7; Task 10 Step 1 |
| §10 — a token whose `aud` is not ours is rejected | Task 3 `TestAudienceResourceIndicatorMode`; Task 10 |
| §10 — with `oauth_enabled=false`, `/mcp` is unchanged and serves no document | Task 2 `TestOauthDisabledIsUnchanged`; Task 3's `test_disabling_oauth_makes_a_valid_jwt_worthless` |
| §10 — exceeding a quota returns `isError: true` with the upgrade URL | Task 8 `TestDenialPayload` |
| §10 — contract tests cover the 12-tool schema | Partially: Tasks 5–6 bring HTTP to 11. `remember` (#308) is the 12th and is out of scope here. |
| §2 — Phase 0 prod bump; §6 conversation ingestion; §8 Phase 4 connect UX | **Out of scope** — prime repo / #306–#308, separate plans |

## Known gaps, deliberate

1. **`default_workspace_id` is advisory, not enforced.** An OAuth principal is user-scoped, so `upload_document`'s "required if you have more than one workspace" rule still asks a multi-workspace user to name one. Wiring `Principal.default_workspace_id` into `_resolve_single_workspace_for_upload` is a two-line follow-up, deliberately not taken here: it changes an authorization-adjacent resolution path and deserves its own tests rather than riding along in Task 1.

2. **The audience check may ship in its weaker mode.** Global Constraint 2's ladder exists precisely because the strong mode may prove unusable against Clerk. If it does, `client_id_allowlist` is what runs in production, and that is materially weaker than what the MCP spec asks for. This is a **known, accepted, documented** risk for beta, not an oversight — and the moment Clerk advertises `resource_indicators_supported`, flipping back is one env var with the tests already written.

3. **Fan-out capture lands behind a migration (Task 7).** Between Tasks 6 and 7, a genuinely multi-workspace user's searches carry no `event_id` and cannot be given feedback. Pinned by an explicit test so it is a visible gap rather than a silent one.

4. **`memory_writes_today` arrives as `0` from the resolver** until the prime repo's #279 lands (sibling plan's own stated gap). Task 8's `writes_per_day` therefore enforces against the engine's own token bucket, which resets on process restart. Correct within a process, under-counted across a redeploy. Acceptable at beta volume; the fix is entirely on the prime side.

5. **Nothing here is verifiable in a deployed environment until the Phase 0 engine bump.** A deployment running an engine older than that bump does not serve `/mcp` at all. Every task in this plan is fully testable locally and in the compose lane without it — that is a design goal, not a coincidence — but the epic's acceptance criteria cannot be demonstrated end to end until the prime repo's Task 1 ships.

6. **Dynamic Client Registration is a prerequisite this repo cannot satisfy.** Even with all ten tasks merged, DCR must be enabled on the authorization server before Claude Desktop or Cursor can register and complete a sign-in. That is an operator action outside both repos. Everything on this side is built and proven against synthetic tokens so that the toggle is the *only* remaining step.
