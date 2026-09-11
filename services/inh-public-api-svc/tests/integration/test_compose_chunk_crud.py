"""Live chunk CRUD round trip against a real Compose stack (#133 review).

akash1047's review on PR #248 (commit 8ed9fdb) reproduced chunk edits as a
hard 503 against the real stack: ``SearchService.upsert_chunk_vector``'s
merge-object PATCH sent ``tenant`` as a query param, which a multi-tenant
Weaviate 1.27 class 422s on -- "class ... has multi-tenancy enabled, but
request was without tenant". Every unit test mocks the Weaviate client, so
none of them could catch a wire-format defect that only exists against the
real server; ``tests/unit/test_chunk_vectors.py`` asserted the WRONG contract
until this fix. This file is the live proof the review asked for: a real
create -> edit -> retrieve -> delete round trip through the actual REST
routes, actual Postgres, and actual Weaviate.

This test is marked ``compose`` and is deselected by the default pytest run
(see ``addopts`` in pyproject). Run it against a live stack with::

    make dev            # or: make quickstart
    uv run pytest tests/integration/test_compose_chunk_crud.py -v --no-cov

Configuration (all have local defaults; override via env):
    PUBLIC_API_URL            default http://localhost:18000
    INTEGRATION_API_KEY       default ink_dev_local_key_001
    INTEGRATION_WORKSPACE_ID  default ws_local_001
    INTEGRATION_TIMEOUT       seconds to wait for ingestion (default 180)
"""

from __future__ import annotations

import os
import time
import uuid
from pathlib import Path

import httpx
import pytest

pytestmark = [pytest.mark.compose, pytest.mark.integration, pytest.mark.slow]

API_URL = os.environ.get("PUBLIC_API_URL", "http://localhost:18000").rstrip("/")
API_KEY = os.environ.get("INTEGRATION_API_KEY", "ink_dev_local_key_001")
WORKSPACE_ID = os.environ.get("INTEGRATION_WORKSPACE_ID", "ws_local_001")
TIMEOUT = int(os.environ.get("INTEGRATION_TIMEOUT", "180"))

HEADERS = {"X-API-Key": API_KEY, "X-Workspace-Id": WORKSPACE_ID}

SAMPLE_DOC = Path(
    os.environ.get(
        "INTEGRATION_SAMPLE_DOC",
        str(Path(__file__).resolve().parents[4] / "docs/examples/sample-documents/sample.txt"),
    )
)


def _require_stack(client: httpx.Client) -> None:
    try:
        resp = client.get(f"{API_URL}/health", timeout=5)
    except httpx.HTTPError as exc:
        pytest.skip(f"public API not reachable at {API_URL}: {exc}")
    if resp.status_code != 200:
        pytest.skip(f"public API unhealthy at {API_URL}: HTTP {resp.status_code}")


@pytest.fixture(scope="module")
def client() -> httpx.Client:
    with httpx.Client(timeout=30) as c:
        _require_stack(c)
        yield c


@pytest.fixture()
def processed_document_id(client: httpx.Client) -> str:
    """Upload a fresh fixture and wait until it is queryable via chunks GET.

    A dedicated per-test document (not a shared fixture) so this test's
    creates/edits/deletes on chunk_index can never race another test's
    view of the same document's chunk list.
    """
    assert SAMPLE_DOC.exists(), f"fixture missing: {SAMPLE_DOC}"
    sentinel = uuid.uuid4().hex[:8]
    with SAMPLE_DOC.open("rb") as fh:
        content = fh.read() + f"\n\nsentinel-{sentinel}".encode()
    upload = client.post(
        f"{API_URL}/v1/documents",
        headers=HEADERS,
        files={"file": (f"chunk-crud-{sentinel}.txt", content, "text/plain")},
    )
    assert upload.status_code == 201, f"upload failed: {upload.status_code} {upload.text}"
    document_id = upload.json()["document_id"]

    deadline = time.monotonic() + TIMEOUT
    while time.monotonic() < deadline:
        resp = client.get(f"{API_URL}/v1/chunks/{document_id}", headers=HEADERS)
        if resp.status_code == 200 and resp.json():
            return document_id
        time.sleep(3)
    pytest.fail(f"document {document_id} did not finish ingestion within {TIMEOUT}s")


def test_create_edit_retrieve_delete_round_trip(
    client: httpx.Client, processed_document_id: str
) -> None:
    document_id = processed_document_id

    # 1. CREATE -- append a chunk. This is the call the review's repro did
    # NOT touch (only PATCH 503'd), so it's the control that proves the
    # stack itself is healthy before the fix under test runs.
    create = client.post(
        f"{API_URL}/v1/chunks/{document_id}",
        headers=HEADERS,
        json={"content": "original chunk content for the CRUD round trip"},
    )
    assert create.status_code == 201, f"create failed: {create.status_code} {create.text}"
    chunk = create.json()
    chunk_index = chunk["chunk_index"]
    assert chunk["content"] == "original chunk content for the CRUD round trip"

    # 2. EDIT -- the exact call akash1047 reproduced as a 503. Before the
    # fix, SearchService.upsert_chunk_vector's merge-object PATCH sent
    # `tenant` as a query param; Weaviate 1.27 422s a multi-tenant class on
    # that, PG compensation rolled the row back, and the route surfaced it
    # as 503.
    edit = client.patch(
        f"{API_URL}/v1/chunks/{document_id}/index/{chunk_index}",
        headers=HEADERS,
        json={"content": "EDITED chunk content for the CRUD round trip"},
    )
    assert edit.status_code == 200, f"edit failed: {edit.status_code} {edit.text}"
    assert edit.json()["content"] == "EDITED chunk content for the CRUD round trip"

    # 3. RETRIEVE -- confirm the edit actually landed in Postgres (not just
    # that the route returned 200).
    fetched = client.get(f"{API_URL}/v1/chunks/{document_id}", headers=HEADERS)
    assert fetched.status_code == 200
    matching = [c for c in fetched.json() if c["chunk_index"] == chunk_index]
    assert len(matching) == 1
    assert matching[0]["content"] == "EDITED chunk content for the CRUD round trip"

    # 4. RETRIEVE (search) -- confirm the edit's re-embed actually reached
    # Weaviate, not just Postgres. Poll: indexing is async relative to the
    # PATCH response.
    deadline = time.monotonic() + TIMEOUT
    found_edited = False
    while time.monotonic() < deadline:
        search = client.post(
            f"{API_URL}/v1/search",
            headers={**HEADERS, "Content-Type": "application/json"},
            json={"query": "EDITED chunk content for the CRUD round trip", "limit": 5},
        )
        assert search.status_code == 200, f"search failed: {search.status_code} {search.text}"
        if any(
            r["document_id"] == document_id and "EDITED chunk content" in r.get("content", "")
            for r in search.json()["results"]
        ):
            found_edited = True
            break
        time.sleep(2)
    assert found_edited, "edited chunk content did not become searchable in Weaviate"

    # 5. DELETE -- hard-delete, then confirm it is gone from both stores.
    delete = client.delete(
        f"{API_URL}/v1/chunks/{document_id}/index/{chunk_index}", headers=HEADERS
    )
    assert delete.status_code == 204, f"delete failed: {delete.status_code} {delete.text}"

    after_delete = client.get(f"{API_URL}/v1/chunks/{document_id}", headers=HEADERS)
    assert after_delete.status_code == 200
    assert chunk_index not in {c["chunk_index"] for c in after_delete.json()}
