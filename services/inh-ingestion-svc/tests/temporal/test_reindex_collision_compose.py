"""Real-server proof that two genuinely colliding DocumentIngestionWorkflow
runs resolve correctly end to end (#110, follow-up review blocker 1).

The unit-level tests (tests/test_temporal_trigger.py,
tests/test_reindex_fencing.py, tests/test_temporal_activities.py) prove the
pieces in isolation with mocks: that start_workflow is called with
id_conflict_policy=TERMINATE_EXISTING, and that the fencing SQL rejects a
stale write given a canned sequence of calls. Neither proves the actual
Temporal SERVER resolves a same-id collision the way the code assumes, and
neither can reproduce the real race (a slow run's activity landing AFTER a
fast run's commit) end to end through the real ingestion pipeline. This file
does, against a live compose stack:

1. Upload a document, wait for it to process.
2. Fire two rapid edited-content re-uploads under the same filename (same
   document_id -> same fixed Temporal workflow id, "ingest-{document_id}").
   The first is very likely still mid-pipeline when the second arrives --
   TERMINATE_EXISTING (src/temporal/trigger.py) terminates it and starts
   fresh, exactly the scenario blocker 1 flagged: termination does not stop
   the first run's already-dispatched store activity (no heartbeat/
   cancellation is wired anywhere in this service), so without the fencing
   check in DatabaseService.store_processed_document / is_active_run
   (services/database.py, migration 016) that abandoned activity's late
   write could silently revert the document to the OLDER content after the
   newer content already looked "processed".
3. Assert the FINAL content is the LAST upload's -- checked once right after
   the document settles, and again after a buffer generous enough for the
   superseded run's slower store step to have completed. That second check
   is what would have caught blocker 1: without fencing, content flips back
   to the stale version once the abandoned write lands.

Uses the public API (host-mapped port 18000) as the entry point, the same
way tests/benchmark/test_ingestion_throughput.py and
services/inh-public-api-svc/tests/evals/test_evals_flywheel.py do -- NOT a
direct Postgres connection from the test process: this repo's .env.example
DATABASE_URL uses the compose-internal hostname ("postgres"), which is not
resolvable from a test process running on the host/CI-runner outside the
compose network (see docker-compose.yml). Going through the real API also
means this test exercises the actual MQ trigger path (#110's other half),
not just the workflow layer.

Marked `compose`; skip-guarded if the public API is not reachable, same
convention as the files referenced above. NOT executed in this sandbox (no
compose stack, and the sandbox's proxy would in any case block reaching a
real cluster the same way it blocks temporal.download per
tests/temporal/test_audit_workflow.py's docstring) -- authored and reviewed
for correctness; CI's `make test-integration` / integration.yml is this
file's first live run.
"""

from __future__ import annotations

import os
import time
import uuid

import httpx
import pytest

pytestmark = pytest.mark.compose

API_URL = os.environ.get("PUBLIC_API_URL", "http://localhost:18000").rstrip("/")
API_KEY = os.environ.get("INTEGRATION_API_KEY", "ink_dev_local_key_001")
WORKSPACE_ID = os.environ.get("INTEGRATION_WORKSPACE_ID", "ws_local_001")
HEADERS = {"X-API-Key": API_KEY, "X-Workspace-Id": WORKSPACE_ID}
TIMEOUT = int(os.environ.get("INTEGRATION_TIMEOUT", "180"))
# How long to wait after the document first looks "processed" before
# re-checking -- must comfortably exceed how long a superseded run's own
# store step could still be running (dominated by the embedding batch call).
POST_SETTLE_BUFFER_SECONDS = int(os.environ.get("REINDEX_RACE_BUFFER_SECONDS", "20"))


@pytest.fixture(scope="module")
def client() -> httpx.Client:
    with httpx.Client(timeout=30) as c:
        try:
            resp = c.get(f"{API_URL}/health", timeout=5)
        except httpx.HTTPError as exc:
            pytest.skip(f"public API not reachable at {API_URL}: {exc}")
        if resp.status_code != 200:
            pytest.skip(f"public API unhealthy: HTTP {resp.status_code}")
        yield c


