"""Live dependency-outage dead-letter/recovery E2E, against the real compose
stack (Task 7 / B4 of the e2e-merge-gates plan).

Every other file in this directory (``test_database_failures.py``,
``test_weaviate_failures.py``, etc.) mocks the failing dependency in-process.
This is the first LIVE one: it actually stops the compose ``weaviate``
container, drives a real document through the real Temporal worker until it
dead-letters, restarts ``weaviate``, hits the real dead-letter retry HTTP
endpoint, and confirms the document recovers -- including becoming
searchable again through the public API.

Step 1 findings (mandatory pre-read per the task brief), with exact file:line
references as of this writing:

* Compose service name for Weaviate: ``weaviate`` (docker-compose.yml:87),
  container_name ``inherent-oss-weaviate`` (docker-compose.yml:89). Its
  healthcheck (docker-compose.yml:110-114) is
  ``wget --spider http://localhost:8080/v1/.well-known/ready``,
  interval=10s, timeout=5s, retries=5 -- so worst case ~50s to flip
  unhealthy->healthy after a fresh `start`, though in practice weaviate
  comes up much faster (single-node, small dev dataset).

* ``depends_on: weaviate: condition: service_healthy`` on
  ``inh-ingestion-svc``/``inh-public-api-svc`` (docker-compose.yml:293,
  ~330) only gates *initial* `docker compose up` ordering. Stopping
  ``weaviate`` later does NOT stop or restart those containers, and does NOT
  fail their own healthchecks: ingestion's healthcheck
  (docker-compose.yml:303-308) just curls its own ``/health``, and the
  public API's plain ``GET /health`` (inh-public-api-svc/src/api/v1/health.py
  liveness_check) does not check Weaviate either (only
  ``/health/ready`` does, via ``_check_weaviate``). So during the outage,
  `docker compose ps` keeps showing both app containers "healthy", and a
  document upload (which only publishes an MQ message) succeeds normally --
  the failure surfaces later, inside the Temporal workflow's storage step.

* Ingestion service's OWN REST API: separate port from the public API.
  docker-compose.yml:262 publishes it as ``18002:8000`` (container listens
  on 8000; INGESTION_API_KEY defaults to ``dev-ingestion-key``,
  docker-compose.yml:282 / .env.example:58). Dead-letter routes
  (services/inh-ingestion-svc/src/api/app.py:790-961), all gated by
  ``X-API-Key`` (verify_api_key, src/api/auth.py) checked against
  INGESTION_API_KEY (NOT the public API's per-workspace key):
    - ``GET  /dead-letter?workspace_id=...&status=pending&limit=...``
      (app.py:796) -- lists dead-letter jobs, workspace_id REQUIRED
      (#177). Response: ``{"jobs": [...], "total": N}``; each job dict has
      an integer ``id`` (NOT ``job_id``) plus ``document_id``,
      ``workspace_id``, ``status``, ``error_type``, ``retry_count``, etc.
      (src/services/database.py:1599 get_dead_letter_jobs).
    - ``GET  /dead-letter/{job_id}?workspace_id=...`` (app.py:860) -- single
      job, 404 if job_id/workspace_id don't match.
    - ``POST /dead-letter/{job_id}/retry?workspace_id=...`` (app.py:891) --
      re-publishes the job's reconstructed original upload message via
      ``trigger.trigger_workflow_async(..., supersede_running=False)``
      (src/temporal/trigger.py:397), i.e. starts a genuinely fresh workflow
      run (full re-extract/chunk/embed/store, not a resume). Requires
      status in {"pending", "retrying"} or 409s. On success:
      ``{"retried": true, "job_id": ..., "new_workflow_id": ...}``.
    - ``POST /dead-letter/{job_id}/abandon?workspace_id=...`` (app.py:938)
      -- not used by this test.

  FIXED by #249 (was previously an open gap, discovered while writing this
  test -- see git history for the original NOTE if you need the pre-fix
  writeup): a successful ingestion of ``document_id`` now resolves that
  document's outstanding ``status='retrying'`` dead-letter rows. The write
  happens from ``DocumentIngestionWorkflow``'s single success path via the
  ``resolve_dead_letter_jobs`` activity ->
  ``DatabaseService.resolve_dead_letter_jobs_for_document`` (best-effort,
  keyed on ``document_id`` rather than the dead-letter job id -- see that
  method's docstring). Step 7 below now asserts the row reaches
  ``status="resolved"`` after the retried run completes and the document is
  searchable again.

* Temporal retry policy that determines pacing -- the Weaviate storage step
  (services/inh-ingestion-svc/src/temporal/workflows/document_ingestion.py,
  the ``store_in_weaviate`` ``execute_activity`` call, ~line 477-487):
  ``RetryPolicy(maximum_attempts=5, initial_interval=2s,
  maximum_interval=30s, backoff_coefficient=2.0)``, activity
  ``start_to_close_timeout=60s``. ``store_in_weaviate`` itself
  (src/temporal/activities/store.py:249) raises immediately (no network
  hang) when ``weaviate_service.is_connected()`` is false -- that check is
  ``self.client.is_ready()`` (src/services/weaviate.py:225), a live HTTP
  call that fails fast (connection refused) once the container is stopped.
  So each of the 5 attempts fails in well under a second, and the ONLY
  meaningful wall-clock cost from this policy is the 4 backoff waits
  between attempts: 2 + 4 + 8 + 16 = 30s (the 5th interval would be 32s,
  capped to 30s, but there is no 6th attempt). PostgreSQL storage
  (``store_in_postgresql``) runs in parallel via ``asyncio.gather`` and is
  unaffected, so it always succeeds; the workflow fails specifically on
  the Weaviate side (document_ingestion.py:532 `if not wv_result.success`)
  and calls ``_record_dead_letter_best_effort`` (document_ingestion.py:542)
  -- this is the dead-letter job we poll for.

  On top of that ~30s floor, the workflow must first finish extract/chunk/
  embed for whatever document we upload before it even reaches the storage
  step, and CI's CPU-only TEI embedder can be slow under load (the compose
  workflow bumps ``INTEGRATION_TIMEOUT`` to 600s for exactly this reason).
  This test paces its dead-letter-appears poll off ``INTEGRATION_TIMEOUT``
  (same env var + 180s local default the other compose tests already use)
  rather than hand-deriving a tighter bound from the 30s retry-backoff
  floor alone, since that floor is only a lower bound on the real wall
  clock, not an upper one.

Sequence (test_dependency_outage_dead_letters_then_recovers):
  0. Positive control (falsifiability, per Task 5/6 lesson): upload a small
     canary document while the stack is fully healthy and confirm it
     becomes searchable normally. This proves the harness/pipeline itself
     is healthy BEFORE we inject any failure, so a later failure in the
     fault-injection path is attributable to dead-letter/retry behavior,
     not a pre-broken environment.
  1. docker compose stop weaviate.
  2. Upload a distinct document via the public API (:18000).
  3. Poll the ingestion API's dead-letter listing (:18002) until a job for
     that document_id appears.
  4. docker compose start weaviate; wait for its healthcheck to report
     healthy again.
  5. POST the retry endpoint for that dead-letter job.
  6. Poll until the document is "processed" (public API) AND its sentinel
     text is returned by /v1/search.

Cleanup (``finally``) unconditionally runs ``docker compose start weaviate``
and waits for it to report healthy, regardless of where the test
failed/passed -- the shared compose stack is needed by Tasks 8-12 too.

Marked ``compose`` + ``failure_injection``. Deliberately NOT ``smoke`` (the
PR-blocking smoke lane must stay at exactly 6 tests) -- this only runs in
the full ``compose`` lane (``integration.yml``'s
``uv run pytest -m compose`` step for this service).
"""

