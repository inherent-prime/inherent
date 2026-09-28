"""Chunk reuse detection at ingest time (inherent#394).

When a newly ingested (or re-ingested) document contains a section that
closely matches an existing chunk from an OLDER, already-ingested document in
the same workspace, that older chunk has been "reused". This module is the
detection step that feeds the usage-based ranking boost: for each new chunk
above a minimum size, it looks up its nearest neighbours in the SAME
tenant's Weaviate collection (excluding this document), confirms the match
with a cheap text-similarity check, and -- idempotently -- bumps the matched
(older) chunk's ``reuse_count`` in both Postgres (source of truth) and
Weaviate (mirrored property, see ``WeaviateService.set_chunk_reuse_count``).

Called from ``store_in_weaviate`` (temporal/activities/store.py), ONLY for a
workspace listed in ``settings.workspace_reuse_detection``
(``WORKSPACE_REUSE_DETECTION``, off by default). The whole call is
best-effort by design (#394: "failures never fail ingestion") -- every
Weaviate/DB error here is caught and logged, never re-raised, and a failure
on one chunk never stops the rest of the document's chunks from being
checked.

Design decisions
-----------------
- **Threshold**: cosine similarity >= 0.92 (sent to Weaviate as certainty (1 + cos) / 2 = 0.96)
  by default (``REUSE_SIMILARITY_THRESHOLD``), confirmed by a cheap text
  ratio (``difflib.SequenceMatcher``) >= 0.7 (``REUSE_TEXT_SIMILARITY_THRESHOLD``)
  on the handful of candidates that already passed the vector check -- never
  the whole workspace, so this stays cheap. See settings.py for the full
  rationale on each default.
- **Dedupe key**: ``(source_chunk_id, reusing_document_id,
  reusing_chunk_content_hash)`` -- see migration 024 / ``record_chunk_reuse``
  for why this makes a re-ingest of the SAME unchanged document a no-op.
- **Cost bound**: top-k nearest neighbours per chunk (``REUSE_TOP_K``,
  default 5), skip chunks shorter than ``REUSE_MIN_CHUNK_CHARS`` (default
  40 -- a two-word chunk hits almost any threshold and would flood
  reuse_count with noise), and the vector lookup is server-side
  (`near_object` + a `certainty` floor), never a fetch-then-filter over the
  whole collection.
- **Same-document exclusion**: the Weaviate query itself excludes
  ``document_id == this document`` (a document can never reuse its own
  chunks, nor a same-``document_id`` reprocessing of itself -- "another
  version of the same document" always keeps the same ``document_id`` in
  this engine).
"""

from __future__ import annotations

import difflib
import hashlib
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from src.models.document import DocumentChunk
    from src.services.database import DatabaseService
    from src.services.weaviate import WeaviateService

logger = structlog.get_logger(__name__)

# See module docstring for the rationale behind each default.
DEFAULT_TOP_K = 5
DEFAULT_MIN_CHUNK_CHARS = 40
DEFAULT_VECTOR_SIMILARITY_THRESHOLD = 0.92
DEFAULT_TEXT_SIMILARITY_THRESHOLD = 0.7


