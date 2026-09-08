"""Minimal authenticated HTTP client and shared error translation."""

from __future__ import annotations

from typing import Any, Iterable

import click
import httpx

from inh_cli.config import Resolved, resolve

# Tests assign a MockTransport here so command bodies never construct real clients.
_transport: httpx.BaseTransport | None = None


class ClientError(click.ClickException):
    """A user-facing API failure with its CLI exit code."""

    def __init__(self, message: str, exit_code: int = 1) -> None:
        super().__init__(message)
        # Click declares this on the class but reads an instance override.
        setattr(self, "exit_code", exit_code)


def make_client(
    resolved: Resolved, *, transport: httpx.BaseTransport | None = None
) -> httpx.Client:
    """Create an authenticated client for one resolved stack."""

    headers = {"X-API-Key": resolved.api_key}
    if resolved.workspace_id:
        headers["X-Workspace-Id"] = resolved.workspace_id
    return httpx.Client(
        base_url=resolved.url,
        headers=headers,
        transport=transport or _transport,
    )


def _body_text(response: httpx.Response) -> str:
    content_type = response.headers.get("content-type", "").split(";", 1)[0]
    if content_type in {"application/problem+json", "application/json"}:
        try:
            payload = response.json()
        except ValueError:
            return ""
        if isinstance(payload, dict):
            detail = payload.get("detail")
            title = payload.get("title")
            if isinstance(detail, str) and title:
                return f"{title}: {detail}"
            if isinstance(detail, str):
                return detail
            if title:
                return str(title)
    return ""


def request(
    client: httpx.Client,
    method: str,
    path: str,
    *,
    allow_statuses: Iterable[int] = (),
    **kwargs: Any,
) -> httpx.Response:
    """Make a request and translate transport and HTTP failures once."""

    try:
        response = client.request(method, path, **kwargs)
    except httpx.ConnectError as error:
        raise ClientError(
            f"No stack reachable at {client.base_url}. Run `inherent up` and try again.",
            exit_code=2,
        ) from error

    if response.status_code in tuple(allow_statuses):
        return response
    # 401 only. A 403 means the key is valid but not authorized for what was
    # asked -- almost always the wrong workspace -- and the server says so in
    # problem+json. Collapsing it here told users to rotate a working key.
    if response.status_code == 401:
        raise ClientError("API key rejected. Check INHERENT_API_KEY or reconnect this CLI.")
    if response.status_code == 400:
        text = _body_text(response)
        if "Multiple workspaces" in text:
            raise ClientError(
                "Multiple workspaces. Pass --workspace <id> or set it in ~/.inherent/config.toml."
            )
    if response.is_error:
        content_type = response.headers.get("content-type", "").split(";", 1)[0]
        if content_type == "application/problem+json":
            try:
                problem = response.json()
            except ValueError as error:
                raise ClientError(
                    f"HTTP {response.status_code}: invalid problem response"
                ) from error
            title = problem.get("title", f"HTTP {response.status_code}")
            detail = problem.get("detail")
            raise ClientError(f"{title}: {detail}" if detail else str(title))
        text = _body_text(response)
        if text:
            raise ClientError(text)
        raise ClientError(f"HTTP {response.status_code}: {response.reason_phrase}")
    return response


def call(
    method: str,
    path: str,
    *,
    workspace_id: str | None = None,
    allow_statuses: Iterable[int] = (),
    **kwargs: Any,
) -> httpx.Response:
    """Resolve config, open a client, and issue one request."""

    resolved = resolve(workspace_id=workspace_id)
    with make_client(resolved) as client:
        return request(client, method, path, allow_statuses=allow_statuses, **kwargs)
