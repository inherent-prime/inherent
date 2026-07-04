"""Text chunking activity for splitting documents into processable chunks.

Reads text from staging (instead of receiving it via gRPC) and writes
chunks back to staging.
"""

import re

import structlog
from temporalio import activity

from src.temporal.lineage import track_event
from src.temporal.models import ChunkData, ChunkTextInput, ChunkTextOutput

logger = structlog.get_logger(__name__)

# Characters-per-token assumption used to translate the embedding model's
# token budget (embedding_max_tokens) into a character budget for the
# character-based chunkers below. ~4 chars/token is the well-known rule of
# thumb for English BPE tokenizers and matches the chars/4 branch of
# estimate_tokens(), keeping the two consistent.
_CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    """Estimate the number of model tokens in ``text`` without a tokenizer.

    Token-count formula (no new dependencies):

        est_tokens = ceil(max(words * 1.3, chars / 4))

    Rationale:
    - ``words * 1.3`` captures sub-word splitting: most BPE tokenizers emit a
      bit more than one token per whitespace word.
    - ``chars / 4`` is the classic ~4-chars-per-token rule and dominates for
      text with few spaces (code, long tokens, CJK-ish content).
    Taking the max of both makes the estimate conservative (it rarely
    under-counts), which is what we want when enforcing an embedding token
    budget: better to over-estimate and split than to over-run the model and
    have TEI silently truncate.
    """
    import math

    if not text:
        return 0
    words = len(text.split())
    chars = len(text)
    return int(math.ceil(max(words * 1.3, chars / _CHARS_PER_TOKEN)))


def _token_budget_char_cap(embedding_max_tokens: int) -> int:
    """Convert an embedding token budget into a max character count per chunk.

    estimate_tokens() takes the max of two branches, so a character cap is only
    safe if it keeps BOTH branches at or under the budget T:

    - chars branch: chars / 4 <= T  =>  chars <= 4T
    - words branch: words * 1.3 <= T. The worst case (most words per char) is
      single-character words separated by spaces, where chars ~= 2*words, i.e.
      words ~= chars / 2. So 1.3 * chars/2 <= T  =>  chars <= 2T / 1.3.

    The binding constraint is the smaller (words-branch) cap, so we take the min
    of both. This guarantees estimate_tokens(chunk) <= T for any chunk we emit
    at or under this character length, instead of relying on TEI truncation.
    """
    chars_branch = embedding_max_tokens * _CHARS_PER_TOKEN
    words_branch = int((2 * embedding_max_tokens) / 1.3)
    return max(1, min(chars_branch, words_branch))


@activity.defn
async def chunk_text(input: ChunkTextInput) -> ChunkTextOutput:
    """Split text into chunks based on the configured strategy.

    Chunking strategies:
    - sentences: Split by sentence boundaries with configurable overlap
    - paragraphs: Split by double newlines (no overlap)
    - tokens: Fixed-size chunks with overlap

    Reads text from staging and writes chunks back to staging. Only the
    chunk count passes through gRPC.

    Args:
        input: Contains workflow_run_id, document_id, strategy, max_chunk_size, and overlap

    Returns:
        ChunkTextOutput with chunk_count (chunks themselves are in staging)
    """
    async with track_event(
        workflow_run_id=input.workflow_run_id,
        document_id=input.document_id,
        workspace_id=input.workspace_id,
        event_type="text_chunked",
    ):
        return await _chunk_text_inner(input)


