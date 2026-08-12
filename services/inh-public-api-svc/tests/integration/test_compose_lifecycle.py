"""Live document-lifecycle and binary-format E2E (upload → delete → refresh → chunks).

What this file adds over ``test_compose_integration.py``: that file proves the
*happy* half of a document's life -- bytes go in, search finds them. Everything
after that point is untested against a running stack. This covers the rest:

- **Delete really removes content.** Every other delete assertion in the repo
  is either offline (mocked stores) or a cross-tenant *refusal*
  (``test_compose_tenancy.py``). Nothing checked that an owner's ``DELETE``
  actually evicts the document from the *vector index*, which is a different
  store from the Postgres row and is deleted by a different code path
  (``delete_document_everywhere``). A delete that dropped the row but left the
  vectors would look perfect from ``GET /v1/documents/{id}`` and keep serving
  the deleted content to every search -- the worst shape of a data-deletion
  bug, and invisible to a 404-only assertion.
- **Real binary formats.** Every live format test so far uploads text the test
  itself constructed. PDF and DOCX ingest through completely different code
  (``_extract_pdf_text`` / ``_extract_docx_text``, both third-party parsers);
  an extractor regression there is invisible to a suite that only ever uploads
  UTF-8.
- **Refresh.** ``POST /v1/documents/{id}/refresh`` (#42) has no live coverage
  at all: it rebuilds the original ``document.uploaded`` event from the stored
  row and republishes it, so it exercises the S3 object, the MQ publish, and
  the pending-row reset -- three things a unit test necessarily fakes.
- **Chunk-size bounds for tabular data.** See
  ``test_xlsx_chunks_stay_within_bounds`` for why this one is not an xfail.

Fixtures are the ``e2e-*`` files in ``docs/examples/sample-documents/``, added
by this change. They are deliberately NOT the pre-existing
``sample.pdf``/``sample.docx``/``sample.xlsx``: those are pinned character-for-
character by ``services/inh-ingestion-svc/tests/test_extraction_by_type.py``
(exact sheet names, the ``[merged A1:D1]`` marker, the header row) and feed the
extraction/chunking quality eval corpus, so rewriting them to carry sentinels
and 500 rows would have broken offline tests and moved eval baselines.

This test is marked ``compose`` and is deselected by the default pytest run
(see ``addopts`` in pyproject). Run it against a live stack with::

    make dev            # or: make quickstart
    uv run pytest tests/integration/test_compose_lifecycle.py -v --no-cov

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
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

pytestmark = [pytest.mark.compose, pytest.mark.integration, pytest.mark.slow]

API_URL = os.environ.get("PUBLIC_API_URL", "http://localhost:18000").rstrip("/")
API_KEY = os.environ.get("INTEGRATION_API_KEY", "ink_dev_local_key_001")
WORKSPACE_ID = os.environ.get("INTEGRATION_WORKSPACE_ID", "ws_local_001")
TIMEOUT = int(os.environ.get("INTEGRATION_TIMEOUT", "180"))

HEADERS = {"X-API-Key": API_KEY, "X-Workspace-Id": WORKSPACE_ID}

# repo root: tests/integration/<file> -> parents[4]
SAMPLE_DIR = Path(__file__).resolve().parents[4] / "docs/examples/sample-documents"

PDF_FIXTURE = "e2e-lifecycle.pdf"
DOCX_FIXTURE = "e2e-lifecycle.docx"
XLSX_FIXTURE = "e2e-tabular.xlsx"

PDF_CONTENT_TYPE = "application/pdf"
DOCX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

# Sentinels baked into the committed fixtures. Unlike the per-run uuid used for
# the delete probe below, these are FIXED: the fixture bytes are committed, so
# content-hash dedup (#75) makes a re-run reuse the same document_id instead of
# growing the workspace by one document per CI run.
PDF_SENTINEL = "ZZE2EPDFQUOKKA"
DOCX_SENTINEL = "ZZE2EDOCXNARWHAL"
XLSX_SENTINEL = "ZZE2EXLSXPANGOLIN"

PDF_QUERY = f"quokka telemetry digest {PDF_SENTINEL} harbour crossings winter survey"
DOCX_QUERY = f"narwhal acoustics ledger {DOCX_SENTINEL} deep dive pings hydrophone array"

# The loose bound for the tabular-chunking assertion. Not the real cap
# (``_token_budget_char_cap`` derives ~1538 chars from EMBEDDING_MAX_TOKENS at
# the compose defaults) -- deliberately far above it, so this test pins the
# defect CLASS ("the sheet came back as one undivided blob") and does not turn
# red every time chunk sizing is retuned. The defect it guards produced a
# 28,344-character chunk from this exact fixture.
MAX_CHUNK_CHARS = 8000


def _require_stack(client: httpx.Client) -> None:
    """Skip (don't fail) when no healthy stack is reachable."""
    try:
        resp = client.get(f"{API_URL}/health", timeout=5)
    except httpx.HTTPError as exc:
        pytest.skip(f"public API not reachable at {API_URL}: {exc}")
    if resp.status_code != 200:
        pytest.skip(f"public API unhealthy at {API_URL}: HTTP {resp.status_code}")


@pytest.fixture(scope="module")
def client() -> Iterator[httpx.Client]:
    with httpx.Client(timeout=30) as c:
        _require_stack(c)
        yield c


def _search(client: httpx.Client, query: str) -> dict:
    resp = client.post(
        f"{API_URL}/v1/search",
        headers={**HEADERS, "Content-Type": "application/json"},
        json={"query": query, "limit": 20},
    )
    assert resp.status_code == 200, f"search failed: {resp.status_code} {resp.text}"
    return resp.json()


def _upload_file(client: httpx.Client, filename: str, content_type: str) -> str:
    """Upload a committed fixture by name; return its document_id."""
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


def _upload_bytes(client: httpx.Client, content: bytes, filename: str, content_type: str) -> str:
    """Upload raw bytes under ``filename``; return the document_id."""
    resp = client.post(
        f"{API_URL}/v1/documents",
        headers=HEADERS,
        files={"file": (filename, content, content_type)},
    )
    assert resp.status_code == 201, f"upload of {filename} failed: {resp.status_code} {resp.text}"
    document_id = resp.json()["document_id"]
    assert document_id
    return document_id


def _await_searchable(client: httpx.Client, document_id: str, query: str, label: str) -> dict:
    """Poll ``/v1/search`` until ``document_id`` is returned; return the last body."""
    deadline = time.monotonic() + TIMEOUT
    last: dict = {}
    while time.monotonic() < deadline:
        last = _search(client, query)
        if document_id in {r["document_id"] for r in last["results"]}:
            return last
        time.sleep(3)
    pytest.fail(
        f"{label} ({document_id}) did not become searchable within {TIMEOUT}s "
        f"(last total_results={last.get('total_results')})"
    )


def _await_status(client: httpx.Client, document_id: str, wanted: str, label: str) -> dict:
    """Poll ``GET /v1/documents/{id}`` until ``status == wanted``; return the body."""
    deadline = time.monotonic() + TIMEOUT
    last: dict = {}
    while time.monotonic() < deadline:
        resp = client.get(f"{API_URL}/v1/documents/{document_id}", headers=HEADERS)
        assert resp.status_code == 200, f"{label} status GET failed: {resp.status_code} {resp.text}"
        last = resp.json()
        if last["status"] == wanted:
            return last
        if last["status"] == "failed":
            pytest.fail(f"{label} ({document_id}) ingestion FAILED: {last}")
        time.sleep(3)
    pytest.fail(
        f"{label} ({document_id}) did not reach '{wanted}' within {TIMEOUT}s "
        f"(last status={last.get('status')})"
    )


def _delete(client: httpx.Client, document_id: str) -> None:
    resp = client.delete(f"{API_URL}/v1/documents/{document_id}", headers=HEADERS)
    assert (
        resp.status_code == 204
    ), f"DELETE of {document_id} returned {resp.status_code}, expected 204: {resp.text}"


def test_upload_delete_gone_from_search_and_404(client: httpx.Client) -> None:
    """A deleted document disappears from BOTH the metadata store and search.

    The two assertions are not redundant -- they interrogate two different
    stores written by two different branches of ``delete_document_everywhere``:
    the 404 proves the Postgres row is gone, and the empty search proves the
    Weaviate objects are gone. A delete that removed the row but left the
    vectors would pass a 404-only test forever while every search kept serving
    content the user believes they erased.

    The document carries a fresh uuid, so the sentinel exists in exactly one
    document in the world at the moment it is asserted on -- a stale copy from
    an earlier run cannot make "the content is gone" fail, and a search index
    that ignores deletes cannot pass by returning nothing for an unrelated
    reason.
    """
    sentinel = f"ZZDELETE{uuid.uuid4().hex.upper()}"
    query = f"delete lifecycle probe {sentinel}"
    document_id = _upload_bytes(
        client,
        (
            f"# Delete lifecycle probe {sentinel}\n\n"
            f"This document exists to be deleted. Sentinel: {sentinel}.\n"
        ).encode("utf-8"),
        f"delete-probe-{sentinel}.md",
        "text/markdown",
    )

    # Positive control: it has to be findable before "it is gone" means anything.
    before = _await_searchable(client, document_id, query, "delete probe")
    assert any(
        sentinel in (r.get("content") or "") for r in before["results"]
    ), f"probe indexed but its sentinel is absent from the retrieved content: {before}"

    _delete(client, document_id)

    # Metadata store: gone immediately (the row delete is transactional and
    # synchronous inside the request), so this is asserted without polling --
    # if it ever needs a retry loop, that is itself the finding.
    resp = client.get(f"{API_URL}/v1/documents/{document_id}", headers=HEADERS)
    assert resp.status_code == 404, (
        f"GET after DELETE returned {resp.status_code}, expected 404 "
        f"(the document row survived its own deletion): {resp.text}"
    )

    # Vector store: polled, because eviction is only as synchronous as
    # Weaviate's own indexing. Exceeding TIMEOUT here is a real finding
    # (deleted content still being served), not a flaky test.
    deadline = time.monotonic() + TIMEOUT
    last: dict = {}
    while time.monotonic() < deadline:
        last = _search(client, query)
        hits = [
            r
            for r in last["results"]
            if r["document_id"] == document_id or sentinel in (r.get("content") or "")
        ]
        if not hits:
            break
        time.sleep(3)
    else:
        pytest.fail(
            f"deleted document {document_id} / sentinel {sentinel} was still retrievable "
            f"{TIMEOUT}s after a 204 DELETE -- search is serving deleted content: {last}"
        )

    # Repeat delete is a 404, not a 500 or a silent 204: the endpoint documents
    # "repeating the delete returns 404 (the document is already gone)".
    repeat = client.delete(f"{API_URL}/v1/documents/{document_id}", headers=HEADERS)
    assert (
        repeat.status_code == 404
    ), f"repeat DELETE returned {repeat.status_code}, expected 404: {repeat.text}"


# Smoke: PDF is the single binary format most likely to be uploaded first by a
# real user and the one whose extractor is a third-party parser rather than a
# decode() call, so a regression in it is both likely and invisible to every
# text-only test. DOCX below stays full-lane -- it is the same class of proof
# through a different library, which is worth having but not worth paying for
# on every pull request.
@pytest.mark.smoke
def test_pdf_becomes_searchable(client: httpx.Client) -> None:
    """A real PDF's text survives extraction → chunking → embedding → retrieval.

    Asserts the sentinel comes back in the retrieved CONTENT, not merely that
    the document_id appears: an extractor that returned an empty string would
    still produce a document that search can name (title/filename signal), and
    a document_id-only assertion would call that a pass while the PDF's actual
    text was never indexed at all.
    """
    document_id = _upload_file(client, PDF_FIXTURE, PDF_CONTENT_TYPE)
    body = _await_searchable(client, document_id, PDF_QUERY, "PDF fixture")
    contents = " ".join(r.get("content") or "" for r in body["results"])
    assert PDF_SENTINEL in contents, (
        f"PDF {document_id} is retrievable but its extracted text is not -- the sentinel "
        f"{PDF_SENTINEL} is absent from every returned chunk: {body}"
    )


def test_docx_becomes_searchable(client: httpx.Client) -> None:
    """Same proof as the PDF case, through python-docx's separate code path."""
    document_id = _upload_file(client, DOCX_FIXTURE, DOCX_CONTENT_TYPE)
    body = _await_searchable(client, document_id, DOCX_QUERY, "DOCX fixture")
    contents = " ".join(r.get("content") or "" for r in body["results"])
    assert DOCX_SENTINEL in contents, (
        f"DOCX {document_id} is retrievable but its extracted text is not -- the sentinel "
        f"{DOCX_SENTINEL} is absent from every returned chunk: {body}"
    )


def test_refresh_document_flow(client: httpx.Client) -> None:
    """``POST /v1/documents/{id}/refresh`` re-ingests an UPLOADED document.

    Worth pinning live because the endpoint's contract is easy to get wrong in
    both directions. It does NOT need a source URI -- the docstring is explicit
    that it "does NOT re-upload bytes, it only re-triggers processing of the
    already-stored file" -- so an uploaded document is a first-class refresh
    target, and a 400/422 demanding a source would be a regression, not the
    documented behavior. What it returns is a ``DocumentUploadResponse`` whose
    ``status`` is ``pending``: the row is reset before the MQ publish, so the
    response describes the *newly enqueued* state, not the old terminal one.

    The document then has to come back to ``processed`` on its own. That round
    trip is the part no unit test can fake: it means the S3 object referenced
    by ``storage_path`` still existed, the MQ publish landed, and the worker
    re-ran extraction over the stored bytes.
    """
    sentinel = f"ZZREFRESH{uuid.uuid4().hex.upper()}"
    document_id = _upload_bytes(
        client,
        (
            f"# Refresh probe {sentinel}\n\n"
            f"Unique bytes so content dedup does not short-circuit ingestion. "
            f"Sentinel: {sentinel}.\n"
        ).encode("utf-8"),
        f"refresh-probe-{sentinel}.md",
        "text/markdown",
    )
    first = _await_status(client, document_id, "processed", "refresh probe")
    assert first["chunk_count"] >= 1, f"probe processed with no chunks: {first}"

    resp = client.post(f"{API_URL}/v1/documents/{document_id}/refresh", headers=HEADERS)
    assert resp.status_code == 200, (
        f"refresh returned {resp.status_code}, expected 200 -- an uploaded document is a "
        f"valid refresh target (no source URI required): {resp.text}"
    )
    body = resp.json()
    # The documented response shape: same identity, re-queued state.
    assert body["document_id"] == document_id, f"refresh minted a NEW document id: {body}"
    assert body["workspace_id"] == WORKSPACE_ID
    assert body["status"] == "pending", f"refresh response status was not 'pending': {body}"
    assert body["mime_type"] == "text/markdown"
    assert body["message"], "refresh response carried no human-readable message"

    # And the re-ingestion actually completes, with the content still intact.
    after = _await_status(client, document_id, "processed", "refreshed probe")
    assert after["chunk_count"] >= 1, f"document has no chunks after refresh: {after}"

    chunks = client.get(f"{API_URL}/v1/chunks/{document_id}", headers=HEADERS)
    assert chunks.status_code == 200, f"chunk fetch failed: {chunks.status_code} {chunks.text}"
    assert sentinel in chunks.text, (
        f"refresh completed but the document's content is gone from its chunks -- "
        f"re-ingestion replaced them with nothing: {chunks.text[:1000]}"
    )

    _delete(client, document_id)


def test_xlsx_chunks_stay_within_bounds(client: httpx.Client) -> None:
    """A 500-row spreadsheet is chunked into many bounded chunks, not one blob.

    This test was specified as a strict xfail pinning the defect in
    ``docs/architecture/overview.md`` §6.2: an XLSX serializes to pipe-delimited
    rows with no ``.``/``!``/``?``-plus-whitespace anywhere, ``_chunk_by_sentences``
    finds zero split points, its size guard (``if current_size + sentence_len >
    max_size and current``) never fires on a single "sentence", and the whole
    sheet lands as ONE chunk -- which ``embed_texts`` then hands to TEI with
    ``truncate=True``, so everything past the model's input ceiling is silently
    dropped from the vector while remaining in the stored content.

    It is NOT an xfail, because the defect is fixed on this branch and the
    xfail would be strict: #129 (``feat(ingestion): format-aware chunking
    driven by registry chunking_hint``, 7d99cea + 9cc2d29) landed on main after
    §6.2 was written, and ``.xlsx`` now resolves to ``chunking_hint="tabular"``
    → ``_chunk_by_rows``, which never splits a row and slices oversized ones.
    Measured on THIS fixture with the compose defaults: ``_chunk_by_sentences``
    → 2 chunks, largest 28,344 chars (the defect); ``_chunk_by_rows`` → 22
    chunks, largest 1,537. The overview text is stale, not wrong-in-principle
    -- see the report for that finding.

    So this stands as the live regression guard for the fix: if tabular
    chunking is ever reverted, mis-wired, or the registry hint stops resolving,
    the 500-row sheet collapses back into a giant chunk and this FAILS.
    """
    document_id = _upload_file(client, XLSX_FIXTURE, XLSX_CONTENT_TYPE)
    _await_status(client, document_id, "processed", "XLSX fixture")

    resp = client.get(f"{API_URL}/v1/chunks/{document_id}", headers=HEADERS)
    assert resp.status_code == 200, f"chunk fetch failed: {resp.status_code} {resp.text}"
    chunks = resp.json()
    assert chunks, f"XLSX {document_id} processed into zero chunks"

    sizes = [len(c["content"]) for c in chunks]
    # Indexed by chunk_index, not by the row's id: DocumentChunk exposes `id` +
    # `chunk_index` (there is no `chunk_id` field on this response), and the
    # index is the thing that says WHERE in the sheet the blob formed.
    oversized = [(c["chunk_index"], n) for c, n in zip(chunks, sizes) if n > MAX_CHUNK_CHARS]
    assert not oversized, (
        f"tabular chunking emitted {len(oversized)} of {len(chunks)} chunk(s) over "
        f"{MAX_CHUNK_CHARS} chars from a 500-row sheet (largest {max(sizes)}; first "
        f"offenders as (chunk_index, chars): {oversized[:5]}). This is the overview §6.2 "
        f"giant-chunk defect: everything past TEI's input ceiling is silently dropped from "
        f"the vector, so semantic search can only match the leading fragment."
    )
    # The other half of the same defect: 'not oversized' would also be true of a
    # sheet that extracted to almost nothing. A 500-row sheet must produce many
    # chunks, and its far end must still be there.
    assert len(chunks) > 1, f"500-row sheet produced a single chunk: sizes={sizes}"
    assert any(
        XLSX_SENTINEL in c["content"] for c in chunks
    ), f"the sheet's Manifest sentinel {XLSX_SENTINEL} is missing from all {len(chunks)} chunks"