from __future__ import annotations

import os
import subprocess
import time
import uuid
from pathlib import Path

import httpx
import pytest

pytestmark = [pytest.mark.compose, pytest.mark.failure_injection]

# repo root: tests/failure_injection/<file> -> parents[4], same depth as
# services/inh-public-api-svc/tests/integration/<file> -> parents[4].
REPO_ROOT = Path(__file__).resolve().parents[4]

PUBLIC_API_URL = os.environ.get("PUBLIC_API_URL", "http://localhost:18000").rstrip("/")
PUBLIC_API_KEY = os.environ.get("INTEGRATION_API_KEY", "ink_dev_local_key_001")
WORKSPACE_ID = os.environ.get("INTEGRATION_WORKSPACE_ID", "ws_local_001")
PUBLIC_HEADERS = {"X-API-Key": PUBLIC_API_KEY, "X-Workspace-Id": WORKSPACE_ID}

INGESTION_API_URL = os.environ.get("INGESTION_API_URL", "http://localhost:18002").rstrip("/")
INGESTION_API_KEY = os.environ.get("INGESTION_API_KEY", "dev-ingestion-key")
INGESTION_HEADERS = {"X-API-Key": INGESTION_API_KEY}

# Same env var + default the other compose tests in this repo use, so a CI
# override (integration.yml sets 600s for the slow-CPU-embedder scenario)
# transparently widens this test's budget too. See the pacing discussion in
# the module docstring for why this, not a tighter hand-derived bound off
# the 30s retry-backoff floor, is the right knob.
TIMEOUT = int(os.environ.get("INTEGRATION_TIMEOUT", "180"))

