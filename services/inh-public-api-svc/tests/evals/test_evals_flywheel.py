"""Compose-backed E2E proof of the evals flywheel (milestone evidence, task 12).

Exercises the whole feature end to end against a live local stack: search
capture (event_id) -> feedback -> promotion to an eval case -> scorecard ->
a mode-comparison run -> purge (cases survive). This is the "does the whole
loop actually work" test the individual REST-endpoint tests do not cover.

Marked ``compose`` + ``retrieval_eval`` like the sibling ranking-regression
test; deselected by default. Run against a live stack with:
``make dev`` then ``uv run pytest tests/evals/test_evals_flywheel.py -m compose -v``.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import httpx
import pytest

pytestmark = [pytest.mark.compose, pytest.mark.retrieval_eval]

API_URL = os.environ.get("PUBLIC_API_URL", "http://localhost:18000").rstrip("/")
API_KEY = os.environ.get("INTEGRATION_API_KEY", "ink_dev_local_key_001")
WORKSPACE_ID = os.environ.get("INTEGRATION_WORKSPACE_ID", "ws_local_001")
TIMEOUT = int(os.environ.get("INTEGRATION_TIMEOUT", "180"))
RUN_TIMEOUT = int(os.environ.get("EVAL_RUN_TIMEOUT", "60"))
HEADERS = {"X-API-Key": API_KEY, "X-Workspace-Id": WORKSPACE_ID}

REPO_ROOT = Path(__file__).resolve().parents[4]
SAMPLE_DIR = REPO_ROOT / "docs" / "examples" / "sample-documents"
QUERY = "what retrieval modes does Inherent support"
MODES = ("keyword", "semantic", "hybrid")


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


def _content_type(filename: str) -> str:
    return {
        ".txt": "text/plain",
        ".md": "text/markdown",
    }.get(os.path.splitext(filename)[1], "application/octet-stream")


def _upload(client: httpx.Client, filename: str) -> str:
    path = SAMPLE_DIR / filename
    assert path.exists(), f"missing fixture {path}"
    with path.open("rb") as fh:
        resp = client.post(
            f"{API_URL}/v1/documents",
            headers=HEADERS,
            files={"file": (filename, fh, _content_type(filename))},
        )
    assert resp.status_code == 201, f"upload {filename} failed: {resp.status_code} {resp.text}"
    return resp.json()["document_id"]


def _wait_processed(client: httpx.Client, document_id: str) -> None:
    deadline = time.monotonic() + TIMEOUT
    while time.monotonic() < deadline:
        resp = client.get(f"{API_URL}/v1/documents/{document_id}", headers=HEADERS)
        assert resp.status_code == 200, f"get document failed: {resp.status_code} {resp.text}"
        if resp.json().get("status") == "processed":
            return
        time.sleep(3)
    pytest.fail(f"document {document_id} not processed within {TIMEOUT}s")


def _search(client: httpx.Client, query: str, mode: str = "hybrid", limit: int = 5) -> dict:
    resp = client.post(
        f"{API_URL}/v1/search",
        headers={**HEADERS, "Content-Type": "application/json"},
        json={"query": query, "limit": limit, "search_mode": mode},
    )
    assert resp.status_code == 200, f"search failed: {resp.status_code} {resp.text}"
    return resp.json()


def test_flywheel_capture_to_replay(client):
    """Full loop: capture -> feedback -> promotion -> scorecard -> run -> purge."""
    # 1. Upload docs/examples/sample-documents/sample.txt + sample.md; poll
    # until each is processed. Both mention "retrieval modes" so the probe
    # query below is guaranteed to find real results once indexed.
    for filename in ("sample.txt", "sample.md"):
        doc_id = _upload(client, filename)
        _wait_processed(client, doc_id)

    # 2. POST /v1/search -> assert response carries a non-null event_id
    # (capture is on by default; this is what the trial script/agents key
    # feedback off of).
    search_body = _search(client, QUERY, mode="hybrid")
    event_id = search_body.get("event_id")
    assert event_id, f"expected non-null event_id, got search response: {search_body}"
    results = search_body["results"]
    assert results, "expected at least one search result to label as useful"
    first_chunk_id = results[0]["chunk_id"]

    # 3. POST /v1/evals/feedback -> promoted is True (positive verdict + a
    # useful chunk promotes the captured event into a labeled eval case).
    fb_resp = client.post(
        f"{API_URL}/v1/evals/feedback",
        headers={**HEADERS, "Content-Type": "application/json"},
        json={
            "event_id": event_id,
            "verdict": "answered",
            "useful_chunk_ids": [first_chunk_id],
        },
    )
    assert fb_resp.status_code == 200, f"feedback failed: {fb_resp.status_code} {fb_resp.text}"
    fb_body = fb_resp.json()
    assert fb_body["promoted"] is True, f"expected promotion, got: {fb_body}"

    # 4. GET /v1/evals/scorecard -> the day-one verdict surface reflects the
    # feedback we just filed: at least one labeled case, a non-empty human
    # readable summary, low_confidence True (tiny sample vs min_sample_size),
    # and at least one recorded feedback event.
    scorecard = client.get(f"{API_URL}/v1/evals/scorecard", headers=HEADERS)
    assert (
        scorecard.status_code == 200
    ), f"scorecard failed: {scorecard.status_code} {scorecard.text}"
    sc_body = scorecard.json()
    assert sc_body["eval_case_count"] >= 1, sc_body
    assert sc_body["summary"], "expected a non-empty human-readable summary"
    assert sc_body["low_confidence"] is True, sc_body
    assert sc_body["feedback_count"] >= 1, sc_body

    # 5. POST /v1/evals/runs -> 202 with a run_id (replay executes as a
    # background task).
    run_start = client.post(f"{API_URL}/v1/evals/runs", headers=HEADERS)
    assert (
        run_start.status_code == 202
    ), f"start run failed: {run_start.status_code} {run_start.text}"
    run_id = run_start.json()["run_id"]
    assert run_id

    # 6. Poll GET /v1/evals/runs/{run_id} until status == "completed".
    deadline = time.monotonic() + RUN_TIMEOUT
    report = None
    while time.monotonic() < deadline:
        run_resp = client.get(f"{API_URL}/v1/evals/runs/{run_id}", headers=HEADERS)
        assert (
            run_resp.status_code == 200
        ), f"get run failed: {run_resp.status_code} {run_resp.text}"
        report = run_resp.json()
        status = report["run"]["status"]
        if status == "completed":
            break
        assert status != "failed", f"eval run failed: {report}"
        time.sleep(2)
    else:
        pytest.fail(f"eval run {run_id} not completed within {RUN_TIMEOUT}s")

    # 7. Aggregates cover all three modes, each metric in [0, 1]; per_case has
    # 3 rows (1 promoted case x 3 modes) carrying the case's query text.
    aggregates = report["run"]["aggregates"]
    assert set(aggregates) == set(MODES), f"expected all three modes, got: {aggregates}"
    for mode, metrics in aggregates.items():
        for field in ("recall_at_k", "mrr", "ndcg_at_k"):
            value = metrics[field]
            assert 0 <= value <= 1, f"{mode}.{field}={value} out of [0,1]: {aggregates}"

    per_case = report["per_case"]
    assert len(per_case) == 3, f"expected 3 rows (1 case x 3 modes), got: {per_case}"
    for row in per_case:
        assert row["query_text"] == QUERY, row

    # 8. The promoted case's expected doc must be findable: hybrid recall > 0.
    assert (
        aggregates["hybrid"]["recall_at_k"] > 0
    ), f"expected hybrid recall_at_k > 0 for the promoted case, got: {aggregates}"

    # 9. DELETE /v1/evals/events -> 200; scorecard's captured_events drops to
    # 0 while eval_case_count stays >= 1 (labeled cases survive the purge —
    # only raw, unlabeled query events are deleted).
    purge = client.delete(f"{API_URL}/v1/evals/events", headers=HEADERS)
    assert purge.status_code == 200, f"purge failed: {purge.status_code} {purge.text}"

    post_purge = client.get(f"{API_URL}/v1/evals/scorecard", headers=HEADERS)
    assert post_purge.status_code == 200, post_purge.text
    post_purge_body = post_purge.json()
    assert post_purge_body["captured_events"] == 0, post_purge_body
    assert post_purge_body["eval_case_count"] >= 1, post_purge_body
