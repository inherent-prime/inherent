"""Non-mocked proof for #138 blocker-2: DatabaseService.user_owns_workspace_in_mongo
must be Mongo-only, and get_authorized_workspace_ids must use it (not the
Mongo-UNION-Postgres get_user_workspace_ids) to validate a workspace-scoped
key's binding.

Why this file exists (and why the previous round's tests didn't catch the
bug): tests/security/test_workspace_isolation.py pins the intersection
ARITHMETIC by mocking user_owns_workspace_in_mongo / get_user_workspace_ids
directly — those tests are structurally incapable of catching a regression
in the CHECK ITSELF (e.g. someone "helpfully" reintroducing the union inside
user_owns_workspace_in_mongo, or a future refactor that calls the wrong DB
method). This file drives the REAL DatabaseService methods — only the
Mongo/Postgres DRIVER internals are faked (a fake motor collection, a fake
SQLAlchemy session), not get_user_workspace_ids or user_owns_workspace_in_mongo
themselves — so a regression in the check's own logic shows up here even
though every input mock stays "correctly" configured.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from src.models.api_key import APIKeyInfo
from src.services.auth import get_authorized_workspace_ids
from src.services.database import DatabaseService
from src.services.metrics import workspace_ownership_lookup_degraded_total

pytestmark = pytest.mark.asyncio


def _degraded_count(source: str) -> float:
    """Current value of the #184 degraded-lookup counter for one source label."""
    return workspace_ownership_lookup_degraded_total.labels(source=source)._value.get()


class _FakeCursor:
    """Minimal async-iterable cursor mimicking motor's AsyncIOMotorCursor."""

    def __init__(self, docs: list[dict]):
        self._docs = docs

    def __aiter__(self):
        self._iter = iter(self._docs)
        return self

    async def __anext__(self):
        try:
            return next(self._iter)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


class _FakeMongoCollection:
    """Fakes the ``workspaces`` collection's two read shapes used by
    DatabaseService: ``find`` (used by get_user_workspace_ids's listing) and
    ``find_one`` (used by user_owns_workspace_in_mongo's targeted check).
    Configured independently so a test can assert Mongo has NO ownership
    record for a specific workspace while still returning a (possibly empty)
    general listing.
    """

    def __init__(self, find_docs: list[dict] | None = None, find_one_result: dict | None = None):
        self._find_docs = find_docs or []
        self._find_one_result = find_one_result

    def find(self, _filter, _projection=None):
        return _FakeCursor(self._find_docs)

    async def find_one(self, _filter):
        return self._find_one_result


class _FakeMongoDB:
    def __init__(self, collection: _FakeMongoCollection):
        self._collection = collection

    def __getitem__(self, name: str):
        assert name == "workspaces"
        return self._collection


class _FakeMongoClient:
    def __init__(self, collection: _FakeMongoCollection):
        self._db = _FakeMongoDB(collection)

    def __getitem__(self, _db_name: str):
        return self._db


class _FakeRow:
    """Mimics a SQLAlchemy Row exposing `.workspace_id` (the only attribute
    get_user_workspace_ids's PG fallback reads)."""

    def __init__(self, workspace_id: str):
        self.workspace_id = workspace_id


class _FakeResult:
    def __init__(self, rows: list[_FakeRow]):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _FakePgSession:
    def __init__(self, rows: list[_FakeRow]):
        self._rows = rows

    async def execute(self, *_args, **_kwargs):
        return _FakeResult(self._rows)

    async def close(self):
        pass


class _FakeSessionCM:
    """Fakes the async context manager DatabaseService.session_factory()
    normally returns (a SQLAlchemy AsyncSession context manager)."""

    def __init__(self, rows: list[_FakeRow]):
        self._rows = rows

    async def __aenter__(self):
        return _FakePgSession(self._rows)

    async def __aexit__(self, *_exc):
        return False


def _fake_pg_history(rows: list[_FakeRow]):
    """Build a session_factory (what DatabaseService.session_factory holds)
    returning PG rows shaped like a `processed_documents` upload-history
    query result, for wiring onto a real DatabaseService instance."""

    def factory():
        return _FakeSessionCM(rows)

    return factory


