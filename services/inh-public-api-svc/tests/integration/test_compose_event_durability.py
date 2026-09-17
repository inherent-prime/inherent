"""Live pin for #240: a search's ``event_id`` is durable BEFORE the response.

#240 was a race with a wrong-looking symptom. Search captured its query event
through ``BackgroundTasks`` -- copied from the audit path, which is fire-and-
forget and correctly so -- but capture is different in one decisive way: it
hands the caller a HANDLE. The response carried an ``event_id`` while the row
behind it had not been INSERTed yet, because background tasks run *after* the
response is sent. An agent doing the obvious thing (search, judge the results,
post feedback on the id it was just given) raced the write and got a 404 whose
message blamed the retention window -- which sent the first investigation
looking for an expiry bug that did not exist.

The fix (PR #242) awaits the INSERT and advertises the id only once the row is
durable, so "no ``event_id``" now means "not captured" (actionable) and an
``event_id`` that IS returned is always immediately usable.

That guarantee is only provable end to end. Offline tests drive
``record_query_event`` against a mocked database, where nothing about ordering
between a background task and a serialized HTTP response is observable -- the
race lived in FastAPI's response lifecycle, not in any function under test.
This file is the only place the promise is checked the way an agent actually
experiences it: over the wire, on the very next round trip, with no sleep.

The endpoints are under ``/v1/evals/`` (``src/api/router.py`` mounts
``evals.router`` with its own ``/evals`` prefix): ``POST /v1/evals/feedback``
and ``GET /v1/evals/scorecard``.

This test is marked ``compose`` and is deselected by the default pytest run
(see ``addopts`` in pyproject). Run it against a live stack with::

    make dev            # or: make quickstart
    uv run pytest tests/integration/test_compose_event_durability.py -v --no-cov

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

import httpx
import pytest

pytestmark = [pytest.mark.compose, pytest.mark.integration, pytest.mark.slow]

API_URL = os.environ.get("PUBLIC_API_URL", "http://localhost:18000").rstrip("/")
API_KEY = os.environ.get("INTEGRATION_API_KEY", "ink_dev_local_key_001")
WORKSPACE_ID = os.environ.get("INTEGRATION_WORKSPACE_ID", "ws_local_001")
TIMEOUT = int(os.environ.get("INTEGRATION_TIMEOUT", "180"))

HEADERS = {"X-API-Key": API_KEY, "X-Workspace-Id": WORKSPACE_ID}
JSON_HEADERS = {**HEADERS, "Content-Type": "application/json"}

# Fresh per module run: the seeded document must be the thing this search
# retrieves, and a stale copy from an earlier run would let the test pass on
# somebody else's content.
SENTINEL = f"ZZEVENT{uuid.uuid4().hex.upper()}"
SEED_FILENAME = f"event-durability-probe-{SENTINEL}.md"
SEED_CONTENT = (
    f"# Event durability probe {SENTINEL}\n\n"
    f"This document exists so the capture-and-feedback loop has real search "
    f"results to judge. Sentinel: {SENTINEL}.\n"
)
SEED_QUERY = f"event durability probe {SENTINEL}"


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


def _search(client: httpx.Client, query: str) -> httpx.Response:
    return client.post(
        f"{API_URL}/v1/search",
        headers=JSON_HEADERS,
        json={"query": query, "limit": 5},
    )


@pytest.fixture(scope="module")
def seeded_document(client: httpx.Client) -> Iterator[str]:
    """A document this run's searches are guaranteed to retrieve.

    Feedback on an event with no results is a legitimate but much weaker probe:
    ``submit_feedback`` short-circuits to ``promoted=False`` when nothing was
    returned, so the promotion half of the loop would never execute and a
    regression in it would go unnoticed. Seeding real content keeps the whole
    path -- capture, feedback, promotion to an eval case -- under test.
    """
    resp = client.post(
        f"{API_URL}/v1/documents",
        headers=HEADERS,
        files={"file": (SEED_FILENAME, SEED_CONTENT.encode("utf-8"), "text/markdown")},
    )
    assert resp.status_code == 201, f"seed upload failed: {resp.status_code} {resp.text}"
    document_id = resp.json()["document_id"]

    deadline = time.monotonic() + TIMEOUT
    last: dict = {}
    while time.monotonic() < deadline:
        search = _search(client, SEED_QUERY)
        assert search.status_code == 200, f"seed search failed: {search.status_code} {search.text}"
        last = search.json()
        if document_id in {r["document_id"] for r in last["results"]}:
            break
        time.sleep(3)
    else:
        pytest.fail(
            f"seed document {document_id} did not become searchable within {TIMEOUT}s "
            f"(last total_results={last.get('total_results')})"
        )

    yield document_id

    delete = client.delete(f"{API_URL}/v1/documents/{document_id}", headers=HEADERS)
    assert delete.status_code == 204, f"seed cleanup failed: {delete.status_code} {delete.text}"


# Smoke: the evals flywheel is the product's differentiator, and its first link
# is exactly one HTTP hop wide -- "is the id you were just handed real?". The
# regression it guards shipped once already (#240) and was invisible to the
# entire offline suite, which is the definition of something the merge gate
# should be paying for.
@pytest.mark.smoke
def test_search_event_id_is_usable_on_the_next_request(
    client: httpx.Client, seeded_document: str
) -> None:
    """search → feedback with NO delay → 200, and the scorecard counts it.

    Deliberately written with no sleep, no retry and no polling between the
    search and the feedback POST. The retry that would make this test robust is
    precisely the workaround #240 forced on callers, so adding one here would
    re-hide the bug: the point is that the immediate, obvious call sequence
    works.

    The scorecard read afterwards is the second half. Feedback returning 200
    proves the event row was found; it does not prove the feedback was stored
    anywhere an operator can see. The scorecard is that surface, so this
    asserts its counters actually moved -- taken as a before/after delta rather
    than absolute values, because this workspace accumulates events from every
    other compose test in the suite.
    """
    before = client.get(f"{API_URL}/v1/evals/scorecard", headers=HEADERS)
    assert before.status_code == 200, f"scorecard read failed: {before.status_code} {before.text}"
    baseline = before.json()

    search = _search(client, SEED_QUERY)
    assert search.status_code == 200, f"search failed: {search.status_code} {search.text}"
    body = search.json()
    assert body["results"], f"search returned nothing to judge: {body}"
    assert seeded_document in {
        r["document_id"] for r in body["results"]
    }, f"search did not retrieve the seeded document {seeded_document}: {body}"

    event_id = body.get("event_id")
    assert event_id, (
        "search returned no event_id, so no agent can report feedback on it. Capture is "
        f"enabled for {WORKSPACE_ID} by default; a missing id means record_query_event "
        f"failed or capture was disabled: {body}"
    )

    # THE #240 ASSERTION: the very next request, immediately.
    feedback = client.post(
        f"{API_URL}/v1/evals/feedback",
        headers=JSON_HEADERS,
        json={"event_id": event_id, "verdict": "answered"},
    )
    assert feedback.status_code == 200, (
        f"feedback on a just-issued event_id returned {feedback.status_code} -- this is "
        f"#240: search advertised {event_id} before the capture row was durable, so the "
        f"caller's next request cannot find it: {feedback.text}"
    )
    verdict = feedback.json()
    assert verdict["event_id"] == event_id, f"feedback echoed a different event: {verdict}"
    # 'answered' with real results promotes unconditionally (grade 2, expected
    # docs = every returned doc) -- so a False here means the promotion rules
    # or the event's stored results regressed, not that the input was marginal.
    assert verdict["promoted"] is True, (
        f"'answered' feedback on a search with {len(body['results'])} results did not "
        f"promote to an eval case: {verdict}"
    )
    assert verdict["case_id"], f"promoted feedback carried no case_id: {verdict}"

    # The operator-visible surface reflects it.
    after = client.get(f"{API_URL}/v1/evals/scorecard", headers=HEADERS)
    assert after.status_code == 200, f"scorecard read failed: {after.status_code} {after.text}"
    scorecard = after.json()

    assert scorecard["workspace_id"] == WORKSPACE_ID
    assert (
        scorecard["captured_events"] > baseline["captured_events"]
    ), f"scorecard captured no new events: {baseline} -> {scorecard}"
    assert scorecard["feedback_count"] > baseline["feedback_count"], (
        f"feedback returned 200 but the scorecard's feedback count did not move: "
        f"{baseline} -> {scorecard}"
    )
    assert scorecard["feedback_distribution"].get("answered", 0) > baseline[
        "feedback_distribution"
    ].get("answered", 0), f"the 'answered' verdict was not counted: {scorecard}"
    assert (
        scorecard["eval_case_count"] >= baseline["eval_case_count"] + 1
    ), f"the promoted case is missing from the scorecard: {baseline} -> {scorecard}"
    assert isinstance(scorecard["summary"], str) and scorecard["summary"]
