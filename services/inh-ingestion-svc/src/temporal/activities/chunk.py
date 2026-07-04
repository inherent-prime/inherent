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
    """Split text into chunks by sentences."""
    sentences = re.split(r"(?<=[.!?])\s+", text)

    chunks = []
    current_chunk: list[str] = []
    current_size = 0
    chunk_index = 0
    start_char = 0

    for sentence in sentences:
        sentence_len = len(sentence)

        # If adding this sentence exceeds max size, save current chunk
        if current_size + sentence_len > max_size and current_chunk:
            chunk_text = " ".join(current_chunk).strip()
            if chunk_text:
                chunks.append(
                    ChunkData(
                        document_id=document_id,
                        content=chunk_text,
                        chunk_index=chunk_index,
                        start_char=start_char,
                        end_char=start_char + len(chunk_text),
                    )
                )
                chunk_index += 1
                start_char += len(chunk_text) + 1

            # Keep some sentences for overlap
            overlap_sentences: list[str] = []
            overlap_size = 0
            for s in reversed(current_chunk):
                if overlap_size + len(s) <= overlap:
                    overlap_sentences.insert(0, s)
                    overlap_size += len(s)
                else:
                    break

            current_chunk = overlap_sentences
            current_size = overlap_size

        current_chunk.append(sentence)
        current_size += sentence_len

    # Save final chunk
    if current_chunk:
        chunk_text = " ".join(current_chunk).strip()
        if chunk_text:
            chunks.append(
                ChunkData(
                    document_id=document_id,
                    content=chunk_text,
                    chunk_index=chunk_index,
                    start_char=start_char,
                    end_char=start_char + len(chunk_text),
                )
            )

    return chunks


def _chunk_by_paragraphs(text: str, document_id: str, max_size: int) -> list[ChunkData]:
    """Split text into chunks by paragraphs."""
    paragraphs = text.split("\n\n")

    chunks = []
    current_chunk: list[str] = []
    current_size = 0
    chunk_index = 0
    start_char = 0

    for para in paragraphs:
        para = para.strip()
        if not para:
            continue

        para_len = len(para)

        # If adding this paragraph exceeds max size, save current chunk
        if current_size + para_len > max_size and current_chunk:
            chunk_text = "\n\n".join(current_chunk).strip()
            if chunk_text:
                chunks.append(
                    ChunkData(
                        document_id=document_id,
                        content=chunk_text,
                        chunk_index=chunk_index,
                        start_char=start_char,
                        end_char=start_char + len(chunk_text),
                    )
                )
                chunk_index += 1
                start_char += len(chunk_text) + 2

            current_chunk = []
            current_size = 0

        current_chunk.append(para)
        current_size += para_len

    # Save final chunk
    if current_chunk:
        chunk_text = "\n\n".join(current_chunk).strip()
        if chunk_text:
            chunks.append(
                ChunkData(
                    document_id=document_id,
                    content=chunk_text,
                    chunk_index=chunk_index,
                    start_char=start_char,
                    end_char=start_char + len(chunk_text),
                )
            )

    return chunks
