"""ChunkEditWorkflow tests against the REAL docker-compose Temporal server.

Closes the verification gap flagged across the #134/#137 review rounds:
tests/temporal/test_chunk_edit_workflow.py's WorkflowEnvironment.
start_time_skipping() needs to download an ephemeral test-server binary from
temporal.download, which this sandbox's proxy policy-blocks -- so that
file's control-flow assertions (re-raise -> RetryPolicy -> compensation ->
ChunkEditResult(success=False)) had never actually run against a live
Temporal server before this file existed, only been read.

docker-compose.yml already runs `temporalio/auto-setup:1.24.2` (service
`temporal`, host port 17233 -> container 7233), so this connects to that
real server with `Client.connect` instead of an ephemeral one -- no
temporal.download needed. Marked `compose` (deselected by default; run via
`uv run pytest -m compose`, e.g. in CI's `make test-integration` /
`integration.yml` job, which brings the compose stack up first).

Uses the SAME mock activities as test_chunk_edit_workflow.py (re-verifying
Temporal's retry/failure wiring, not real Postgres/Weaviate -- those already
have their own compose coverage elsewhere) but a dedicated task queue and
per-run-unique workflow IDs, so this can never be picked up by the real
`inh-ingestion-svc` worker also polling the `document-ingestion` queue in
the same compose stack, and can be re-run against the same persistent
server without workflow-ID collisions.

NOT executed in this sandbox (no compose stack, and the sandbox's proxy
would in any case block reaching a real cluster the same way it blocks
temporal.download) -- authored and reviewed for correctness, but CI is
this file's first live run. The TEST_TEMPORAL_HOST default matches
docker-compose.yml's host-mapped port for a test process running on the CI
runner itself (outside the compose network), per how the existing
`integration.yml` "Run ingestion compose benchmarks" step invokes
`uv run pytest -m compose` directly on the runner after `docker compose up`.
"""

from __future__ import annotations

import os
import uuid
from typing import Any

import pytest
from temporalio import activity
from temporalio.client import Client
from temporalio.worker import Worker

from src.temporal.models import ChunkEditInput, ChunkEditWeaviateFailureInput
from src.temporal.workflows.chunk_edit import ChunkEditWorkflow

pytestmark = pytest.mark.compose

# Matches docker-compose.yml's `temporal` service port mapping (17233:7233)
# for a test process running on the host/CI-runner, not inside the compose
# network (where it would instead be reachable at temporal:7233).
TEST_TEMPORAL_HOST = os.getenv("TEST_TEMPORAL_HOST", "localhost:17233")

# Dedicated so this test worker is never a candidate for a workflow task the
# real ingestion-svc worker (polling "document-ingestion") would otherwise
# also be eligible to pick up.
TASK_QUEUE = "chunk-edit-compose-test-queue"


def _input(**overrides) -> ChunkEditInput:
    defaults: dict[str, Any] = {
        "document_id": "doc1",
        "chunk_index": 0,
        "content": "corrected dosage: 10mg",
        "workspace_id": "ws1",
        "user_id": "user1",
    }
    defaults.update(overrides)
    return ChunkEditInput(**defaults)


@activity.defn(name="update_chunk_postgresql")
async def mock_pg_update_succeeds(input: ChunkEditInput) -> bool:
    return True


class _AlwaysFailsWeaviate:
    """Always raises, counting attempts -- proves the real server actually
    drives RetryPolicy(maximum_attempts=3), not just that the SDK object
    was constructed correctly (all a mocked/ephemeral-only test can show)."""

    def __init__(self) -> None:
        self.attempts = 0

    @activity.defn(name="update_chunk_weaviate")
    async def __call__(self, input: ChunkEditInput) -> bool:
        self.attempts += 1
        raise ConnectionError("TEI sidecar unreachable")


class _RecordsFailureCalls:
    # Annotated with the real input type rather than `Any` -- temporalio
    # picks its deserialization target from this annotation, and `Any`
    # yields a raw dict, breaking the attribute assertions below. See the
    # fuller note in test_chunk_edit_workflow.py.
    @activity.defn(name="record_chunk_edit_weaviate_failure")
    async def __call__(self, input: ChunkEditWeaviateFailureInput) -> bool:
        self.calls.append(input)
        return True

    def __init__(self) -> None:
        self.calls: list[ChunkEditWeaviateFailureInput] = []


@pytest.fixture()
async def compose_client() -> Client:
    """Connect to the real compose Temporal server.

    Not wrapped in a skip-if-unreachable try/except like tests/conftest.py's
    db_service fixture: `compose` is already deselected by default (see
    pyproject.toml addopts), so a connection failure here means the compose
    marker was passed without the stack actually being up -- that should
    fail loudly, the same way the other `-m compose` suites in this repo
    (e.g. tests/benchmark/*) expect the stack to already be running.
    """
    return await Client.connect(TEST_TEMPORAL_HOST, namespace="default")


@pytest.mark.asyncio
async def test_permanent_weaviate_failure_is_reported_not_swallowed_live(
    compose_client: Client,
):
    """Real-server mirror of test_chunk_edit_workflow.py's core #137
    regression check: a Weaviate failure that survives every retry must
    yield ChunkEditResult(success=False) with the real cause message (not
    Temporal's generic "Activity task failed" -- see workflows/chunk_edit.py
    for the `.cause` unwrapping this pins), and the compensating activity
    must have been invoked."""
    weaviate_activity = _AlwaysFailsWeaviate()
    failure_recorder = _RecordsFailureCalls()
    run_id = uuid.uuid4().hex

    async with Worker(
        compose_client,
        task_queue=TASK_QUEUE,
        workflows=[ChunkEditWorkflow],
        # Bound __call__, not the instance -- see the same note in
        # test_chunk_edit_workflow.py. temporalio's _Definition.from_callable
        # returns None for a class instance, so the Worker rejects it.
        activities=[
            mock_pg_update_succeeds,
            weaviate_activity.__call__,
            failure_recorder.__call__,
        ],
    ):
        result = await compose_client.execute_workflow(
            ChunkEditWorkflow.run,
            _input(),
            id=f"chunk-edit-compose-test-permanent-failure-{run_id}",
            task_queue=TASK_QUEUE,
        )

    assert result.success is False
    assert result.error is not None
    assert "Weaviate" in result.error
    assert "PostgreSQL updated" in result.error
    # Must be the real underlying error, not Temporal's generic wrapper
    # message -- the exact defect the judge's blocker 1 caught.
    assert "TEI sidecar unreachable" in result.error
    assert "Activity task failed" not in result.error

    assert weaviate_activity.attempts == 3
    assert len(failure_recorder.calls) == 1
    assert failure_recorder.calls[0].document_id == "doc1"
    assert "TEI sidecar unreachable" in failure_recorder.calls[0].error_message


@pytest.mark.asyncio
async def test_happy_path_both_stores_succeed_live(compose_client: Client):
    @activity.defn(name="update_chunk_weaviate")
    async def mock_weaviate_update_succeeds(input: ChunkEditInput) -> bool:
        return True

    run_id = uuid.uuid4().hex

    async with Worker(
        compose_client,
        task_queue=TASK_QUEUE,
        workflows=[ChunkEditWorkflow],
        activities=[mock_pg_update_succeeds, mock_weaviate_update_succeeds],
    ):
        result = await compose_client.execute_workflow(
            ChunkEditWorkflow.run,
            _input(),
            id=f"chunk-edit-compose-test-happy-path-{run_id}",
            task_queue=TASK_QUEUE,
        )

    assert result.success is True
    assert result.document_id == "doc1"
