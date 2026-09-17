"""Offline tests for the one-shot release-stack bootstrap mode."""

from __future__ import annotations

import os
from types import SimpleNamespace
from uuid import uuid4

import pytest
from structlog.testing import capture_logs

from src.services import bootstrap
from src.utils.logger import _is_production_env


@pytest.fixture(autouse=True)
def cleanup_test_data():
    """Keep these unit tests independent of the package's PostgreSQL fixture."""
    yield


class FakeCursor:
    def __init__(self, calls: list[tuple[str, tuple[object, ...]]], rowcount: int = 1) -> None:
        self.calls = calls
        self.rowcount = rowcount

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def execute(self, sql: str, params: tuple[object, ...]) -> None:
        self.calls.append((sql, params))


class FakePostgres:
    def __init__(self, calls: list[tuple[str, tuple[object, ...]]], rowcount: int = 1) -> None:
        self.calls = calls
        self.rowcount = rowcount

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def cursor(self) -> FakeCursor:
        return FakeCursor(self.calls, self.rowcount)


class FakeCollection:
    def __init__(self, calls: list[tuple[dict, dict, bool]]) -> None:
        self.calls = calls

    async def update_one(self, query: dict, update: dict, *, upsert: bool) -> None:
        self.calls.append((query, update, upsert))


class FakeMongo:
    def __init__(self, calls: list[tuple[dict, dict, bool]]) -> None:
        self.calls = calls
        self.closed = False

    def __getitem__(self, _name: str):
        return self

    @property
    def workspaces(self) -> FakeCollection:
        return FakeCollection(self.calls)

    def close(self) -> None:
        self.closed = True


def _settings(api_key: str = "ink_secret_value") -> SimpleNamespace:
    return SimpleNamespace(
        bootstrap_api_key=api_key,
        bootstrap_workspace_id="ws_default",
        bootstrap_user_id="local-user",
        bootstrap_key_name="Local CLI Key",
        bootstrap_workspace_name="Default Workspace",
        bootstrap_action="seed",
        bootstrap_key_prefix=None,
        database_url="postgresql://postgres:test@postgres/main",
        mongodb_uri="mongodb://mongodb:27017",
        mongodb_db_name="main",
    )


def _patch_databases(monkeypatch, rowcount: int = 1):
    postgres_calls: list[tuple[str, tuple[object, ...]]] = []
    mongo_calls: list[tuple[dict, dict, bool]] = []
    mongo = FakeMongo(mongo_calls)
    monkeypatch.setattr(
        bootstrap.psycopg2,
        "connect",
        lambda _url: FakePostgres(postgres_calls, rowcount),
    )
    monkeypatch.setattr(bootstrap, "AsyncIOMotorClient", lambda _uri: mongo)
    return postgres_calls, mongo_calls, mongo


def test_bootstrap_mode_uses_production_logging(monkeypatch) -> None:
    monkeypatch.delenv("NODE_ENV", raising=False)
    monkeypatch.setenv("SERVICE_MODE", "bootstrap")
    assert _is_production_env() is True


async def test_invalid_key_fails_before_database_access(monkeypatch) -> None:
    postgres = monkeypatch.setattr(
        bootstrap.psycopg2,
        "connect",
        lambda _url: pytest.fail("PostgreSQL must not be contacted"),
    )
    mongo = monkeypatch.setattr(
        bootstrap,
        "AsyncIOMotorClient",
        lambda _uri: pytest.fail("MongoDB must not be contacted"),
    )

    with pytest.raises(ValueError, match="ink_"):
        await bootstrap.run_bootstrap(_settings("bad-key"))

    assert postgres is None and mongo is None


