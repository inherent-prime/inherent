"""Unit tests for the per-identity entitlements seam (#309).

See ``src/services/entitlements.py`` for the design ("no plan names or tier
values belong in this repo" -- a deployment plugs in its own provider) and
``tests/unit/test_quotas.py`` for how ``Entitlements`` values are enforced.
"""

from __future__ import annotations

import pytest

from src.services.auth import Principal
from src.services.entitlements import (
    Entitlements,
    NullEntitlementsProvider,
    get_entitlements_provider,
    set_entitlements_provider,
)

pytestmark = pytest.mark.asyncio


def _principal(principal_id: str = "user-1") -> Principal:
    return Principal(principal_id=principal_id, principal_type="api_key", scopes=frozenset())


class TestEntitlementsUnlimited:
    def test_default_entitlements_is_unlimited(self):
        """Every field defaults to None -- the all-optional, absent-means-
        unlimited shape the issue's limits table specifies."""
        entitlements = Entitlements()
        assert entitlements.unlimited is True

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"calls_per_month": 1000},
            {"writes_per_day": 50},
            {"calls_per_minute": 10},
            {"max_documents": 100},
        ],
    )
    def test_any_single_limit_makes_it_not_unlimited(self, kwargs):
        assert Entitlements(**kwargs).unlimited is False

    def test_upgrade_url_alone_does_not_affect_unlimited(self):
        """upgrade_url is metadata for a rejection message, not a limit --
        setting only it must not flip a principal from unlimited to
        limited (which would make every call pay the quota-check cost for
        an operator who only configured a support URL)."""
        entitlements = Entitlements(upgrade_url="https://example.com/upgrade")
        assert entitlements.unlimited is True


class TestNullEntitlementsProvider:
    async def test_returns_unlimited_for_any_principal(self):
        """The shipped default -- every principal, regardless of identity,
        is unlimited (#309 design constraint #1)."""
        provider = NullEntitlementsProvider()
        result = await provider.get_entitlements(_principal("anyone"))
        assert result == Entitlements()
        assert result.unlimited is True


class TestProviderSingleton:
    def test_default_provider_is_null_provider(self):
        provider = get_entitlements_provider()
        assert isinstance(provider, NullEntitlementsProvider)

    def test_repeated_calls_return_the_same_instance(self):
        assert get_entitlements_provider() is get_entitlements_provider()

    async def test_set_entitlements_provider_overrides_the_default(self):
        class _FixedProvider:
            async def get_entitlements(self, principal: Principal) -> Entitlements:
                return Entitlements(calls_per_minute=5)

        set_entitlements_provider(_FixedProvider())
        provider = get_entitlements_provider()
        result = await provider.get_entitlements(_principal())
        assert result.calls_per_minute == 5
