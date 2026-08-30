"""store_in_weaviate heartbeating during embedding (#298).

Documents with large chunk counts used to hit store_in_weaviate's
StartToClose deterministically: #239 scaled the budget with chunk count,
but capped it at STORE_MAX_TIMEOUT_SECONDS (originally 900s) so one
pathological document could not pin a worker slot forever. #298's repro
(60,215 chunks) needed well over 15 minutes even with zero retries, so it
hit that cap on every attempt and never completed -- and because
asyncio.to_thread(embed_texts, ...) ran the whole document as one opaque
call, Temporal had no way to tell "still progressing, just slow" from
"hung", and the thread kept embedding after Temporal gave up on it.

These tests exercise the fix end-to-end through
WeaviateService.store_chunks_with_tenant (what store_in_weaviate actually
calls) inside a real Temporal activity context
(temporalio.testing.ActivityEnvironment), proving:

1. Progress is reported during a long store (real per-batch heartbeats, not
   a fixed timer).
2. A wedged store is detected and stopped without waiting out the full
   embed -- exercised by cancelling the activity (what heartbeat_timeout
   does server-side when heartbeats stop arriving) partway through and
   confirming the remaining, not-yet-started batches never dispatch.
3. A large document (previously capped at 900s, now budgeted up to
   STORE_MAX_TIMEOUT_SECONDS = 7200s) actually completes when the store
   path itself imposes no artificial per-document limit, and still fails
   loudly (raises, does not swallow) when a batch genuinely errors deep
   into a large document.
"""

from __future__ import annotations

import asyncio

import pytest
from temporalio.testing import ActivityEnvironment

from src.models.document import DocumentChunk
from src.services.weaviate import WeaviateService


@pytest.fixture(autouse=True)
def cleanup_test_data():
    """No-op override of the package-level DB-dependent autouse fixture."""
    yield


def _chunks(n: int) -> list[DocumentChunk]:
    return [
        DocumentChunk(
            document_id="d", content=f"chunk {i}", chunk_index=i, start_char=0, end_char=5
        )
        for i in range(n)
    ]


def _wired_service(n_chunks: int):
    """A WeaviateService whose Weaviate client boundary is fully mocked, so
    only the embedding path (under test) does real work."""
    from unittest.mock import AsyncMock, MagicMock

    settings = MagicMock()
    settings.weaviate_url = "http://localhost:8080"
    svc = WeaviateService(settings)
    svc.client = MagicMock()
    svc.ensure_workspace_collection = AsyncMock(return_value="Workspace_x")
    svc.ensure_user_tenant = AsyncMock(return_value="User_y")

    collection = MagicMock()
    svc.client.collections.get.return_value = collection
    tenant_collection = MagicMock()
    collection.with_tenant.return_value = tenant_collection
    batch = MagicMock()
    cm = MagicMock()
    cm.__enter__.return_value = batch
    cm.__exit__.return_value = False
    tenant_collection.batch.dynamic.return_value = cm
    tenant_collection.batch.failed_objects = []

    return svc, _chunks(n_chunks)


async def test_progress_is_reported_during_a_long_store(monkeypatch):
    """A multi-batch store must heartbeat real, monotonically-advancing
    progress -- not stay silent until the whole document finishes."""
    from src.services import embedder as emb

    monkeypatch.setenv("EMBEDDING_BATCH_SIZE", "5")
    monkeypatch.setenv("EMBEDDING_MAX_CONCURRENCY", "1")  # deterministic order
    monkeypatch.setattr(emb, "_post_embed", lambda inputs: [[0.0, 0.1] for _ in inputs])

    svc, chunks = _wired_service(23)  # 23 chunks / batch 5 -> 5 batches

    env = ActivityEnvironment()
    heartbeats: list[tuple[int, int]] = []
    env.on_heartbeat = lambda *details: heartbeats.append(
        (details[0]["chunk_batches_done"], details[0]["chunk_batches_total"])
    )

    count = await env.run(
        svc.store_chunks_with_tenant,
        chunks,
        "doc1",
        "ws1",
        "u1",
        "file.txt",
        "text/plain",
    )

    assert count == 23
    assert heartbeats == [(1, 5), (2, 5), (3, 5), (4, 5), (5, 5)]


async def test_no_heartbeat_outside_an_activity(monkeypatch):
    """store_chunks_with_tenant is also called directly by processor.py and
    reindex_from_postgres.py, with no Temporal activity context. It must
    not raise "not in an activity" -- the heartbeat callback has to be a
    no-op there, not a hard dependency on activity context."""
    from src.services import embedder as emb

    monkeypatch.setenv("EMBEDDING_BATCH_SIZE", "5")
    monkeypatch.setattr(emb, "_post_embed", lambda inputs: [[0.0] for _ in inputs])

    svc, chunks = _wired_service(7)
    count = await svc.store_chunks_with_tenant(
        chunks, "doc1", "ws1", "u1", "file.txt", "text/plain"
    )
    assert count == 7