async def test_bootstrap_upserts_key_and_workspace_without_logging_secret(monkeypatch) -> None:
    postgres_calls, mongo_calls, mongo = _patch_databases(monkeypatch)

    with capture_logs() as logs:
        await bootstrap.run_bootstrap(_settings())

    sql, params = postgres_calls[0]
    assert "ON CONFLICT (key_hash) DO UPDATE" in sql
    assert "status = 'active'" in sql
    assert params[1] == bootstrap.hashlib.sha256(b"ink_secret_value").hexdigest()
    assert params[2:] == (
        "ink_secret_v",
        "local-user",
        "ws_default",
        "Local CLI Key",
        '["read", "write", "search"]',
        1000,
    )
    assert mongo_calls == [
        (
            {"_id": "ws_default"},
            {"$set": {"user_id": "local-user", "name": "Default Workspace"}},
            True,
        )
    ]
    assert mongo.closed is True
    assert "ink_secret_value" not in str(logs)
    assert "ink_secret_v" in str(logs)


async def test_rerun_is_idempotent_and_rotated_key_gets_a_new_id(monkeypatch) -> None:
    postgres_calls, _mongo_calls, _mongo = _patch_databases(monkeypatch)

    await bootstrap.run_bootstrap(_settings())
    await bootstrap.run_bootstrap(_settings())
    await bootstrap.run_bootstrap(_settings("ink_rotated_value"))

    params = [call[1] for call in postgres_calls]
    assert params[0][1] == params[1][1]
    assert params[0][0] != params[1][0] != params[2][0]
    assert params[0][1] != params[2][1]


@pytest.mark.compose
@pytest.mark.integration
async def test_bootstrap_is_idempotent_against_compose_datastores() -> None:
    """Exercise both real upserts against the host-published Compose stores."""
    database_url = os.getenv(
        "BOOTSTRAP_TEST_DATABASE_URL",
        "postgresql://postgres:postgres@localhost:15432/knowledge_base",
    )
    mongodb_uri = os.getenv("BOOTSTRAP_TEST_MONGODB_URI", "mongodb://localhost:27018")
    suffix = uuid4().hex
    api_key = f"ink_test_{suffix}"
    workspace_id = f"ws_test_{suffix}"
    settings = _settings(api_key)
    settings.database_url = database_url
    settings.mongodb_uri = mongodb_uri
    settings.bootstrap_workspace_id = workspace_id

    # Probe both stores before seeding so an unavailable local stack skips
    # without leaving a record in whichever store happened to be reachable.
    postgres = None
    mongo = None
    try:
        postgres = bootstrap.psycopg2.connect(database_url)
        mongo = bootstrap.AsyncIOMotorClient(mongodb_uri, serverSelectionTimeoutMS=1000)
        await mongo.admin.command("ping")
    except Exception as exc:
        if postgres:
            postgres.close()
        if mongo:
            mongo.close()
        pytest.skip(f"Compose datastores unavailable: {exc}")

    key_hash = bootstrap.hashlib.sha256(api_key.encode()).hexdigest()
    try:
        await bootstrap.run_bootstrap(settings)
        await bootstrap.run_bootstrap(settings)

        with postgres.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM api_keys WHERE key_hash = %s", (key_hash,))
            assert cursor.fetchone() == (1,)
        workspace = await mongo["main"].workspaces.find_one({"_id": workspace_id})
        assert workspace["user_id"] == "local-user"
    finally:
        with postgres.cursor() as cursor:
            cursor.execute("DELETE FROM api_keys WHERE key_hash = %s", (key_hash,))
        postgres.commit()
        postgres.close()
        await mongo["main"].workspaces.delete_one({"_id": workspace_id})
        mongo.close()


# --- BOOTSTRAP_ACTION: `inherent keys create|revoke` reach this container ---


@pytest.mark.asyncio
async def test_revoke_marks_exactly_one_key_and_leaves_mongo_alone(monkeypatch) -> None:
    postgres_calls, mongo_calls, _ = _patch_databases(monkeypatch, rowcount=1)
    settings = _settings()
    settings.bootstrap_action = "revoke"
    settings.bootstrap_key_prefix = "ink_abc12345"

    await bootstrap.run_bootstrap(settings)

    assert len(postgres_calls) == 1
    sql, params = postgres_calls[0]
    assert "status = 'revoked'" in sql
    assert params == ("ink_abc12345",)
    assert mongo_calls == []