async def detect_and_record_chunk_reuse(
    *,
    weaviate_service: WeaviateService,
    db_service: DatabaseService,
    workspace_id: str,
    user_id: str,
    document_id: str,
    chunks: list[DocumentChunk],
    vector_similarity_threshold: float = DEFAULT_VECTOR_SIMILARITY_THRESHOLD,
    text_similarity_threshold: float = DEFAULT_TEXT_SIMILARITY_THRESHOLD,
    top_k: int = DEFAULT_TOP_K,
    min_chunk_chars: int = DEFAULT_MIN_CHUNK_CHARS,
) -> int:
    """Detect near-duplicate chunks from OTHER documents and bump their reuse_count.

    ``chunks`` are the chunks JUST written for ``document_id`` (already
    embedded and stored in Weaviate, and already present in Postgres --
    this must run after both stores succeed). Returns the number of NEW
    reuse events actually recorded (0 on a fully idempotent re-run, e.g.
    re-ingesting an unchanged document).

    Best-effort: every exception is caught here and logged, never raised --
    matching the caller's contract that reuse detection can never fail
    ingestion. A failure on one candidate/chunk does not stop the others.
    """
    recorded = 0
    for chunk in chunks:
        content = chunk.content or ""
        if len(content.strip()) < min_chunk_chars:
            continue  # too small to meaningfully "reuse" -- see module docstring

        try:
            chunk_uuid = weaviate_service.chunk_object_uuid(
                workspace_id, user_id, document_id, chunk.chunk_index
            )
            candidates = await weaviate_service.find_similar_chunks(
                workspace_id=workspace_id,
                user_id=user_id,
                chunk_uuid=chunk_uuid,
                exclude_document_id=document_id,
                # Weaviate `certainty` is (1 + cosine) / 2, not cosine: convert so the
                # documented cosine threshold is what is actually enforced.
                certainty_threshold=(1 + vector_similarity_threshold) / 2,
                top_k=top_k,
            )
        except Exception as exc:  # noqa: BLE001 -- best-effort, see docstring
            logger.warning(
                "chunk_reuse_lookup_failed",
                document_id=document_id,
                workspace_id=workspace_id,
                chunk_index=chunk.chunk_index,
                error=str(exc),
            )
            continue

        if not candidates:
            continue

        reusing_content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        recorded += await _confirm_and_record(
            db_service=db_service,
            weaviate_service=weaviate_service,
            workspace_id=workspace_id,
            user_id=user_id,
            document_id=document_id,
            content=content,
            reusing_content_hash=reusing_content_hash,
            candidates=candidates,
            text_similarity_threshold=text_similarity_threshold,
        )
    return recorded


async def _confirm_and_record(
    *,
    db_service: DatabaseService,
    weaviate_service: WeaviateService,
    workspace_id: str,
    user_id: str,
    document_id: str,
    content: str,
    reusing_content_hash: str,
    candidates: list[dict[str, Any]],
    text_similarity_threshold: float,
) -> int:
    """Confirm each vector candidate with a text check and record reuse. Returns count recorded."""
    recorded = 0
    for candidate in candidates:
        source_document_id = candidate.get("document_id")
        source_chunk_index = candidate.get("chunk_index")
        candidate_content = candidate.get("content") or ""

        if not source_document_id or source_chunk_index is None:
            continue
        if source_document_id == document_id:
            # Belt-and-braces: the Weaviate query already excludes this
            # document_id, but a chunk must NEVER be counted as reusing
            # itself or another chunk of the same document/version (#394).
            continue

        if text_similarity_threshold > 0:
            ratio = difflib.SequenceMatcher(None, content, candidate_content).ratio()
            if ratio < text_similarity_threshold:
                # Vector-similar but textually unrelated -- likely two
                # different chunks that merely embed close together (e.g.
                # shared boilerplate structure). Skip to avoid a false
                # positive reuse count.
                continue

        try:
            source_chunk = await db_service.get_chunk_for_reuse(
                document_id=source_document_id, chunk_index=source_chunk_index
            )
            if source_chunk is None:
                # Weaviate/Postgres briefly out of sync, or a stale object
                # from a since-reprocessed document -- nothing to bump.
                continue

            new_count = await db_service.record_chunk_reuse(
                source_chunk_id=source_chunk["id"],
                reusing_document_id=document_id,
                reusing_chunk_content_hash=reusing_content_hash,
                workspace_id=workspace_id,
                similarity=float(candidate.get("certainty") or 0.0),
            )
            if new_count is None:
                # Idempotent no-op: this exact (source, reusing document,
                # content) triple was already recorded.
                continue

            await weaviate_service.set_chunk_reuse_count(
                workspace_id=workspace_id,
                user_id=user_id,
                chunk_uuid=_as_uuid(candidate["uuid"]),
                reuse_count=new_count,
                last_reused_at=datetime.now(UTC),
            )
            recorded += 1
        except Exception as exc:  # noqa: BLE001 -- best-effort, see module docstring
            logger.warning(
                "chunk_reuse_record_failed",
                document_id=document_id,
                workspace_id=workspace_id,
                source_document_id=source_document_id,
                source_chunk_index=source_chunk_index,
                error=str(exc),
            )
            continue
    return recorded


def _as_uuid(value: Any) -> uuid.UUID:
    """Weaviate's client may hand back either a `uuid.UUID` or its str form."""
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
