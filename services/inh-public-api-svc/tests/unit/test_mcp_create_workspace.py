"""Unit tests for the ``create_workspace`` MCP tool.

The engine cannot create workspaces itself: ownership and plan limits live in
the host app (intg-svc). The tool is a thin, authenticated call to the host's
``POST /internal/workspaces``. These tests mock the host with
``httpx.MockTransport`` and cover: success, write-scope enforcement, refusal
of workspace-scoped keys and plain API keys, host 400/401/403/404/5xx mapping,
and that the service secret never reaches logs or tool output.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from unittest.mock import patch

import httpx
import pytest
import structlog
from pydantic import SecretStr

from src.config.settings import settings
from src.mcp_server import server as mcp_server
from src.models.api_key import APIKeyInfo
from src.services import workspace_provisioning

pytestmark = pytest.mark.asyncio

SECRET = "super-secret-service-value-123"
HOST = "http://intg-svc:3001"


def _oauth_key(subject: str = "user_clerk_1", permissions=("read", "search", "write")):
    """The identity the HTTP transport synthesizes for an OAuth caller."""
    return APIKeyInfo(
        key_id=f"oauth:{subject}",
        user_id="mongo-user-1",
        workspace_id=None,
        permissions=list(permissions),  # type: ignore[arg-type]
    )


def _api_key(workspace_id: str | None = None) -> APIKeyInfo:
    return APIKeyInfo(
        key_id="key-1",
        user_id="user-1",
        workspace_id=workspace_id,
        permissions=["read", "search", "write"],
    )


@pytest.fixture(autouse=True)
def _configured_host(monkeypatch):
    monkeypatch.setattr(settings, "intg_svc_url", HOST)
    monkeypatch.setattr(settings, "engine_service_secret", SecretStr(SECRET))


def _mock_host(handler: Callable[[httpx.Request], httpx.Response]):
    """Route the provisioning client's requests to ``handler``."""
    transport = httpx.MockTransport(handler)
    return patch.object(
        workspace_provisioning,
        "_build_client",
        lambda: httpx.AsyncClient(transport=transport, timeout=1.0),
    )


async def _call(key: APIKeyInfo, **arguments):
    tool = mcp_server._TOOLS["create_workspace"]
    return (await tool.handler(key, {"api_key": "x", **arguments}))[0].text


