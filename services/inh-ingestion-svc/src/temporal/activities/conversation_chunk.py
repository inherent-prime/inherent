"""chunk_conversation: turn-aware chunking for conversation ingestion (#306).

Runs AFTER `redact_turns` in `ConversationMemoryWorkflow`'s flush pipeline:

    buffered turns -> redact_turns -> chunk_conversation -> store(append=True)

THE SECURITY PROPERTY (read before changing this activity's input wiring)
-----------------------------------------------------------------------------
This activity reads turn TEXT from `ChunkConversationInput.redacted_turns`
ONLY -- never from anywhere else. `redacted_turns` is `redact_turns`'s
output (see redact.py's module docstring, "THE SHARPEST EDGE"): the one and
only place raw, pre-redaction turn text is allowed to exist in this
pipeline is inside `redact_turns` itself. `redact_turns`'s
`non_retryable=True` stops redaction FROM being retried; it does nothing to
stop a *different* bug -- this activity (or the workflow calling it)
re-deriving turn text from the workflow's own raw buffer instead of
`redact_turns`'s output. If that ever happens, every credential
`redact_turns` redacted is back in the pipeline as if it had never run.

`ChunkConversationInput.turn_meta` carries `ts`/`client` -- NON-text,
NON-sensitive fields `RedactedTurn` doesn't carry (see redact.py: it only
ever returns `turn_id`/`text`/`role`/`redaction_counts`). Reading those from
the workflow's buffer is safe and necessary (they're not sensitive and
`redact_turns` never claimed to carry them); the invariant is specifically
about TEXT, and only text.

Turn-aware chunking
----------------------
Each chunk belongs to exactly ONE turn -- chunk boundaries never straddle
two different turns, so `role` is always a single, well-defined value per
chunk (never "half user, half assistant"). A turn's text ordinarily becomes
exactly one chunk; a turn long enough to exceed the max-chunk-size budget is
split into several chunks *within* that turn (using the same size-based,
word-boundary-respecting splitter `chunk_text` uses for oversized text), but
every one of those sub-chunks still carries that SAME turn's turn_index/
role/ts/client -- it is never merged with a neighboring turn's text.

Every chunk records `turn_index` (the turn's position in the ORIGINAL
buffered flush, 0-based), `role`, `ts`, and `client` in `ChunkData.metadata`
(store.py's `_risk_metadata` promotes these into the persisted
`document_chunks.metadata` JSONB and `weaviate.py` promotes them onto
Weaviate object properties -- same promote-from-metadata pattern #44/#129
already established, extended rather than forked).

Chunk indexing (append mode)
-------------------------------
`chunk_index` is GLOBAL and CONTINUES from this conversation document's
current `chunk_count` (read once, up front, before any chunk is produced) --
never restarts at 0 -- so `store_processed_document`/`store_chunks_with_tenant`
called with `append=True` can insert this flush's chunks without colliding
with (or needing to independently recompute an offset against) chunks a
previous flush already committed. Computing the offset ONCE here, before
either store activity runs, is what makes `store_in_postgresql` and
`store_in_weaviate` safely parallelizable (`asyncio.gather`, matching
DocumentIngestionWorkflow) despite append mode -- if each store activity
computed its own offset independently, running them in parallel could race
(one reading the pre-write count, the other reading a count that already
includes the first activity's freshly-committed rows).
"""

from __future__ import annotations

import structlog
from temporalio import activity

from src.temporal.lineage import track_event
from src.temporal.models import ChunkConversationInput, ChunkConversationOutput

logger = structlog.get_logger(__name__)


@activity.defn
async def chunk_conversation(input: ChunkConversationInput) -> ChunkConversationOutput:
    """Turn-aware chunking of a redacted conversation-turn batch (#306).

    Reads `input.redacted_turns` (POST-redaction text only -- see module
    docstring) and `input.turn_meta` (ts/client), and writes turn-aware
    chunks to staging under `input.workflow_run_id`, exactly like `chunk_text`
    does -- so `store_in_postgresql`/`store_in_weaviate` read them completely
    unmodified.

    Args:
        input: redacted turns + non-text metadata + this flush's context.

    Returns:
        ChunkConversationOutput with chunk_count (chunks themselves are in staging).
    """
    async with track_event(
        workflow_run_id=input.workflow_run_id,
        document_id=input.document_id,
        workspace_id=input.workspace_id,
        event_type="conversation_chunked",
    ):
        return await _chunk_conversation_inner(input)


