from __future__ import annotations

import json

import httpx
import pytest
from typer.testing import CliRunner

import inh_cli.client as client_mod
from inh_cli.main import app


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def test_search_posts_hybrid_by_default(api_env, inherent_home, runner, monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/v1/search"
        body = json.loads(request.content)
        assert body == {"query": "hello", "limit": 10, "search_mode": "hybrid"}
        return httpx.Response(
            200,
            json={
                "results": [{"score": 0.9, "document_name": "README.md", "content": "hello world"}]
            },
            request=request,
        )

    monkeypatch.setattr(client_mod, "_transport", httpx.MockTransport(handler))
    result = runner.invoke(app, ["search", "hello"])
    assert result.exit_code == 0, result.output
    assert "README.md" in result.stdout


def test_search_json_is_verbatim(api_env, inherent_home, runner, monkeypatch) -> None:
    payload = {"results": [{"score": 1, "content": "x"}], "event_id": "evt"}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload, request=request)

    monkeypatch.setattr(client_mod, "_transport", httpx.MockTransport(handler))
    result = runner.invoke(app, ["--json", "search", "q"])
    assert json.loads(result.stdout) == payload


def test_search_mode_typo_is_usage_error(api_env, inherent_home, runner) -> None:
    result = runner.invoke(app, ["search", "q", "--mode", "nonsense"])
    assert result.exit_code == 1