def _ok_handler(captured: list[httpx.Request] | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if captured is not None:
            captured.append(request)
        body = json.loads(request.content)
        return httpx.Response(
            201,
            json={
                "workspace_id": "ws_new",
                "name": body["name"],
                "description": body.get("description"),
                "role": "owner",
            },
        )

    return handler


# --- registry ---------------------------------------------------------------


async def test_registered_as_write_tool_exposed_on_http() -> None:
    tool = mcp_server._TOOLS["create_workspace"]
    assert tool.permission == "write"
    assert tool.http_exposed is True
    assert tool.input_schema["required"] == ["api_key", "name"]
    assert set(tool.input_schema["properties"]) == {"api_key", "name", "description"}


# --- success ----------------------------------------------------------------


async def test_success_calls_host_with_secret_header_and_clerk_id() -> None:
    captured: list[httpx.Request] = []
    with _mock_host(_ok_handler(captured)):
        text = await _call(_oauth_key("user_abc"), name="Research", description="Notes")

    assert len(captured) == 1
    req = captured[0]
    assert req.method == "POST"
    assert str(req.url) == f"{HOST}/internal/workspaces"
    assert req.headers["X-Service-Secret"] == SECRET
    assert json.loads(req.content) == {
        "clerk_user_id": "user_abc",
        "name": "Research",
        "description": "Notes",
    }
    assert not text.startswith("Error:")
    payload = json.loads(text.split("```json", 1)[1].rsplit("```", 1)[0])["structured"]
    assert payload == {
        "workspace_id": "ws_new",
        "name": "Research",
        "description": "Notes",
        "role": "owner",
    }
    assert SECRET not in text


async def test_description_omitted_when_not_given() -> None:
    captured: list[httpx.Request] = []
    with _mock_host(_ok_handler(captured)):
        await _call(_oauth_key(), name="Solo")
    assert "description" not in json.loads(captured[0].content)


@pytest.mark.parametrize("name", ["", "   ", None, 42])
async def test_blank_or_non_string_name_rejected_without_calling_host(name) -> None:
    captured: list[httpx.Request] = []
    with _mock_host(_ok_handler(captured)):
        text = await _call(_oauth_key(), name=name)
    assert text.startswith("Error:") and "name" in text
    assert captured == []


# --- identity ---------------------------------------------------------------


async def test_workspace_scoped_key_refused_without_calling_host() -> None:
    captured: list[httpx.Request] = []
    with _mock_host(_ok_handler(captured)):
        text = await _call(_api_key(workspace_id="ws-1"), name="X")
    assert text.startswith("Error:") and "workspace-scoped" in text
    assert captured == []


async def test_plain_api_key_without_clerk_identity_refused() -> None:
    captured: list[httpx.Request] = []
    with _mock_host(_ok_handler(captured)):
        text = await _call(_api_key(), name="X")
    assert text.startswith("Error:") and "OAuth" in text
    assert captured == []


async def test_scoped_oauth_shaped_key_still_refused() -> None:
    key = _oauth_key().model_copy(update={"workspace_id": "ws-1"})
    with _mock_host(_ok_handler()):
        text = await _call(key, name="X")
    assert "workspace-scoped" in text


# --- write scope ------------------------------------------------------------


async def test_missing_write_scope_rejected_by_oauth_dispatcher() -> None:
    """A token without kb:write never reaches the handler (insufficient_scope)."""
    from src.mcp_server import http_transport
    from src.services.auth import Principal

    principal = Principal(
        principal_id="user_clerk_1",
        principal_type="oauth",
        scopes=frozenset({"kb:read", "kb:search"}),
        resolved_user_id="mongo-user-1",
    )
    captured: list[httpx.Request] = []
    with _mock_host(_ok_handler(captured)):
        result = await http_transport._call_tool_oauth("create_workspace", {"name": "X"}, principal)
    assert result.isError is True
    assert result.structuredContent["error"] == "insufficient_scope"
    assert result.structuredContent["scope"] == "kb:write"
    assert captured == []


async def test_oauth_end_to_end_with_write_scope_creates_workspace() -> None:
    from src.mcp_server import http_transport
    from src.services.auth import Principal

    principal = Principal(
        principal_id="user_clerk_9",
        principal_type="oauth",
        scopes=frozenset({"kb:read", "kb:search", "kb:write"}),
        resolved_user_id="mongo-user-9",
    )
    captured: list[httpx.Request] = []
    with (
        _mock_host(_ok_handler(captured)),
        patch.object(http_transport, "check_quota", return_value=None),
    ):
        result = await http_transport._call_tool_oauth(
            "create_workspace", {"name": "Via OAuth"}, principal
        )
    assert result.isError is False
    assert json.loads(captured[0].content)["clerk_user_id"] == "user_clerk_9"


# --- host error mapping -----------------------------------------------------


def _status(code: int, body: dict | None = None):
    return lambda request: httpx.Response(code, json=body or {})


async def test_403_plan_limit_message_passes_through() -> None:
    msg = "Workspace limit reached for the Free plan (1 of 1)."
    with _mock_host(_status(403, {"error": msg})):
        text = await _call(_oauth_key(), name="X")
    assert text == f"Error: {msg}"


async def test_403_entitlement_required_gets_clear_message() -> None:
    with _mock_host(_status(403, {"error": "entitlement_required"})):
        text = await _call(_oauth_key(), name="X")
    assert text.startswith("Error:") and "plan" in text.lower()
    assert "entitlement_required" not in text


async def test_404_unknown_user_message() -> None:
    with _mock_host(_status(404, {"error": "user_not_found"})):
        text = await _call(_oauth_key(), name="X")
    assert text.startswith("Error:") and "account" in text.lower()


async def test_400_invalid_input_message_passes_through() -> None:
    with _mock_host(_status(400, {"error": "name must be at most 100 characters"})):
        text = await _call(_oauth_key(), name="X")
    assert text == "Error: invalid workspace details: name must be at most 100 characters"


@pytest.mark.parametrize("code", [500, 502, 503])
async def test_5xx_is_generic_try_again_later(code) -> None:
    with _mock_host(_status(code, {"error": "boom stack trace at line 3"})):
        text = await _call(_oauth_key(), name="X")
    assert text.startswith("Error:") and "try again later" in text
    assert "boom" not in text and "stack" not in text


async def test_401_bad_secret_is_generic_and_does_not_leak() -> None:
    with _mock_host(_status(401, {"error": "bad secret"})):
        text = await _call(_oauth_key(), name="X")
    assert "try again later" in text
    assert SECRET not in text and "secret" not in text.lower()


async def test_network_failure_is_generic_try_again_later() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with _mock_host(boom):
        text = await _call(_oauth_key(), name="X")
    assert "try again later" in text


async def test_malformed_success_body_is_generic_error() -> None:
    with _mock_host(lambda r: httpx.Response(201, text="<html>not json</html>")):
        text = await _call(_oauth_key(), name="X")
    assert "try again later" in text


async def test_host_not_configured_is_clear_error(monkeypatch) -> None:
    monkeypatch.setattr(settings, "intg_svc_url", None)
    text = await _call(_oauth_key(), name="X")
    assert text.startswith("Error:") and "not available" in text


async def test_secret_not_configured_is_clear_error(monkeypatch) -> None:
    monkeypatch.setattr(settings, "engine_service_secret", None)
    text = await _call(_oauth_key(), name="X")
    assert text.startswith("Error:") and "not available" in text


# --- secret hygiene ---------------------------------------------------------


@pytest.mark.parametrize("code", [201, 401, 403, 500])
async def test_secret_never_in_logs_or_output(code, caplog, capsys) -> None:
    body = {"workspace_id": "w", "name": "X", "description": None, "role": "owner"}
    caplog.set_level(logging.DEBUG)
    with structlog.testing.capture_logs() as cap, _mock_host(_status(code, body)):
        text = await _call(_oauth_key(), name="X")
    out = capsys.readouterr()
    haystack = " ".join([text, caplog.text, out.out, out.err, json.dumps(cap, default=str)])
    assert SECRET not in haystack


async def test_secret_repr_is_masked() -> None:
    assert SECRET not in repr(settings.engine_service_secret)
    assert SECRET not in repr(settings.model_dump())