def _upload(client: httpx.Client, filename: str, content: bytes) -> str:
    resp = client.post(
        f"{API_URL}/v1/documents",
        headers=HEADERS,
        files={"file": (filename, content, "text/plain")},
    )
    assert resp.status_code == 201, f"upload {filename} failed: {resp.status_code} {resp.text}"
    return resp.json()["document_id"]


def _wait_processed(client: httpx.Client, document_id: str, timeout: int = TIMEOUT) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = client.get(f"{API_URL}/v1/documents/{document_id}", headers=HEADERS)
        assert resp.status_code == 200, f"get document failed: {resp.status_code} {resp.text}"
        body = resp.json()
        if body.get("status") == "processed":
            return body
        time.sleep(2)
    pytest.fail(f"document {document_id} not processed within {timeout}s")


def _full_text(client: httpx.Client, document_id: str) -> str:
    resp = client.get(f"{API_URL}/v1/chunks/{document_id}/context", headers=HEADERS)
    assert resp.status_code == 200, f"get context failed: {resp.status_code} {resp.text}"
    return resp.json()["full_text"]


def test_stale_reindex_does_not_clobber_a_newer_one(client: httpx.Client):
    """(#110 blocker 1) Two rapid edited-content re-uploads race the same
    document_id's fixed Temporal workflow id. The LAST upload's content must
    survive -- including after a buffer long enough for the superseded first
    run's abandoned store activity to have finished."""
    filename = f"reindex-race-{uuid.uuid4().hex}.txt"

    doc_id = _upload(client, filename, b"VERSION-1 original content, nothing racing yet")
    _wait_processed(client, doc_id)

    # Fire two edited re-uploads back to back, same filename -> reindex in
    # place (see docs/keeping-content-current.md), same document_id, same
    # fixed workflow id "ingest-{document_id}" (src/temporal/trigger.py). No
    # wait between them -- the first is (very likely) still mid-pipeline
    # when the second arrives, which is exactly the collision #110's
    # TERMINATE_EXISTING (and this fencing fix) needs to resolve correctly.
    _upload(client, filename, b"VERSION-2 stale content that must NOT win the race")
    doc_id_2 = _upload(client, filename, b"VERSION-3 final content that MUST win the race")
    assert doc_id_2 == doc_id, "reindex-in-place must reuse the same document_id"

    body = _wait_processed(client, doc_id)
    assert body.get("chunk_count", 0) >= 1

    text = _full_text(client, doc_id)
    assert (
        "VERSION-3" in text
    ), f"expected the LAST re-upload's content to have won, got: {text[:200]!r}"
    assert "VERSION-2" not in text

    # The regression this pins: pre-fencing, VERSION-2's terminated run kept
    # its store activity running (termination stops the WORKFLOW, not an
    # already-dispatched ACTIVITY -- no heartbeat/cancellation is wired), and
    # that activity's late write could land AFTER VERSION-3 already looked
    # "processed", silently reverting the document. Re-check after a buffer
    # generous enough for that late write to have landed if it were going to.
    time.sleep(POST_SETTLE_BUFFER_SECONDS)

    text_after_buffer = _full_text(client, doc_id)
    assert "VERSION-3" in text_after_buffer, (
        "content reverted after a buffer -- a superseded run's stale write "
        "landed late and clobbered the newer content (#110 blocker 1)"
    )
    assert "VERSION-2" not in text_after_buffer

    # Also confirm chunk_count didn't quietly change either -- a partial
    # clobber (e.g. Weaviate reverted but Postgres didn't, or vice versa)
    # would still be a real bug even if full_text happened to still match.
    body_after_buffer = client.get(f"{API_URL}/v1/documents/{doc_id}", headers=HEADERS).json()
    assert body_after_buffer["chunk_count"] == body["chunk_count"]
