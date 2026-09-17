"""OpenAI-compatible wire-format adapter tests against a recorded fixture (#311 item 3)."""

from __future__ import annotations

import json
from pathlib import Path

import httpx

from inh_contracts.embedding import OpenAICompatibleProvider

FIXTURES = Path(__file__).parent / "fixtures"


def _mock_transport(json_body: object, captured: list[httpx.Request]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json=json_body)

    return httpx.MockTransport(handler)


def test_embed_batch_posts_expected_wire_shape() -> None:
    fixture = json.loads((FIXTURES / "openai_embeddings_response.json").read_text())
    captured: list[httpx.Request] = []
    provider = OpenAICompatibleProvider(
        base_url="http://openai-compatible.local",
        model_id="text-embedding-3-small",
        dimension=3,
        api_key="sk-test",
        transport=_mock_transport(fixture, captured),
    )

    provider.embed_batch(["hello", "world"])

    request = captured[0]
    assert request.url.path == "/v1/embeddings"
    body = json.loads(request.content)
    assert body == {"model": "text-embedding-3-small", "input": ["hello", "world"]}
    assert request.headers["authorization"] == "Bearer sk-test"


def test_embed_batch_orders_by_index_not_response_order() -> None:
    """The fixture's `data` array is deliberately out of order (index 1 before 0)."""
    fixture = json.loads((FIXTURES / "openai_embeddings_response.json").read_text())
    provider = OpenAICompatibleProvider(
        base_url="http://openai-compatible.local",
        model_id="text-embedding-3-small",
        dimension=3,
        transport=httpx.MockTransport(lambda req: httpx.Response(200, json=fixture)),
    )

    vectors = provider.embed_batch(["a", "b"])

    # index 0's embedding first, then index 1's -- request order, not the
    # (deliberately scrambled) response array order.
    assert vectors == [[0.101, 0.202, 0.303], [0.401, 0.502, 0.603]]


def test_name() -> None:
    provider = OpenAICompatibleProvider(base_url="http://x", model_id="m", dimension=3)
    assert provider.name == "openai_compatible"
