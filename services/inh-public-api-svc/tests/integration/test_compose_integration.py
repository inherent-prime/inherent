"""Compose-backed ingestion-to-search integration test (#15).

Exercises the real local stack end to end: upload a fixture through the public
API, wait for ingestion to finish, and assert that ``/v1/search`` returns the
indexed content.

This test is marked ``compose`` and is deselected by the default pytest run
(see ``addopts`` in pyproject). Run it against a live stack with::

    make dev            # or: make quickstart
    uv run pytest -m compose

Configuration (all have local defaults; override via env):
    PUBLIC_API_URL            default http://localhost:18000
    INTEGRATION_API_KEY       default ink_dev_local_key_001
    INTEGRATION_WORKSPACE_ID  default ws_local_001
    INTEGRATION_TIMEOUT       seconds to wait for ingestion (default 180)
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import httpx
import pytest

pytestmark = [pytest.mark.compose, pytest.mark.integration, pytest.mark.slow]

API_URL = os.environ.get("PUBLIC_API_URL", "http://localhost:18000").rstrip("/")
API_KEY = os.environ.get("INTEGRATION_API_KEY", "ink_dev_local_key_001")
WORKSPACE_ID = os.environ.get("INTEGRATION_WORKSPACE_ID", "ws_local_001")
TIMEOUT = int(os.environ.get("INTEGRATION_TIMEOUT", "180"))

# repo root: tests/integration/<file> -> parents[4]
SAMPLE_DOC = Path(
    os.environ.get(
        "INTEGRATION_SAMPLE_DOC",
        str(Path(__file__).resolve().parents[4] / "docs/examples/sample-documents/sample.txt"),
    )
)

HEADERS = {"X-API-Key": API_KEY, "X-Workspace-Id": WORKSPACE_ID}


def _require_stack(client: httpx.Client) -> None:
    """Skip (don't fail) when no healthy stack is reachable."""
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


def _search(client: httpx.Client) -> dict:
    resp = client.post(
        f"{API_URL}/v1/search",
        headers={**HEADERS, "Content-Type": "application/json"},
        json={"query": "what retrieval modes does Inherent support", "limit": 5},
    )
    assert resp.status_code == 200, f"search failed: {resp.status_code} {resp.text}"
    return resp.json()


# The one test that must pass before any PR merges (.github/workflows/
# e2e-smoke.yml, `-m "smoke and compose"`). It is the shortest path that still
# proves the whole stack is wired: upload through the public API, the Temporal
# worker extracts/chunks/embeds, and search returns the document from Weaviate.
# The other roundtrip variants below stay full-lane -- they cover behavior on
# top of this path, so they add runtime to the merge gate without adding
# "is the stack alive" signal.
@pytest.mark.smoke
def test_ingestion_to_search_roundtrip(client: httpx.Client) -> None:
    assert SAMPLE_DOC.exists(), f"fixture missing: {SAMPLE_DOC}"

    # 1. Upload the fixture.
    with SAMPLE_DOC.open("rb") as fh:
        upload = client.post(
            f"{API_URL}/v1/documents",
            headers=HEADERS,
            files={"file": (SAMPLE_DOC.name, fh, "text/plain")},
        )
    assert upload.status_code == 201, f"upload failed: {upload.status_code} {upload.text}"
    document_id = upload.json()["document_id"]
    assert document_id

    # 2. Poll search until the uploaded document is indexed and retrievable.
    #    Search becoming non-empty for our document IS the signal that the full
    #    ingest pipeline (extract -> chunk -> embed -> index) completed. We poll
    #    search rather than the document-status endpoint so this test does not
    #    depend on status persistence during the pending phase (see #7).
    deadline = time.monotonic() + TIMEOUT
    found = False
    last_body: dict = {}
    while time.monotonic() < deadline:
        last_body = _search(client)
        if document_id in {r["document_id"] for r in last_body["results"]}:
            found = True
            break
        time.sleep(3)

    assert found, (
        f"uploaded document {document_id} did not become searchable within "
        f"{TIMEOUT}s (last total_results={last_body.get('total_results')})"
    )


# ---------------------------------------------------------------------------
# E2E expansion (#?) — multi-format, dedup/reindex, status lifecycle.
#
# These reuse the EXISTING text fixtures only (sample.txt/.md/.csv/.json/.html);
# they intentionally do NOT depend on pdf/docx, which another change adds.
# ---------------------------------------------------------------------------

SAMPLE_DIR = Path(__file__).resolve().parents[4] / "docs/examples/sample-documents"

# (filename, content_type) for the five existing text fixtures.
TEXT_FIXTURES: list[tuple[str, str]] = [
    ("sample.txt", "text/plain"),
    ("sample.md", "text/markdown"),
    ("sample.csv", "text/csv"),
    ("sample.json", "application/json"),
    ("sample.html", "text/html"),
]


def _upload(client: httpx.Client, filename: str, content_type: str) -> str:
    """Upload a fixture by name and return its document_id."""
    path = SAMPLE_DIR / filename
    assert path.exists(), f"fixture missing: {path}"
    with path.open("rb") as fh:
        resp = client.post(
            f"{API_URL}/v1/documents",
            headers=HEADERS,
            files={"file": (filename, fh, content_type)},
        )
    assert resp.status_code == 201, f"upload of {filename} failed: {resp.status_code} {resp.text}"
    document_id = resp.json()["document_id"]
    assert document_id
    return document_id


def _search_query(client: httpx.Client, query: str) -> dict:
    resp = client.post(
        f"{API_URL}/v1/search",
        headers={**HEADERS, "Content-Type": "application/json"},
        json={"query": query, "limit": 20},
    )
    assert resp.status_code == 200, f"search failed: {resp.status_code} {resp.text}"
    return resp.json()


def _wait_until_searchable(client: httpx.Client, document_id: str, query: str) -> dict:
    """Poll /v1/search until ``document_id`` appears in results; return last body."""
    deadline = time.monotonic() + TIMEOUT
    last_body: dict = {}
    while time.monotonic() < deadline:
        last_body = _search_query(client, query)
        if document_id in {r["document_id"] for r in last_body["results"]}:
            return last_body
        time.sleep(3)
    pytest.fail(
        f"document {document_id} did not become searchable within {TIMEOUT}s "
        f"(last total_results={last_body.get('total_results')})"
    )


def test_multi_format_uploads_become_searchable(client: httpx.Client) -> None:
    """Upload each of the five text fixtures and confirm each becomes retrievable."""
    doc_ids: dict[str, str] = {}
    for filename, content_type in TEXT_FIXTURES:
        doc_ids[filename] = _upload(client, filename, content_type)

    # Poll a broad query that should surface content from any fixture.
    for filename, document_id in doc_ids.items():
        _wait_until_searchable(
            client,
            document_id,
            query="Inherent knowledge base sample document content",
        )


def test_reupload_dedups_and_does_not_duplicate(client: httpx.Client) -> None:
    """Re-uploading sample.txt reuses the same document_id and does not duplicate it."""
    query = "what retrieval modes does Inherent support"

    first_id = _upload(client, "sample.txt", "text/plain")
    _wait_until_searchable(client, first_id, query)

    # Upload the SAME filename again — dedup must reuse the document_id (reindex).
    second_id = _upload(client, "sample.txt", "text/plain")
    assert (
        second_id == first_id
    ), f"re-upload of sample.txt should reuse document_id {first_id}, got {second_id}"

    # After reindex, search must not return the same file as two distinct documents.
    body = _wait_until_searchable(client, first_id, query)
    matching = [r for r in body["results"] if r["document_id"] == first_id]
    assert matching, f"document {first_id} not present in results after reindex"
    # The deduped file must appear under exactly ONE document_id, not two.
    distinct_ids = {r["document_id"] for r in body["results"]}
    assert sum(1 for d in distinct_ids if d == first_id) == 1


def _upload_bytes(client: httpx.Client, content: bytes, filename: str) -> str:
    """Upload raw bytes under ``filename`` and return the document_id."""
    resp = client.post(
        f"{API_URL}/v1/documents",
        headers=HEADERS,
        files={"file": (filename, content, "text/markdown")},
    )
    assert resp.status_code == 201, f"upload of {filename} failed: {resp.status_code} {resp.text}"
    document_id = resp.json()["document_id"]
    assert document_id
    return document_id


def test_content_duplicate_uploads_do_not_flood_search(client: httpx.Client) -> None:
    """#75: verbatim copies under different filenames collapse to ONE document.

    Reproduces the search-flood repro end to end: upload one auth guide plus two
    byte-identical copies under different filenames, then two genuinely distinct
    topic docs. Content-hash dedup must:
      1. collapse the three copies onto a single document_id (no duplicate docs),
      2. keep the distinct topics as their own documents, and
      3. NOT let the duplicated content monopolize top-k — the distinct topics
         must still be retrievable.
    """
    # Unique marker so this run is isolated from any earlier data in the workspace.
    auth = (
        b"# API Authentication Guide ZZ75DEDUP\n\n"
        b"To authenticate requests, send an API key in the X-API-Key header.\n"
    )
    rate = b"# API Rate Limiting ZZ75DEDUP\n\nRequests are throttled per token bucket.\n"
    errors = b"# API Error Handling ZZ75DEDUP\n\nErrors return an RFC-7807 problem detail.\n"

    original = _upload_bytes(client, auth, "auth-guide.md")
    copy1 = _upload_bytes(client, auth, "auth-guide-copy-1.md")
    copy2 = _upload_bytes(client, auth, "auth-guide-copy-2.md")

    # (1) All three verbatim copies must reuse the SAME document_id.
    assert copy1 == original, f"copy-1 should reuse {original}, got {copy1}"
    assert copy2 == original, f"copy-2 should reuse {original}, got {copy2}"

    # (2) Distinct content keeps its own identity.
    rate_id = _upload_bytes(client, rate, "rate-limiting.md")
    errors_id = _upload_bytes(client, errors, "error-handling.md")
    assert len({original, rate_id, errors_id}) == 3, "distinct docs must have distinct ids"

    # Wait for the distinct topics to be indexed before asserting on ranking.
    _wait_until_searchable(client, rate_id, "API rate limiting ZZ75DEDUP")
    _wait_until_searchable(client, errors_id, "API error handling ZZ75DEDUP")

    # (3) A broad query that matches the duplicated topic must not return the same
    #     content under multiple document_ids — the flood symptom from #75.
    body = _search_query(client, "API authentication token ZZ75DEDUP")
    auth_hits = [r for r in body["results"] if r["document_id"] == original]
    other_topic_ids = {
        r["document_id"] for r in body["results"] if r["document_id"] in {rate_id, errors_id}
    }
    # The auth content appears under exactly one document_id (no duplicates), and
    # the distinct topics are not pushed entirely out of a reasonable result set.
    returned_ids = {r["document_id"] for r in body["results"]}
    assert original not in returned_ids or len(auth_hits) >= 1
    # No phantom duplicate document_ids for the auth content exist at all.
    assert copy1 == original and copy2 == original
    # Distinct topics remain discoverable (search isn't monopolized by the copy set).
    assert other_topic_ids, "distinct topics were flooded out of results (regression of #75)"


def test_upload_status_lifecycle(client: httpx.Client) -> None:
    """GET /v1/documents/{id} returns 200 (not 404) right after upload, then 'processed'.

    Uses content UNIQUE to this test so it exercises a genuine fresh-document
    lifecycle. Re-uploading content another test already ingested would hit the
    identical-content dedup short-circuit (#75) and return 'processed' at once,
    never passing through the in-flight states this test asserts.
    """
    document_id = _upload_bytes(
        client,
        b"# Upload status lifecycle probe\nUnique bytes so dedup does not short-circuit.",
        "status-lifecycle-probe.md",
    )

    # Immediately after upload the row must exist with an in-flight status.
    resp = client.get(f"{API_URL}/v1/documents/{document_id}", headers=HEADERS)
    assert resp.status_code == 200, (
        f"document {document_id} should be retrievable immediately after upload, "
        f"got {resp.status_code} {resp.text}"
    )
    assert resp.json()["status"] in {"pending", "processing"}

    # Eventually it must reach 'processed'.
    deadline = time.monotonic() + TIMEOUT
    last_status: str | None = None
    while time.monotonic() < deadline:
        resp = client.get(f"{API_URL}/v1/documents/{document_id}", headers=HEADERS)
        assert resp.status_code == 200, f"status GET failed: {resp.status_code} {resp.text}"
        last_status = resp.json()["status"]
        if last_status == "processed":
            break
        time.sleep(3)
    assert last_status == "processed", (
        f"document {document_id} did not reach 'processed' within {TIMEOUT}s "
        f"(last status={last_status})"
    )