async def test_wedged_store_stops_without_finishing_all_batches(monkeypatch):
    """A worker that stops making progress must be interruptible mid-embed,
    not run every remaining batch to completion regardless of what Temporal
    decided. Cancelling the activity stands in for heartbeat_timeout firing
    server-side (that is the real trigger in production; ActivityEnvironment
    has no server-side clock to simulate the timeout itself, but the effect
    on the activity -- its task gets cancelled -- is identical)."""
    from src.services import embedder as emb

    monkeypatch.setenv("EMBEDDING_BATCH_SIZE", "1")
    monkeypatch.setenv("EMBEDDING_MAX_CONCURRENCY", "1")  # strictly serial

    dispatched: list[str] = []
    release = asyncio.Event()

    def wedged_post(inputs):
        dispatched.append(inputs[0])
        if inputs[0] == "chunk 0":
            # The batch that hangs -- e.g. a wedged TEI connection.
            import time as _time

            while not release.is_set():
                _time.sleep(0.01)
        return [[0.0] for _ in inputs]

    monkeypatch.setattr(emb, "_post_embed", wedged_post)

    # 50 chunks: if cancellation did not actually stop dispatch, this test
    # would hang (or, with the old code, silently burn CPU embedding all 50
    # after the activity had already been given up on).
    svc, chunks = _wired_service(50)

    env = ActivityEnvironment()
    heartbeats: list[tuple[int, int]] = []
    env.on_heartbeat = lambda *details: heartbeats.append(
        (details[0]["chunk_batches_done"], details[0]["chunk_batches_total"])
    )

    task = asyncio.ensure_future(
        env.run(
            svc.store_chunks_with_tenant,
            chunks,
            "doc1",
            "ws1",
            "u1",
            "file.txt",
            "text/plain",
        )
    )
    await asyncio.sleep(0.05)  # let the first (wedged) batch start
    env.cancel()
    release.set()  # let the blocked thread return so the test exits cleanly

    with pytest.raises(asyncio.CancelledError):
        await task

    # Only the wedged batch was ever dispatched -- the cancellation was
    # observed before any of the other 49 batches started, and no heartbeat
    # ever fired (the one batch in flight never completed).
    assert dispatched == ["chunk 0"]
    assert heartbeats == []


async def test_large_document_completes_when_nothing_else_fails(monkeypatch):
    """A document far past the OLD 900s cap's practical chunk count must
    still complete end to end -- the store path itself has no artificial
    per-document ceiling; only the (now much larger) Temporal StartToClose
    budget does, and that is out of this unit's scope."""
    from src.services import embedder as emb

    monkeypatch.setenv("EMBEDDING_BATCH_SIZE", "32")
    monkeypatch.setenv("EMBEDDING_MAX_CONCURRENCY", "4")
    monkeypatch.setattr(emb, "_post_embed", lambda inputs: [[0.0] for _ in inputs])

    n = 3000  # well past what used to force the 900s cap (535 chunks, #228)
    svc, chunks = _wired_service(n)

    env = ActivityEnvironment()
    seen_totals: list[int] = []
    env.on_heartbeat = lambda *details: seen_totals.append(details[0]["chunk_batches_total"])

    count = await env.run(
        svc.store_chunks_with_tenant,
        chunks,
        "doc1",
        "ws1",
        "u1",
        "file.txt",
        "text/plain",
    )

    assert count == n
    assert seen_totals, "a 3000-chunk document must heartbeat across its batches"
    assert seen_totals[-1] == -(-n // 32)  # ceil division: expected batch count


async def test_large_document_fails_loudly_on_a_genuine_error(monkeypatch):
    """A large document must not silently under-report on a real failure --
    an error deep into the batch sequence must still raise, exactly like
    the existing #8 partial-failure contract, just at scale."""
    import httpx

    from src.services import embedder as emb

    monkeypatch.setenv("EMBEDDING_BATCH_SIZE", "32")
    monkeypatch.setenv("EMBEDDING_MAX_CONCURRENCY", "1")
    monkeypatch.setenv("EMBEDDING_BATCH_MAX_RETRIES", "1")  # fail fast in test

    calls = {"n": 0}

    def failing_after_many_batches(inputs):
        calls["n"] += 1
        if calls["n"] > 20:
            req = httpx.Request("POST", "/embed")
            resp = httpx.Response(500, request=req)
            raise httpx.HTTPStatusError("tei internal error", request=req, response=resp)
        return [[0.0] for _ in inputs]

    monkeypatch.setattr(emb, "_post_embed", failing_after_many_batches)

    svc, chunks = _wired_service(3000)  # ~94 batches at size 32; fails at batch 21

    env = ActivityEnvironment()
    with pytest.raises(httpx.HTTPStatusError, match="tei internal error"):
        await env.run(
            svc.store_chunks_with_tenant,
            chunks,
            "doc1",
            "ws1",
            "u1",
            "file.txt",
            "text/plain",
        )