# Weaviate's own healthcheck is interval=10s/timeout=5s/retries=5 (~50s worst
# case) after a fresh `docker compose start`; add margin for image/volume
# reattachment on a loaded CI runner.
WEAVIATE_HEALTHY_TIMEOUT = int(os.environ.get("WEAVIATE_HEALTHY_TIMEOUT", "120"))

WEAVIATE_CONTAINER = "inherent-oss-weaviate"

POLL_INTERVAL = 3


def _docker_compose(*args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", "compose", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _weaviate_health_status() -> str | None:
    """Return the container's Docker healthcheck status, or None if unknown."""
    result = subprocess.run(
        ["docker", "inspect", "--format", "{{.State.Health.Status}}", WEAVIATE_CONTAINER],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _wait_weaviate_healthy(timeout: int = WEAVIATE_HEALTHY_TIMEOUT) -> None:
    deadline = time.monotonic() + timeout
    last_status: str | None = None
    while time.monotonic() < deadline:
        last_status = _weaviate_health_status()
        if last_status == "healthy":
            return
        time.sleep(2)
    pytest.fail(f"weaviate did not become healthy within {timeout}s (last status={last_status!r})")


def _require_stack() -> httpx.Client:
    """Skip (don't fail) when the stack, or docker control of it, is unavailable."""
    try:
        ps = subprocess.run(
            ["docker", "compose", "ps", "--format", "json"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        pytest.skip(f"docker compose not usable from test host: {exc}")
    if ps.returncode != 0 or not ps.stdout.strip():
        pytest.skip(f"docker compose stack not up in {REPO_ROOT}: {ps.stderr.strip()}")

    client = httpx.Client(timeout=30)
    try:
        resp = client.get(f"{PUBLIC_API_URL}/health", timeout=5)
    except httpx.HTTPError as exc:
        client.close()
        pytest.skip(f"public API not reachable at {PUBLIC_API_URL}: {exc}")
    if resp.status_code != 200:
        client.close()
        pytest.skip(f"public API unhealthy at {PUBLIC_API_URL}: HTTP {resp.status_code}")

    try:
        resp = client.get(f"{INGESTION_API_URL}/health", timeout=5)
    except httpx.HTTPError as exc:
        client.close()
        pytest.skip(f"ingestion API not reachable at {INGESTION_API_URL}: {exc}")
    if resp.status_code != 200:
        client.close()
        pytest.skip(f"ingestion API unhealthy at {INGESTION_API_URL}: HTTP {resp.status_code}")

    if _weaviate_health_status() != "healthy":
        client.close()
        pytest.skip("weaviate is not healthy before the test starts; refusing to inject a fault")

    return client


def _upload_bytes(client: httpx.Client, content: bytes, filename: str) -> str:
    resp = client.post(
        f"{PUBLIC_API_URL}/v1/documents",
        headers=PUBLIC_HEADERS,
        files={"file": (filename, content, "text/plain")},
    )
    assert resp.status_code == 201, f"upload of {filename} failed: {resp.status_code} {resp.text}"
    document_id = resp.json()["document_id"]
    assert document_id
    return document_id


def _search(client: httpx.Client, query: str) -> dict:
    resp = client.post(
        f"{PUBLIC_API_URL}/v1/search",
        headers={**PUBLIC_HEADERS, "Content-Type": "application/json"},
        json={"query": query, "limit": 20},
    )
    assert resp.status_code == 200, f"search failed: {resp.status_code} {resp.text}"
    return resp.json()


def _wait_searchable(client: httpx.Client, document_id: str, query: str, timeout: int) -> None:
    deadline = time.monotonic() + timeout
    last_body: dict = {}
    while time.monotonic() < deadline:
        last_body = _search(client, query)
        if document_id in {r["document_id"] for r in last_body["results"]}:
            return
        time.sleep(POLL_INTERVAL)
    pytest.fail(
        f"document {document_id} did not become searchable within {timeout}s "
        f"(last total_results={last_body.get('total_results')})"
    )


def _document_status(client: httpx.Client, document_id: str) -> dict:
    resp = client.get(f"{PUBLIC_API_URL}/v1/documents/{document_id}", headers=PUBLIC_HEADERS)
    assert resp.status_code == 200, f"get document failed: {resp.status_code} {resp.text}"
    return resp.json()


def _wait_document_processed(client: httpx.Client, document_id: str, timeout: int) -> dict:
    deadline = time.monotonic() + timeout
    last_status: str | None = None
    while time.monotonic() < deadline:
        body = _document_status(client, document_id)
        last_status = body.get("status")
        if last_status == "processed":
            return body
        time.sleep(POLL_INTERVAL)
    pytest.fail(
        f"document {document_id} did not reach 'processed' within {timeout}s (last status={last_status})"
    )


def _find_dead_letter_job(client: httpx.Client, document_id: str) -> dict | None:
    resp = client.get(
        f"{INGESTION_API_URL}/dead-letter",
        headers=INGESTION_HEADERS,
        params={"workspace_id": WORKSPACE_ID, "status": "pending", "limit": 200},
    )
    assert resp.status_code == 200, f"dead-letter list failed: {resp.status_code} {resp.text}"
    for job in resp.json()["jobs"]:
        if job.get("document_id") == document_id:
            return job
    return None


def _wait_dead_lettered(client: httpx.Client, document_id: str, timeout: int) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = _find_dead_letter_job(client, document_id)
        if job is not None:
            return job
        time.sleep(POLL_INTERVAL)
    pytest.fail(
        f"document {document_id} did not appear in the dead-letter listing within {timeout}s"
    )


def test_dependency_outage_dead_letters_then_recovers() -> None:
    """Weaviate outage -> upload dead-letters -> recovery -> retry -> searchable.

    See the module docstring for the discovered retry-policy/endpoint facts
    that drive this test's pacing and requests.
    """
    client = _require_stack()
    try:
        # --- 0. Positive control: prove the pipeline works normally FIRST,
        # before injecting any fault, so a later failure is attributable to
        # the dead-letter/retry path and not a pre-broken environment.
        canary_marker = f"CANARY-{uuid.uuid4().hex}"
        canary_id = _upload_bytes(
            client,
            f"# Dead-letter test positive control\nSentinel token {canary_marker}.".encode(),
            f"dead-letter-canary-{uuid.uuid4().hex}.md",
        )
        _wait_searchable(client, canary_id, canary_marker, timeout=TIMEOUT)

        # --- 1. Take Weaviate down.
        stop = _docker_compose("stop", "weaviate")
        assert stop.returncode == 0, f"docker compose stop weaviate failed: {stop.stderr}"

        # --- 2. Upload a distinct document while Weaviate is down. Extraction/
        # chunking/PG-storage succeed; the parallel Weaviate-storage activity
        # fails every attempt (connection refused, fails fast) and, after the
        # 5-attempt/30s-backoff RetryPolicy is exhausted, the workflow marks
        # the document failed and records a dead-letter job.
        outage_marker = f"OUTAGE-{uuid.uuid4().hex}"
        document_id = _upload_bytes(
            client,
            f"# Dead-letter outage probe\nSentinel token {outage_marker}.".encode(),
            f"dead-letter-outage-{uuid.uuid4().hex}.md",
        )

        # --- 3. Poll the ingestion API's dead-letter listing for this document.
        job = _wait_dead_lettered(client, document_id, timeout=TIMEOUT)
        job_id = job["id"]
        assert job["workspace_id"] == WORKSPACE_ID
        assert job.get("status") == "pending"

        # --- 4. Bring Weaviate back and wait for it to report healthy.
        start = _docker_compose("start", "weaviate")
        assert start.returncode == 0, f"docker compose start weaviate failed: {start.stderr}"
        _wait_weaviate_healthy()

        # --- 5. Retry the dead-lettered job via the ingestion API.
        retry_resp = client.post(
            f"{INGESTION_API_URL}/dead-letter/{job_id}/retry",
            headers=INGESTION_HEADERS,
            params={"workspace_id": WORKSPACE_ID},
        )
        assert (
            retry_resp.status_code == 200
        ), f"retry of dead-letter job {job_id} failed: {retry_resp.status_code} {retry_resp.text}"
        assert retry_resp.json().get("retried") is True

        # --- 6. The re-triggered workflow must fully re-process the document
        # (fresh extract/chunk/embed/store) and it must become searchable
        # again -- proof recovery didn't just flip a status flag.
        body = _wait_document_processed(client, document_id, timeout=TIMEOUT)
        assert body["status"] == "processed"
        _wait_searchable(client, document_id, outage_marker, timeout=TIMEOUT)

        # --- 7. #249: the dead-letter job row must reach status="resolved"
        # once the retried workflow run genuinely completes -- the document
        # above is processed AND searchable, so its dead-letter row must not
        # still read "retrying" (indistinguishable from a retry still in
        # flight or one that silently failed). The resolve write is
        # best-effort and keyed on document_id (see
        # DatabaseService.resolve_dead_letter_jobs_for_document), so this
        # polls briefly rather than asserting on the very first read --
        # it runs as one of the last activities in the workflow's success
        # path, after the document is already visibly processed+searchable,
        # so in practice it should already be set by the time we get here,
        # but poll for a short window to absorb ordinary scheduling lag.
        deadline = time.monotonic() + 30
        job_after_retry: dict = {}
        while time.monotonic() < deadline:
            job_after_retry_resp = client.get(
                f"{INGESTION_API_URL}/dead-letter/{job_id}",
                headers=INGESTION_HEADERS,
                params={"workspace_id": WORKSPACE_ID},
            )
            assert job_after_retry_resp.status_code == 200, (
                f"dead-letter job {job_id} not readable after retry: "
                f"{job_after_retry_resp.status_code} {job_after_retry_resp.text}"
            )
            job_after_retry = job_after_retry_resp.json()
            if job_after_retry.get("status") == "resolved":
                break
            time.sleep(POLL_INTERVAL)

        assert job_after_retry.get("id") == job_id
        assert job_after_retry.get("document_id") == document_id
        assert job_after_retry.get("status") == "resolved", (
            f"dead-letter job {job_id} did not reach status='resolved' "
            f"within 30s of the retried document becoming processed+"
            f"searchable (#249) -- last observed status="
            f"{job_after_retry.get('status')!r}"
        )
    finally:
        # Unconditional: never leave the shared stack with Weaviate down --
        # Tasks 8-12 still need it healthy. Cleanup must be airtight, not
        # best-effort: a subprocess failure here (TimeoutExpired/OSError/
        # CalledProcessError) must still hit the loud "fix manually" path
        # instead of propagating past it silently, and the client must
        # always be closed even if the health-wait itself raises.
        try:
            try:
                restore = _docker_compose("start", "weaviate")
                if restore.returncode != 0:
                    pytest.fail(
                        f"CLEANUP FAILED: 'docker compose start weaviate' returned "
                        f"{restore.returncode}: {restore.stderr}. Stack may be left "
                        f"with weaviate down -- fix manually before running further "
                        f"tests."
                    )
            except (subprocess.TimeoutExpired, OSError, subprocess.CalledProcessError) as exc:
                pytest.fail(
                    f"CLEANUP FAILED: 'docker compose start weaviate' raised "
                    f"{exc!r}. Stack may be left with weaviate down -- fix manually "
                    f"before running further tests: docker compose start weaviate"
                )
            _wait_weaviate_healthy()
        finally:
            client.close()
