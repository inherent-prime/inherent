"""Per-identity entitlements for the MCP dispatcher (#309).

This module supplies the LIMITS a ``Principal`` (``src/services/auth.py``,
#295) is allowed to consume -- it does not itself enforce anything; see
``src/mcp_server/quotas.py`` for the enforcement/metering logic that reads
these values and checks the caller's current usage against them.

Design (#309 issue, "Design" section)
---------------------------------------
"Enforcement reads limits from the resolved identity ... rather than from
engine config. Deployments that resolve identity from a local table supply
their own values; deployments that resolve it from an external service get
whatever that service returns. **No plan names or tier values belong in this
repo.**"

That is why ``Entitlements`` is a plain bag of optional numbers with no
notion of "free" / "pro" / "enterprise" anywhere in it, and why the lookup is
a ``Protocol`` (``EntitlementsProvider``) rather than a concrete
implementation reading a hardcoded table: a deployment wires in its own
provider (a local Postgres/Redis table, a call to an external billing
service, ...) via ``set_entitlements_provider`` at startup. Nothing in this
repo ships a provider that returns anything but "unlimited".

Default-open (#309 design constraint #1)
------------------------------------------
``NullEntitlementsProvider`` -- the shipped default -- returns an all-``None``
``Entitlements`` for every principal, i.e. unlimited. An API-key principal
with no entitlement record configured therefore behaves EXACTLY as it did
before this issue: nobody loses access by upgrading to a build that includes
this module. See ``tests/unit/test_entitlements.py`` and
``tests/unit/test_quotas.py::test_default_open_principal_incurs_no_quota_io``
for the pinned proof.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from src.services.auth import Principal


@dataclass(frozen=True)
class Entitlements:
    """Per-identity limits (#309). Every field is optional; ``None`` means
    "no limit of this kind" -- NOT "zero calls allowed". This mirrors the
    issue's table exactly:

    - ``calls_per_month``: total tool calls (read + write).
    - ``writes_per_day``: calls to write-annotated tools only (``ToolDef.
      permission == "write"`` -- the same tag REST's per-route dependencies
      and this dispatcher's own permission check already use, #14).
    - ``calls_per_minute``: burst ceiling, every tool call.
    - ``max_documents``: cap on documents a principal may hold, checked only
      against the one tool that can increase that count -- see
      ``quotas._DOCUMENT_INCREASING_TOOLS`` for why ``delete_document`` /
      ``refresh_stale_source`` are deliberately excluded even though they
      also carry ``permission="write"``.
    - ``upgrade_url``: optional operator-configured URL surfaced in a
      rejection so the caller knows how to raise the limit (issue's
      "Acceptance" section). Not a limit itself.
    """

    calls_per_month: int | None = None
    writes_per_day: int | None = None
    calls_per_minute: int | None = None
    max_documents: int | None = None
    upgrade_url: str | None = None

    @property
    def unlimited(self) -> bool:
        """True when none of the four numeric limits are set.

        The single check ``quotas.check_quota`` uses to short-circuit BEFORE
        touching the rate limiter or the database -- the default-open path
        (#309 design constraint #1) must cost an already-unlimited caller
        nothing beyond this one lookup and four ``is None`` comparisons.
        """
        return (
            self.calls_per_month is None
            and self.writes_per_day is None
            and self.calls_per_minute is None
            and self.max_documents is None
        )


@runtime_checkable
class EntitlementsProvider(Protocol):
    """Pluggable source of truth for a principal's ``Entitlements`` (#309).

    Deliberately a ``Protocol``, not an ABC: a deployment's provider (a
    Postgres table, a Redis hash, an HTTP call to an external billing
    service) needs no dependency on this module beyond matching this one
    async method's shape.
    """

    async def get_entitlements(self, principal: "Principal") -> Entitlements:
        """Return ``principal``'s current limits.

        MAY raise on infrastructure failure (DB down, network error, ...) --
        callers (``quotas.check_quota``) treat any exception here as an
        infrastructure problem and fail OPEN (allow the call, log loudly),
        never as "this principal has no entitlements" (which is instead
        spelled by returning ``Entitlements()`` normally). See #309 design
        constraint #2.
        """
        ...


class NullEntitlementsProvider:
    """The shipped default provider: every principal is unlimited (#309).

    This is what makes "no entitlement record configured" and "self-hosted,
    no entitlements system wired up at all" the SAME code path, both
    resolving to ``Entitlements()`` (all ``None``) rather than needing a
    special case anywhere in the dispatcher.
    """

    async def get_entitlements(self, principal: "Principal") -> Entitlements:  # noqa: ARG002
        return Entitlements()


# Process-wide singleton, mirroring src/core/rate_limiter.py's
# get_rate_limiter/set_rate_limiter pattern (get_*/set_* + a module-level
# global) so both the "swap the backend" and the "reset between tests"
# stories are the same shape a maintainer has already seen once in this repo.
_entitlements_provider: EntitlementsProvider | None = None


def get_entitlements_provider() -> EntitlementsProvider:
    """Get the global entitlements provider, defaulting to
    ``NullEntitlementsProvider`` (unlimited for everyone) if a deployment
    has not called ``set_entitlements_provider`` with its own."""
    global _entitlements_provider
    if _entitlements_provider is None:
        _entitlements_provider = NullEntitlementsProvider()
    return _entitlements_provider


def set_entitlements_provider(provider: EntitlementsProvider) -> None:
    """Install a deployment-specific (or test-double) entitlements provider."""
    global _entitlements_provider
    _entitlements_provider = provider