async def test_scoped_key_denied_despite_pg_upload_history_when_mongo_has_no_record():
    """The exact scenario #138 blocker-2 exists to close: Mongo says the
    user does NOT own ws-revoked (transferred/deleted), but Postgres still
    has a processed_documents row proving the user uploaded to it once
    (rows are never deleted on transfer — this is the realistic case, since
    a workspace worth protecting has content).

    Three real code paths are exercised against the SAME fake backends:
    1. get_user_workspace_ids (the union) DOES still see ws-revoked via the
       Postgres fallback — proving the fake wiring is realistic AND
       demonstrating why intersecting against this union (the previous
       round's fix) does not close the hole.
    2. user_owns_workspace_in_mongo (the new, targeted check) correctly
       returns False — it must not consult Postgres at all.
    3. get_authorized_workspace_ids — the actual function REST and MCP both
       call — resolves a key scoped to ws-revoked to NO authorised
       workspaces, end to end.
    """
    database = DatabaseService()  # real object; __init__ does no I/O

    mongo = _FakeMongoCollection(find_docs=[], find_one_result=None)
    pg_history = [_FakeRow("ws-revoked")]
    database.session_factory = _fake_pg_history(pg_history)

    with patch(
        "src.services.mongo_client.get_mongo_client",
        return_value=_FakeMongoClient(mongo),
    ):
        # 1. Sanity check + regression demonstration: the union helper is
        # fooled by stale PG history.
        union_result = await database.get_user_workspace_ids("user-1")
        assert union_result == ["ws-revoked"]

        # 2. The targeted Mongo-only check is NOT fooled by the same data.
        owns = await database.user_owns_workspace_in_mongo("user-1", "ws-revoked")
        assert owns is False

        # 3. End-to-end: get_authorized_workspace_ids (called by both REST's
        # _resolve_workspace and every MCP tool) denies the scoped key.
        key = APIKeyInfo(
            key_id="key-ws",
            user_id="user-1",
            workspace_id="ws-revoked",
            permissions=["read", "search"],
            rate_limit=100,
            expires_at=None,
            status="active",
        )
        authorized = await get_authorized_workspace_ids(key, database)

    assert authorized == []


async def test_scoped_key_allowed_when_mongo_confirms_current_ownership():
    """Control case: Mongo DOES have an ownership record for the workspace —
    the scoped key resolves normally. Guards against the fix over-correcting
    into always denying."""
    database = DatabaseService()

    mongo = _FakeMongoCollection(find_docs=[], find_one_result={"_id": "ws-a"})

    with patch(
        "src.services.mongo_client.get_mongo_client",
        return_value=_FakeMongoClient(mongo),
    ):
        owns = await database.user_owns_workspace_in_mongo("user-1", "ws-a")
        assert owns is True

        key = APIKeyInfo(
            key_id="key-ws",
            user_id="user-1",
            workspace_id="ws-a",
            permissions=["read", "search"],
            rate_limit=100,
            expires_at=None,
            status="active",
        )
        authorized = await get_authorized_workspace_ids(key, database)

    assert authorized == ["ws-a"]


async def test_mongo_failure_during_ownership_check_raises_not_swallows():
    """#138 blocker-2: a Mongo failure while validating a scoped key's
    binding must RAISE, not log-and-return a best-effort answer — revocation
    enforcement must not silently stop working during a Mongo outage. This
    is the opposite of get_user_workspace_ids's own error handling
    (log-and-continue), which is correct for a listing convenience but wrong
    for an authorization decision.
    """
    database = DatabaseService()

    class _ExplodingCollection:
        async def find_one(self, _filter):
            raise ConnectionError("mongo is down")

    class _ExplodingDB:
        def __getitem__(self, name):
            assert name == "workspaces"
            return _ExplodingCollection()

    class _ExplodingClient:
        def __getitem__(self, _name):
            return _ExplodingDB()

    with patch(
        "src.services.mongo_client.get_mongo_client",
        return_value=_ExplodingClient(),
    ):
        with pytest.raises(ConnectionError):
            await database.user_owns_workspace_in_mongo("user-1", "ws-a")


