"""OAuth subject -> Inherent user via a Mongo lookup (prime#329).

Resolution order under test: claim > Mongo lookup > static map > None.
The real ``DatabaseService.find_users_by_subject`` runs against an in-memory
stand-in for the users collection, so soft-delete, duplicates and "no
caching" are exercised for real.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from bson import ObjectId
from fastapi.testclient import TestClient
from pydantic import ValidationError

import src.services.auth as auth_mod
from src.config import settings
from src.config.settings import Settings
from src.main import create_app
from src.services.auth import OAuthClaims, OAuthIdentityLookupError, resolve_oauth_user
from src.services.database import DatabaseService
from tests._oauth_test_helpers import ISSUER, RESOURCE, make_token, patch_jwks_client

pytestmark = pytest.mark.security

SUB = "user_clerk_123"
USER_OID = ObjectId()


class _Cursor:
    def __init__(self, docs):
        self._docs = docs

    def limit(self, _n):
        return self

    def __aiter__(self):
        self._it = iter(self._docs)
        return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


class _Users:
    def __init__(self, docs: list[dict[str, Any]]):
        self.docs = docs
        self.fail = False
        self.queries: list[dict[str, Any]] = []

    def find(self, flt, projection=None):
        if self.fail:
            raise RuntimeError("mongo down")
        self.queries.append(flt)
        return _Cursor([d for d in self.docs if all(d.get(k) == v for k, v in flt.items())])


class _Db:
    def __init__(self, users: _Users):
        self.users = users

    def __getitem__(self, name):
        assert name == "users"
        return self.users


class _Client:
    def __init__(self, users: _Users):
        self._db = _Db(users)

    def __getitem__(self, _name):
        return self._db


@pytest.fixture
def users(monkeypatch):
    """A users collection wired in as the identity store."""
    monkeypatch.setattr(settings, "oauth_subject_lookup_collection", "users")
    monkeypatch.setattr(settings, "oauth_subject_lookup_field", "clerk_id")
    collection = _Users([{"_id": USER_OID, "clerk_id": SUB}])
    database = DatabaseService()
    with (
        patch("src.services.mongo_client.get_mongo_client", return_value=_Client(collection)),
        patch.object(auth_mod, "get_database", AsyncMock(return_value=database)),
    ):
        yield collection


def _claims(**raw: Any) -> OAuthClaims:
    return OAuthClaims(subject=SUB, scopes=frozenset({"kb:read"}), raw=raw)


async def test_lookup_resolves_subject_to_stringified_object_id(users):
    assert await resolve_oauth_user(_claims()) == str(USER_OID)


async def test_lookup_uses_a_configured_id_field(users, monkeypatch):
    monkeypatch.setattr(settings, "oauth_subject_lookup_id_field", "inherent_id")
    users.docs[0]["inherent_id"] = "user-abc"
    assert await resolve_oauth_user(_claims()) == "user-abc"


async def test_claim_beats_lookup_beats_static_map(users, monkeypatch):
    monkeypatch.setattr(settings, "_oauth_subject_users", {SUB: "from-static-map"})
    monkeypatch.setattr(settings, "oauth_user_id_claim", "inherent_user_id")

    assert await resolve_oauth_user(_claims(inherent_user_id="from-claim")) == "from-claim"
    assert await resolve_oauth_user(_claims()) == str(USER_OID)  # claim absent -> lookup

    users.docs.clear()  # lookup finds nothing -> static map
    assert await resolve_oauth_user(_claims()) == "from-static-map"


async def test_not_found_and_no_static_entry_resolves_to_none(users):
    users.docs.clear()
    assert await resolve_oauth_user(_claims()) is None


async def test_duplicate_matches_do_not_resolve_and_skip_the_static_map(users, monkeypatch):
    monkeypatch.setattr(settings, "_oauth_subject_users", {SUB: "from-static-map"})
    users.docs.append({"_id": ObjectId(), "clerk_id": SUB})
    assert await resolve_oauth_user(_claims()) is None


async def test_soft_deleted_user_does_not_resolve_even_with_a_static_entry(users, monkeypatch):
    monkeypatch.setattr(settings, "_oauth_subject_users", {SUB: "from-static-map"})
    users.docs[0]["deleted_at"] = datetime.now(timezone.utc)
    assert await resolve_oauth_user(_claims()) is None


async def test_null_deleted_at_counts_as_live(users):
    users.docs[0]["deleted_at"] = None
    assert await resolve_oauth_user(_claims()) == str(USER_OID)


async def test_deleted_check_can_be_disabled(users, monkeypatch):
    monkeypatch.setattr(settings, "oauth_subject_lookup_deleted_field", "")
    users.docs[0]["deleted_at"] = datetime.now(timezone.utc)
    assert await resolve_oauth_user(_claims()) == str(USER_OID)


async def test_a_live_user_alongside_a_deleted_duplicate_still_resolves(users):
    users.docs.append(
        {"_id": ObjectId(), "clerk_id": SUB, "deleted_at": datetime.now(timezone.utc)}
    )
    assert await resolve_oauth_user(_claims()) == str(USER_OID)


async def test_deletion_applies_on_the_very_next_request_no_cache(users):
    assert await resolve_oauth_user(_claims()) == str(USER_OID)
    users.docs[0]["deleted_at"] = datetime.now(timezone.utc)
    assert await resolve_oauth_user(_claims()) is None
    assert len(users.queries) == 2  # one indexed read per resolution


async def test_mongo_failure_fails_closed_instead_of_using_the_static_map(users, monkeypatch):
    monkeypatch.setattr(settings, "_oauth_subject_users", {SUB: "from-static-map"})
    users.fail = True
    with pytest.raises(OAuthIdentityLookupError):
        await resolve_oauth_user(_claims())


async def test_the_claim_still_wins_when_mongo_is_down(users, monkeypatch):
    monkeypatch.setattr(settings, "oauth_user_id_claim", "inherent_user_id")
    users.fail = True
    assert await resolve_oauth_user(_claims(inherent_user_id="from-claim")) == "from-claim"


async def test_lookup_unset_never_touches_mongo(monkeypatch):
    """Today's behaviour: no lookup settings -> the step is skipped entirely."""
    monkeypatch.setattr(settings, "_oauth_subject_users", {SUB: "from-static-map"})
    with patch.object(auth_mod, "get_database", AsyncMock(side_effect=AssertionError)):
        assert await resolve_oauth_user(_claims()) == "from-static-map"


