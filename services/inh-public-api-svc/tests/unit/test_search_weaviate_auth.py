"""Public-API must authenticate to Weaviate when a key is configured.

The public API queries Weaviate over httpx GraphQL. When WEAVIATE_API_KEY is set
(Weaviate API-key auth enabled), requests must carry an Authorization: Bearer
header — otherwise enabling Weaviate auth would break search.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.services.search import SearchService


@pytest.mark.asyncio
async def test_client_sends_bearer_auth_when_key_set():
    svc = SearchService(database=MagicMock(), weaviate_url="http://w", weaviate_api_key="secret")
    client = await svc._get_client()
    try:
        assert client.headers.get("authorization") == "Bearer secret"
    finally:
        await svc.close()


@pytest.mark.asyncio
async def test_client_has_no_auth_header_without_key():
    svc = SearchService(database=MagicMock(), weaviate_url="http://w")
    client = await svc._get_client()
    try:
        assert "authorization" not in client.headers
    finally:
        await svc.close()