# ---------------------------------------------------------------------------
# #184: the log-and-swallow / raise paths above must also be observable via
# metric, not just a warning log or the exception itself. These drive the
# REAL DatabaseService methods (same rationale as the rest of this file) with
# only the Mongo/Postgres driver internals faked, so a regression that
# reintroduces a silent swallow (or drops the metric bump on the raise path)
# shows up here even though every input mock stays "correctly" configured.
# ---------------------------------------------------------------------------


async def test_mongo_workspace_listing_failure_increments_degraded_metric():
    """get_user_workspace_ids's Mongo branch log-and-swallows a Mongo failure
    (a listing convenience, unlike user_owns_workspace_in_mongo's raise) --
    but the swallow must be observable as a RATE, not just a warning log,
    mirroring AUDIT_MESSAGES_DROPPED_TOTAL (inh-ingestion-svc, #18)."""
    database = DatabaseService()
    database.session_factory = _fake_pg_history([])  # PG fallback: empty, no error

    class _ExplodingCollection:
        def find(self, _filter, _projection=None):
            raise ConnectionError("mongo is down")

    class _ExplodingDB:
        def __getitem__(self, name):
            assert name == "workspaces"
            return _ExplodingCollection()

    class _ExplodingClient:
        def __getitem__(self, _name):
            return _ExplodingDB()

    before = _degraded_count("mongo")

    with patch(
        "src.services.mongo_client.get_mongo_client",
        return_value=_ExplodingClient(),
    ):
        # The swallow means this must still return normally (empty set, since
        # the PG fallback above is also empty) -- not raise. Metric emission
        # must not change that behavior (CLAUDE.md: a metric emission failing
        # must not break the request -- here it's the metric SUCCEEDING that
        # must not change the swallow's own semantics).
        result = await database.get_user_workspace_ids("user-1")

    assert result == []
    assert _degraded_count("mongo") == before + 1


async def test_pg_workspace_fallback_failure_increments_degraded_metric():
    """Twin of the Mongo test above: the PG-fallback branch's own swallow
    must also be observable, with its own distinct label so the two failure
    sources aren't conflated on a dashboard."""
    database = DatabaseService()
    mongo = _FakeMongoCollection(find_docs=[], find_one_result=None)

    class _ExplodingSessionCM:
        async def __aenter__(self):
            raise ConnectionError("db degraded")

        async def __aexit__(self, *_exc):
            return False

    database.session_factory = lambda: _ExplodingSessionCM()
    before = _degraded_count("postgres_fallback")

    with patch(
        "src.services.mongo_client.get_mongo_client",
        return_value=_FakeMongoClient(mongo),
    ):
        result = await database.get_user_workspace_ids("user-1")

    assert result == []
    assert _degraded_count("postgres_fallback") == before + 1


async def test_mongo_ownership_check_failure_increments_metric_before_raising():
    """#184: the raise path doesn't swallow (#138 blocker-2, still true) but
    now ALSO increments the degraded-lookup counter, with its OWN label
    (never conflated with the listing-convenience "mongo" label above), before
    propagating -- so a spike in these raises is visible on a dashboard, not
    only as scattered 5xx/error responses across every scoped-key request."""
    database = DatabaseService()

    class _ExplodingCollection:
        async def find_one(self, _filter):
            raise ConnectionError("mongo is down")

    class _ExplodingDB:
        def __getitem__(self, name):
            assert name == "workspaces"
            return _ExplodingCollection()

    class _ExplodingClient:
        def __getitem__(self, _name):
            return _ExplodingDB()

    before = _degraded_count("mongo_ownership_check")

    with patch(
        "src.services.mongo_client.get_mongo_client",
        return_value=_ExplodingClient(),
    ):
        with pytest.raises(ConnectionError):
            await database.user_owns_workspace_in_mongo("user-1", "ws-a")

    assert _degraded_count("mongo_ownership_check") == before + 1