async def test_subject_is_matched_as_a_literal_never_an_operator(users):
    users.docs.clear()
    assert await resolve_oauth_user(OAuthClaims(subject="$ne", scopes=frozenset(), raw={})) is None
    assert users.queries == [{"clerk_id": "$ne"}]


# --------------------------------------------------------------------------- #
# Startup validation
# --------------------------------------------------------------------------- #
def _settings(**env: str) -> Settings:
    return Settings(_env_file=None, **env)


def test_lookup_settings_default_to_disabled():
    cfg = _settings()
    assert not cfg.oauth_subject_lookup_enabled
    assert cfg.oauth_subject_lookup_id_field == "_id"


def test_lookup_settings_accept_simple_identifiers():
    cfg = _settings(OAUTH_SUBJECT_LOOKUP_COLLECTION="users", OAUTH_SUBJECT_LOOKUP_FIELD="clerk_id")
    assert cfg.oauth_subject_lookup_enabled


@pytest.mark.parametrize(
    "overrides",
    [
        {"OAUTH_SUBJECT_LOOKUP_COLLECTION": "users"},  # field missing
        {"OAUTH_SUBJECT_LOOKUP_FIELD": "clerk_id"},  # collection missing
        {"OAUTH_SUBJECT_LOOKUP_COLLECTION": "us.ers", "OAUTH_SUBJECT_LOOKUP_FIELD": "clerk_id"},
        {"OAUTH_SUBJECT_LOOKUP_COLLECTION": "users", "OAUTH_SUBJECT_LOOKUP_FIELD": "$where"},
        {"OAUTH_SUBJECT_LOOKUP_COLLECTION": "users", "OAUTH_SUBJECT_LOOKUP_FIELD": "a.b"},
        {
            "OAUTH_SUBJECT_LOOKUP_COLLECTION": "users",
            "OAUTH_SUBJECT_LOOKUP_FIELD": "clerk_id",
            "OAUTH_SUBJECT_LOOKUP_ID_FIELD": "",
        },
        {
            "OAUTH_SUBJECT_LOOKUP_COLLECTION": "users",
            "OAUTH_SUBJECT_LOOKUP_FIELD": "clerk_id",
            "OAUTH_SUBJECT_LOOKUP_DELETED_FIELD": "x y",
        },
    ],
)
def test_lookup_settings_reject_unsafe_or_partial_config(overrides):
    with pytest.raises(ValidationError):
        _settings(**overrides)


# --------------------------------------------------------------------------- #
# /mcp gate: an identity-store outage is a 503, not a silent anonymous caller
# --------------------------------------------------------------------------- #
def test_mcp_gate_answers_503_when_the_identity_lookup_is_down(monkeypatch):
    monkeypatch.setattr(settings, "oauth_enabled", True)
    monkeypatch.setattr(settings, "oauth_authorization_server", ISSUER)
    monkeypatch.setattr(settings, "oauth_resource_identifier", RESOURCE)
    patch_jwks_client(monkeypatch, auth_mod)
    app = create_app()
    with (
        patch("src.main.get_database", new_callable=AsyncMock),
        patch(
            "src.mcp_server.http_transport.Principal.from_oauth_claims",
            AsyncMock(side_effect=OAuthIdentityLookupError("x")),
        ),
        TestClient(app) as client,
    ):
        r = client.post(
            "/mcp",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "Authorization": f"Bearer {make_token()}",
            },
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )
    assert r.status_code == 503
