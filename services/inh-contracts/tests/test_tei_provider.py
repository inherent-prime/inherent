"""TEI wire-format adapter tests against a recorded response fixture (#311 item 3)."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from inh_contracts.embedding import TEIProvider

FIXTURES = Path(__file__).parent / "fixtures"


def _mock_transport(json_body: object, captured: list[httpx.Request]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json=json_body)

    return httpx.MockTransport(handler)


def test_embed_batch_posts_expected_wire_shape_and_parses_bare_list() -> None:
    fixture = json.loads((FIXTURES / "tei_embed_response.json").read_text())
    captured: list[httpx.Request] = []
    provider = TEIProvider(
        base_url="http://tei.local",
        model_id="BAAI/bge-small-en-v1.5",
        dimension=3,
        transport=_mock_transport(fixture, captured),
    )

    vectors = provider.embed_batch(["hello", "world"])

    assert vectors == fixture
    assert len(captured) == 1
    request = captured[0]
    assert request.url.path == "/embed"
    body = json.loads(request.content)
    # truncate=true must survive the wire -- oversized inputs otherwise 413
    # and crash the entire batch (see tei_provider.py's docstring).
    assert body == {"inputs": ["hello", "world"], "truncate": True}


def test_embed_batch_raises_on_http_error_status() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    provider = TEIProvider(
        base_url="http://tei.local",
        model_id="m",
        dimension=3,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(httpx.HTTPStatusError):
        provider.embed_batch(["x"])


def test_name_and_identity() -> None:
    provider = TEIProvider(base_url="http://tei.local", model_id="m", dimension=384)
    assert provider.name == "tei"
    assert provider.model_id == "m"
    assert provider.dimension == 384
    identity = provider.identity()
    assert identity.model_id == "m"
    assert identity.dimension == 384


def test_authorization_header_sent_when_api_key_set() -> None:
    """TEI accepts an optional key -- when set it must reach the request as a header."""
    captured: list[httpx.Request] = []
    provider = TEIProvider(
        base_url="http://tei.local",
        model_id="m",
        dimension=3,
        api_key="tei-secret-key",
        transport=_mock_transport([[0.0, 0.0, 0.0]], captured),
    )

    provider.embed_batch(["x"])

    assert captured[0].headers["authorization"] == "Bearer tei-secret-key"


def test_no_authorization_header_when_api_key_unset() -> None:
    """Zero-config local dev: TEI works with no API key at all (#311 item 2)."""
    captured: list[httpx.Request] = []
    provider = TEIProvider(
        base_url="http://tei.local",
        model_id="m",
        dimension=3,
        transport=_mock_transport([[0.0, 0.0, 0.0]], captured),
    )

    provider.embed_batch(["x"])

    assert "authorization" not in captured[0].headers