async def _chunk_text_inner(input: ChunkTextInput) -> ChunkTextOutput:
    """Inner implementation for text chunking (wrapped by lineage tracking)."""
    from src.temporal.shared_services import get_staging_service

    staging = get_staging_service()

    # Read text from staging
    text = staging.read_text(input.workflow_run_id)

    from src.config.settings import get_settings

    settings = get_settings()

    document_id = input.document_id
    # Resolve chunking config HERE (not in @workflow.run, a Temporal determinism
    # anti-pattern, #38). Per-document overrides on the input win; otherwise fall
    # back to settings. The activity already reads settings for the token budget.
    strategy = input.strategy or settings.chunking_strategy
    overlap = input.chunk_overlap if input.chunk_overlap is not None else settings.chunk_overlap
    requested_max = (
        input.max_chunk_size if input.max_chunk_size is not None else settings.max_chunk_size
    )

    # Model-aware sizing: never let a single chunk exceed the embedding
    # model's token budget. We translate embedding_max_tokens into a character
    # cap (see _token_budget_char_cap) and clamp the requested max_chunk_size
    # to it, so estimated tokens stay under the budget instead of relying on
    # TEI's silent server-side truncation.
    char_cap = _token_budget_char_cap(settings.embedding_max_tokens)
    max_size = min(requested_max, char_cap)

    logger.info(
        "Chunking text",
        document_id=document_id,
        strategy=strategy,
        text_length=len(text),
        requested_max_chunk_size=requested_max,
        effective_max_chunk_size=max_size,
        embedding_max_tokens=settings.embedding_max_tokens,
    )

    if not text:
        return ChunkTextOutput(chunk_count=0)

    chunks: list[ChunkData] = []

    if strategy == "sentences":
        chunks = _chunk_by_sentences(text, document_id, max_size, overlap)
    elif strategy == "paragraphs":
        chunks = _chunk_by_paragraphs(text, document_id, max_size)
    else:  # tokens (default)
        chunks = _chunk_by_size(text, document_id, max_size, overlap)

    # Populate a consistent, model-aware token estimate for every chunk so the
    # value stored in PostgreSQL/Weaviate matches the budget we enforced above
    # (replaces the old naive len(content.split()) used at storage time).
    for c in chunks:
        c.token_count = estimate_tokens(c.content)

    # RAG-poisoning / prompt-injection risk signal (#44). Computed per chunk so
    # individual poisoned chunks can be surfaced even within an otherwise benign
    # document. NON-BLOCKING: this never raises and never drops a chunk; it only
    # tags the chunk so search/audit can weigh it.
    from src.services.quality import compute_content_risk

    for c in chunks:
        risk_level, risk_reasons = compute_content_risk(c.content)
        c.content_risk = risk_level
        c.content_risk_reasons = risk_reasons

    logger.info(
        "Text chunked successfully",
        document_id=document_id,
        chunk_count=len(chunks),
        max_chunk_token_estimate=max((c.token_count for c in chunks), default=0),
    )

    # Run data quality checks on chunks
    from src.services.quality import DataQualityService

    chunks_for_check = [
        {
            "content": c.content,
            "chunk_index": c.chunk_index,
        }
        for c in chunks
    ]
    quality = DataQualityService()
    quality_results = quality.check_chunks(chunks_for_check, filename=document_id)
    quality.log_results(quality_results, document_id=document_id)
    if quality.has_critical_failure(quality_results):
        raise RuntimeError(f"Chunk quality check failed for {document_id}: 0 chunks produced")

    # Write chunks to staging as list of dicts
    chunks_dicts = [
        {
            "document_id": c.document_id,
            "content": c.content,
            "chunk_index": c.chunk_index,
            "start_char": c.start_char,
            "end_char": c.end_char,
            "token_count": c.token_count,
            # Risk signal (#44) carried through staging to the store activities.
            "content_risk": c.content_risk,
            "content_risk_reasons": c.content_risk_reasons,
        }
        for c in chunks
    ]
    staging.write_chunks(input.workflow_run_id, chunks_dicts)

    return ChunkTextOutput(chunk_count=len(chunks))


def _chunk_by_size(text: str, document_id: str, max_size: int, overlap: int) -> list[ChunkData]:
    """Split text into fixed-size chunks with overlap."""
    chunks = []
    start = 0
    chunk_index = 0

    while start < len(text):
        end = min(start + max_size, len(text))

        # Try to break at word boundary
        if end < len(text):
            last_space = text.rfind(" ", start, end)
            if last_space > start:
                end = last_space

        chunk_text = text[start:end].strip()
        if chunk_text:
            chunks.append(
                ChunkData(
                    document_id=document_id,
                    content=chunk_text,
                    chunk_index=chunk_index,
                    start_char=start,
                    end_char=end,
                )
            )
            chunk_index += 1

        # Move to next chunk with overlap
        start = end - overlap if end - overlap > start else end

    return chunks


