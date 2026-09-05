"""Contracts for the CLI identity and local-only admin read surfaces (#278, #279)."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from src.api.router import router as api_router
from src.config import settings
from src.models.api_key import APIKeyInfo
from src.services import auth as auth_module
from src.services.auth import AuthService, get_api_key_info
from src.services.database import get_database

pytestmark = [pytest.mark.contract, pytest.mark.security]


@pytest.fixture
def key_info() -> APIKeyInfo:
    return APIKeyInfo(
        key_id="key-b",
        name="Principal B",
        user_id="user-b",
        workspace_id="ws-b",
        permissions=["read", "write", "search"],
    )


@pytest.fixture
def database() -> AsyncMock:
    database = AsyncMock()
    database.user_owns_workspace_in_mongo.return_value = True
    database.list_admin_workspaces.return_value = [
        {"workspace_id": "ws-b", "name": "B", "user_id": "user-b", "document_count": 2}
    ]
    database.list_admin_keys.return_value = [
        {
            "key_id": "key-b",
            "key_name": "Principal B",
            "key_prefix": "ink_bbbbbbbb",
            "workspace_id": "ws-b",
            "user_id": "user-b",
            "permissions": ["read"],
            "status": "active",
            "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
            "last_used_at": None,
            "expires_at": None,
        }
    ]
    return database


@pytest.fixture
def app(key_info: APIKeyInfo, database: AsyncMock):
    application = FastAPI()
    application.include_router(api_router)

    async def override_key_info():
        return key_info

    async def override_database():
        return database

    application.dependency_overrides[get_api_key_info] = override_key_info
    application.dependency_overrides[get_database] = override_database
    yield application
    application.dependency_overrides.clear()


@pytest.fixture
async def client(app):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://engine.example"
    ) as client:
        yield client


async def test_whoami_reports_only_the_authenticated_binding(client, database):
    response = await client.get("/v1/whoami")

    assert response.status_code == 200
    assert response.json() == {
        "key_id": "key-b",
        "key_name": "Principal B",
        "user_id": "user-b",
        "workspace_id": "ws-b",
        "workspace_ids": ["ws-b"],
        "permissions": ["read", "write", "search"],
        "engine_version": settings.version,
        "endpoint": "https://engine.example",
    }
    database.user_owns_workspace_in_mongo.assert_awaited_once_with("user-b", "ws-b")
    serialized = response.text
    assert "ink_b-secret" not in serialized
    assert "key_hash" not in serialized


async def test_user_scoped_whoami_reports_full_owned_set(client, database, key_info):
    key_info.workspace_id = None
    database.get_user_workspace_ids.return_value = ["ws-a", "ws-b"]

    response = await client.get("/v1/whoami")

    assert response.status_code == 200
    assert response.json()["workspace_ids"] == ["ws-a", "ws-b"]
    database.get_user_workspace_ids.assert_awaited_once_with("user-b")


async def test_stale_scoped_binding_does_not_fall_back_to_owned_set(client, database):
    database.user_owns_workspace_in_mongo.return_value = False

    response = await client.get("/v1/whoami")

    assert response.status_code == 200
    assert response.json()["workspace_ids"] == []
    database.get_user_workspace_ids.assert_not_awaited()


@pytest.mark.parametrize("path", ["/v1/admin/workspaces", "/v1/admin/keys"])
async def test_admin_routes_are_hidden_when_disabled(client, monkeypatch, path):
    monkeypatch.setattr(settings, "admin_api_enabled", False)
    assert (await client.get(path)).status_code == 404


async def test_enabled_admin_routes_are_paginated_safe_projections(client, database, monkeypatch):
    monkeypatch.setattr(settings, "admin_api_enabled", True)

    workspaces = await client.get("/v1/admin/workspaces?page=2&page_size=999")
    keys = await client.get("/v1/admin/keys?page=2&page_size=999")

    assert workspaces.status_code == keys.status_code == 200
    assert set(workspaces.json()[0]) == {"workspace_id", "name", "user_id", "document_count"}
    assert set(keys.json()[0]) == {
        "key_id",
        "key_name",
        "key_prefix",
        "workspace_id",
        "user_id",
        "permissions",
        "status",
        "created_at",
        "last_used_at",
        "expires_at",
    }
    database.list_admin_workspaces.assert_awaited_once_with(offset=100, limit=100)
    database.list_admin_keys.assert_awaited_once_with(offset=100, limit=100)
    assert "key_hash" not in keys.text
    assert "ink_b-secret" not in keys.text


async def test_admin_default_page_size(client, database, monkeypatch):
    monkeypatch.setattr(settings, "admin_api_enabled", True)
    await client.get("/v1/admin/workspaces")
    database.list_admin_workspaces.assert_awaited_once_with(offset=0, limit=20)


async def test_enabled_admin_requires_authentication(monkeypatch):
    monkeypatch.setattr(settings, "admin_api_enabled", True)
    application = FastAPI()
    application.include_router(api_router)
    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://test"
    ) as client:
        response = await client.get("/v1/admin/keys")
    assert response.status_code == 401


@pytest.mark.parametrize("path", ["/v1/admin/workspaces", "/v1/admin/keys"])
async def test_disabled_admin_is_hidden_without_authentication(monkeypatch, path):
    monkeypatch.setattr(settings, "admin_api_enabled", False)
    application = FastAPI()
    application.include_router(api_router)
    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://test"
    ) as client:
        response = await client.get(path)
    assert response.status_code == 404


@pytest.mark.parametrize("case", ["missing", "garbage", "revoked", "expired"])
async def test_whoami_rejects_auth_failures(case, monkeypatch):
    database = AsyncMock()
    headers = {}
    if case != "missing":
        headers = {"X-API-Key": f"ink_{case}"}
    if case == "expired":
        database.validate_api_key.return_value = APIKeyInfo(
            key_id="expired",
            user_id="user",
            permissions=["read"],
            expires_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
        )
    else:
        # Invalid and revoked keys are both absent from the active-only DB query.
        database.validate_api_key.return_value = None

    async def fake_auth_service():
        return AuthService(database)

    monkeypatch.setattr(auth_module, "get_auth_service", fake_auth_service)
    application = FastAPI()
    application.include_router(api_router)
    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://test"
    ) as client:
        response = await client.get("/v1/whoami", headers=headers)
    assert response.status_code == 401