@pytest.mark.asyncio
async def test_revoke_of_an_unknown_prefix_fails(monkeypatch) -> None:
    """Exit non-zero, or the CLI reports a revocation that never happened."""
    _patch_databases(monkeypatch, rowcount=0)
    settings = _settings()
    settings.bootstrap_action = "revoke"
    settings.bootstrap_key_prefix = "ink_missing00"

    with pytest.raises(ValueError, match="No active API key"):
        await bootstrap.run_bootstrap(settings)


@pytest.mark.asyncio
async def test_revoke_refuses_an_ambiguous_prefix(monkeypatch) -> None:
    _patch_databases(monkeypatch, rowcount=2)
    settings = _settings()
    settings.bootstrap_action = "revoke"
    settings.bootstrap_key_prefix = "ink_shared000"

    with pytest.raises(ValueError, match="refusing to revoke"):
        await bootstrap.run_bootstrap(settings)


@pytest.mark.asyncio
async def test_revoke_requires_an_ink_prefix_before_touching_postgres(monkeypatch) -> None:
    postgres_calls, _, _ = _patch_databases(monkeypatch)
    settings = _settings()
    settings.bootstrap_action = "revoke"
    settings.bootstrap_key_prefix = "abc"

    with pytest.raises(ValueError, match="BOOTSTRAP_KEY_PREFIX"):
        await bootstrap.run_bootstrap(settings)
    assert postgres_calls == []


@pytest.mark.asyncio
async def test_create_adds_a_key_without_renaming_the_workspace(monkeypatch) -> None:
    """`keys create` must not reset a workspace the operator renamed."""
    postgres_calls, mongo_calls, _ = _patch_databases(monkeypatch)
    settings = _settings(api_key="ink_second_key")
    settings.bootstrap_action = "create"

    await bootstrap.run_bootstrap(settings)

    assert len(postgres_calls) == 1
    assert "INSERT INTO api_keys" in postgres_calls[0][0]
    # One upsert, and it writes only on insert: an existing workspace keeps
    # whatever name the operator gave it.
    assert len(mongo_calls) == 1
    query, update, upsert = mongo_calls[0]
    assert query == {"_id": "ws_default"}
    assert upsert is True
    assert "$set" not in update
    assert update["$setOnInsert"] == {
        "user_id": "local-user",
        "name": "Default Workspace",
    }


@pytest.mark.asyncio
async def test_create_binds_the_key_to_a_workspace_that_exists(monkeypatch) -> None:
    """A key bound to a never-created workspace 403s on every request.

    `create` used to skip Mongo entirely, so `inherent keys create --workspace
    ws_new` returned a key whose `whoami` reported `workspace_id` set but
    `workspace_ids` empty.
    """
    _, mongo_calls, _ = _patch_databases(monkeypatch)
    settings = _settings(api_key="ink_new_workspace_key")
    settings.bootstrap_action = "create"
    settings.bootstrap_workspace_id = "ws_brand_new"

    await bootstrap.run_bootstrap(settings)

    assert [query for query, _, _ in mongo_calls] == [{"_id": "ws_brand_new"}]
    assert all(upsert for _, _, upsert in mongo_calls)


@pytest.mark.asyncio
async def test_seed_still_keeps_the_workspace_name_current(monkeypatch) -> None:
    """`seed` owns the workspace, so it writes the name on every run."""
    _, mongo_calls, _ = _patch_databases(monkeypatch)

    await bootstrap.run_bootstrap(_settings())

    _, update, _ = mongo_calls[0]
    assert update["$set"] == {"user_id": "local-user", "name": "Default Workspace"}


@pytest.mark.asyncio
async def test_unknown_action_fails_before_any_database_access(monkeypatch) -> None:
    postgres_calls, mongo_calls, _ = _patch_databases(monkeypatch)
    settings = _settings()
    settings.bootstrap_action = "delete-everything"

    with pytest.raises(ValueError, match="BOOTSTRAP_ACTION"):
        await bootstrap.run_bootstrap(settings)
    assert postgres_calls == [] and mongo_calls == []