def _chunk_by_sentences(
    text: str, document_id: str, max_size: int, overlap: int
) -> list[ChunkData]:
    """Split text into chunks by sentences.

    Offsets map to the real source positions (#25): each sentence's span in the
    source is precomputed, so a chunk's start_char/end_char come from its first
    and last sentence spans rather than accumulated join-length guesses. The
    source span is preserved even with overlap or non-single-space separators.
    """
    sentences = re.split(r"(?<=[.!?])\s+", text)

    # Precompute each sentence's (start, end) in the source by scanning forward.
    spans: list[tuple[int, int]] = []
    cursor = 0
    for sentence in sentences:
        idx = text.find(sentence, cursor) if sentence else cursor
        if idx == -1:
            idx = cursor
        spans.append((idx, idx + len(sentence)))
        cursor = idx + len(sentence)

    chunks: list[ChunkData] = []
    current: list[int] = []  # sentence indices in the current chunk
    current_size = 0
    chunk_index = 0

    def _emit(indices: list[int]) -> None:
        nonlocal chunk_index
        content = " ".join(sentences[i] for i in indices).strip()
        if not content:
            return
        chunks.append(
            ChunkData(
                document_id=document_id,
                content=content,
                chunk_index=chunk_index,
                start_char=spans[indices[0]][0],
                end_char=spans[indices[-1]][1],
            )
        )
        chunk_index += 1

    for i, sentence in enumerate(sentences):
        sentence_len = len(sentence)

        if current_size + sentence_len > max_size and current:
            _emit(current)

            # Keep some trailing sentences (by size) for overlap.
            overlap_indices: list[int] = []
            overlap_size = 0
            for j in reversed(current):
                if overlap_size + len(sentences[j]) <= overlap:
                    overlap_indices.insert(0, j)
                    overlap_size += len(sentences[j])
                else:
                    break
            current = overlap_indices
            current_size = overlap_size

        current.append(i)
        current_size += sentence_len

    if current:
        _emit(current)

    return chunks


def _chunk_by_paragraphs(text: str, document_id: str, max_size: int) -> list[ChunkData]:
    """Split text into chunks by paragraphs.

    Offsets map to real source positions (#25): each (stripped) paragraph's span
    in the source is located by a forward scan, and a chunk's start/end come from
    its first/last paragraph spans.
    """
    raw_paragraphs = text.split("\n\n")

    # Build (paragraph_text, start, end) for each non-empty stripped paragraph.
    entries: list[tuple[str, int, int]] = []
    cursor = 0
    for raw in raw_paragraphs:
        para = raw.strip()
        # Advance the cursor over the raw block (+2 for the "\n\n" separator).
        block_start = cursor
        cursor += len(raw) + 2
        if not para:
            continue
        idx = text.find(para, block_start)
        if idx == -1:
            idx = block_start
        entries.append((para, idx, idx + len(para)))

    chunks: list[ChunkData] = []
    current: list[tuple[str, int, int]] = []
    current_size = 0
    chunk_index = 0

    def _emit(items: list[tuple[str, int, int]]) -> None:
        nonlocal chunk_index
        content = "\n\n".join(p for p, _s, _e in items).strip()
        if not content:
            return
        chunks.append(
            ChunkData(
                document_id=document_id,
                content=content,
                chunk_index=chunk_index,
                start_char=items[0][1],
                end_char=items[-1][2],
            )
        )
        chunk_index += 1

    for para, s, e in entries:
        para_len = len(para)
        if current_size + para_len > max_size and current:
            _emit(current)
            current = []
            current_size = 0
        current.append((para, s, e))
        current_size += para_len

    if current:
        _emit(current)

    return chunks