async def _chunk_conversation_inner(input: ChunkConversationInput) -> ChunkConversationOutput:
    """Inner implementation for conversation chunking (wrapped by lineage tracking)."""
    from src.config.settings import get_settings
    from src.temporal.activities.chunk import estimate_tokens
    from src.temporal.shared_services import get_db_service, get_staging_service

    if not input.redacted_turns:
        return ChunkConversationOutput(chunk_count=0)

    settings = get_settings()
    max_size = input.max_chunk_size if input.max_chunk_size is not None else settings.max_chunk_size
    overlap = input.chunk_overlap if input.chunk_overlap is not None else settings.chunk_overlap

    turn_meta_by_id = {m.turn_id: m for m in input.turn_meta}

    # Global chunk_index offset (append mode): read ONCE, before any chunk is
    # produced -- see module docstring "Chunk indexing (append mode)" for why
    # this must happen here and not inside store_in_postgresql/
    # store_in_weaviate. 0 for a brand-new conversation (no row yet).
    db = get_db_service()
    start_index = await db.get_document_chunk_count(input.document_id)

    # Single pass: for each turn (in order), split its text into one or more
    # word-boundary-respecting pieces (usually exactly one -- see
    # _split_turn_text), stamping every piece with that SAME turn's
    # turn_index/role/ts/client and a globally-continuing chunk_index.
    #
    # Written directly to the staged dict shape store.py's _risk_metadata
    # already promotes into document_chunks.metadata / Weaviate properties
    # (same promote-from-staged-dict pattern #44/#129 established for
    # content_risk/chunking_strategy, extended here rather than forked).
    chunks_dicts: list[dict] = []
    chunk_index = start_index
    for turn_index, turn in enumerate(input.redacted_turns):
        meta = turn_meta_by_id.get(turn.turn_id)
        ts = meta.ts if meta is not None else ""
        client = meta.client if meta is not None else None

        for content, start_char, end_char in _split_turn_text(turn.text, max_size, overlap):
            chunks_dicts.append(
                {
                    "document_id": input.document_id,
                    "content": content,
                    "chunk_index": chunk_index,
                    "start_char": start_char,
                    "end_char": end_char,
                    "token_count": estimate_tokens(content),
                    "content_risk": "none",
                    "content_risk_reasons": [],
                    "chunking_strategy": "conversation_turn",
                    # Conversation attribution (#306).
                    "turn_index": turn_index,
                    "turn_id": turn.turn_id,
                    "role": turn.role,
                    "turn_ts": ts,
                    "client": client,
                }
            )
            chunk_index += 1

    logger.info(
        "chunk_conversation: batch chunked",
        document_id=input.document_id,
        turns_in=len(input.redacted_turns),
        chunk_count=len(chunks_dicts),
        start_index=start_index,
    )

    staging = get_staging_service()
    staging.write_chunks(input.workflow_run_id, chunks_dicts)

    return ChunkConversationOutput(chunk_count=len(chunks_dicts))


def _split_turn_text(text: str, max_size: int, overlap: int) -> list[tuple[str, int, int]]:
    """Split ONE turn's text into (content, start_char, end_char) pieces.

    Word-boundary-respecting, matching chunk.py's `_chunk_by_size` -- the
    common case (a turn shorter than `max_size`) returns exactly one piece
    spanning the whole turn untouched. A turn is NEVER merged with another
    turn's text (see module docstring "Turn-aware chunking").
    """
    if not text:
        return []
    if len(text) <= max_size:
        return [(text, 0, len(text))]

    pieces: list[tuple[str, int, int]] = []
    start = 0
    while start < len(text):
        end = min(start + max_size, len(text))
        if end < len(text):
            last_space = text.rfind(" ", start, end)
            if last_space > start:
                end = last_space
        piece = text[start:end].strip()
        if piece:
            pieces.append((piece, start, end))
        start = end - overlap if end - overlap > start else end
    return pieces
