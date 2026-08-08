"""Text chunking activity for splitting documents into processable chunks.

Reads text from staging (instead of receiving it via gRPC) and writes
chunks back to staging.

Format-aware chunking (#129)
-----------------------------
Before this, `_chunk_text_inner` picked sentences/paragraphs/tokens purely
from config -- the same rule for a one-page memo and a 10,000-row XLSX.
Measured cost of that (see the #129 issue body, "tokens" config): a 10k-row
XLSX flattened to 529,057 chars produced 669 positional token chunks, of
which exactly ONE carried the header row and exactly ONE carried the
"## Sheet:" line.

The SHIPPED default is worse, not milder: `CHUNKING_STRATEGY=sentences`
(settings.py, .env.example) splits on `[.!?]` -- and a flattened XLSX's
pipe-delimited rows contain NO sentence-ending punctuation at all, so the
"sentence" splitter never finds a boundary and the entire 10,000-row sheet
becomes exactly ONE chunk (measured: 510,258 chars, one chunk, for a
similarly-shaped 10k-row sheet). `embedder.py`'s `_post_embed` calls TEI
with `truncate=True` (the all-MiniLM-L6-v2 model's 256-token input limit) --
so ~99.8% of that single chunk is silently discarded before a vector is
ever computed. This is not a hypothetical worst case; it is what every
default-configuration deployment does today. Separately: an .eml's
From/To/Subject/Date block appears once at the top, so any mid-body chunk
of a longer email carries no sender, no subject, no thread. A fragment that
cannot be interpreted or cited on its own -- or, worse, is never embedded at
all -- is the defect this closes.

Resolution precedence (per the #129 issue's proposed contract):

    per-document override (``ChunkTextInput.strategy``)
        > registry ``chunking_hint`` (``inh_contracts.FILE_TYPE_REGISTRY``, #117)
        > global config (``settings.chunking_strategy``)

The registry's ``ChunkingHint`` is a CLOSED 4-value vocabulary (prose /
tabular / structured / media, see ``inh_contracts.file_types``) -- not the
"markdown_headers / rows / code / sections" per-format names the #129 issue
sketched before #117 actually landed. Each hint maps to ONE format-aware
strategy below, chosen to fit every format currently carrying that hint
rather than one strategy per format:

- ``tabular`` (csv, xlsx) -> ``_chunk_by_rows``: never splits a row/line in
  half; every chunk carries the nearest table header row (and XLSX's
  "## Sheet: <name>" heading, when present) so a chunk showing "6009" also
  shows which column that is and which sheet it came from.
- ``structured`` (json, pptx) -> ``_chunk_by_sections``: splits at the
  extractor's own "## " section markers (PPTX's "## Slide N: Title"); a
  section too large for one chunk is sliced further with the heading carried
  into every slice. Degrades to plain size-based chunking when no "## "
  markers exist at all (JSON has none) -- the hint and the actual text shape
  can legitimately disagree, and that must never crash or emit one
  unbounded chunk.
- ``prose`` (txt, markdown, docx, eml, epub, rtf, odt, pdf, html) ->
  ``_chunk_prose``: unchanged sentence chunking UNLESS the text opens with a
  "Key: value" header block (exactly the shape ``_extract_eml_text`` always
  emits for From/To/Cc/Date/Subject) -- when it does, that block is carried
  into every chunk, not just the one sentence-window that happens to contain
  it positionally. Format-agnostic by design (keyed on TEXT SHAPE, not on
  "this is specifically an .eml"), so the vast majority of prose documents
  that have no such block chunk byte-for-byte as before #129.
- ``media`` (png) -> plain size-based chunking. OCR/placeholder output is
  short and unstructured; there is no header/section shape to preserve.

Every chunk records which strategy actually produced it in
``ChunkData.chunking_strategy`` (store.py promotes it into the persisted
``metadata`` JSONB, alongside the existing #44 risk signal) so the #34 eval
suite can attribute retrieval quality per strategy, not just per file type.
"""

import re

import structlog
from temporalio import activity

from src.temporal.lineage import track_event
from src.temporal.models import ChunkData, ChunkTextInput, ChunkTextOutput

logger = structlog.get_logger(__name__)

# Injected context (table header, section heading, email header block) is
# NOT part of the chunk's real source span -- it is truncated to this many
# characters so it can never itself blow the max_chunk_size budget (the
# adversarial "200 columns" / "huge Cc list" case: without a cap, the
# injected context alone could exceed the whole chunk budget).
_MAX_INJECTED_CONTEXT_CHARS = 500

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

    # Strategy resolution precedence (#129): per-document override > registry
    # chunking_hint > global config. `input.strategy` is the pre-#129 explicit
    # per-document override -- when the caller set it, it wins outright and
    # format-aware dispatch below never runs at all (a caller that explicitly
    # asked for "paragraphs" gets paragraphs regardless of what the registry
    # thinks this content type should look like). Otherwise, resolve the
    # registry's chunking_hint from `content_type` -- resolving to None (no
    # content_type given, or a content type with no registry entry) is a
    # deliberate, silent degrade to the pre-#129 global-config dispatch, not
    # an error: an unregistered content type reaching this activity at all
    # already failed loudly at extraction (#117's UnregisteredContentType),
    # so by the time chunking runs, "no hint" just means "an older caller
    # that hasn't been updated to pass content_type yet".
    chunking_hint = None
    if input.strategy is None and input.content_type:
        from inh_contracts.file_types import get_spec_for_mime

        spec = get_spec_for_mime(input.content_type)
        chunking_hint = spec.chunking_hint if spec is not None else None

    logger.info(
        "Chunking text",
        document_id=document_id,
        override_strategy=input.strategy,
        content_type=input.content_type,
        resolved_chunking_hint=chunking_hint,
        text_length=len(text),
        requested_max_chunk_size=requested_max,
        effective_max_chunk_size=max_size,
        embedding_max_tokens=settings.embedding_max_tokens,
    )

    if not text:
        return ChunkTextOutput(chunk_count=0)

    chunks: list[ChunkData] = []
    # The name recorded in every chunk's `chunking_strategy` field for eval
    # attribution (#129 acceptance criterion) -- always the ACTUAL function
    # dispatched to, never the hint itself (a "structured" hint that
    # degrades to token chunking because the text has no section markers
    # must say "tokens", not "sections", or the eval suite would attribute
    # quality to a strategy that never ran).
    chunking_strategy_used: str

    if input.strategy is not None:
        # Explicit per-document override -- byte-for-byte the pre-#129
        # 3-way dispatch, untouched.
        chunking_strategy_used = input.strategy
        if input.strategy == "sentences":
            chunks = _chunk_by_sentences(text, document_id, max_size, overlap)
        elif input.strategy == "paragraphs":
            chunks = _chunk_by_paragraphs(text, document_id, max_size)
        else:  # tokens
            chunks = _chunk_by_size(text, document_id, max_size, overlap)
    elif chunking_hint == "tabular":
        chunks = _chunk_by_rows(text, document_id, max_size)
        chunking_strategy_used = "rows"
    elif chunking_hint == "structured":
        # Computed once here and passed down (#129 follow-up item 12) so
        # _chunk_by_sections doesn't re-scan the whole text a second time
        # just to re-derive the same flag -- up to 5,000,000 chars per the
        # extraction cost guard.
        has_section_markers = _has_section_markers(text)
        chunks = _chunk_by_sections(
            text, document_id, max_size, overlap, _has_markers=has_section_markers
        )
        chunking_strategy_used = "sections" if has_section_markers else "tokens"
    elif chunking_hint == "prose":
        # Same single-scan reasoning as above (#129 follow-up item 12).
        header = _detect_header_block(text)
        chunks = _chunk_prose(text, document_id, max_size, overlap, _header=header)
        chunking_strategy_used = "prose_header" if header[0] else "sentences"
    elif chunking_hint == "media":
        # OCR/placeholder text is short and unstructured -- no header or
        # section shape worth preserving; plain size-based chunking.
        chunks = _chunk_by_size(text, document_id, max_size, overlap)
        chunking_strategy_used = "tokens"
    else:
        # No content_type resolvable to a registry hint -- the pre-#129
        # global-config 3-way dispatch, untouched.
        strategy = settings.chunking_strategy
        chunking_strategy_used = strategy
        if strategy == "sentences":
            chunks = _chunk_by_sentences(text, document_id, max_size, overlap)
        elif strategy == "paragraphs":
            chunks = _chunk_by_paragraphs(text, document_id, max_size)
        else:  # tokens (default)
            chunks = _chunk_by_size(text, document_id, max_size, overlap)

    for c in chunks:
        c.chunking_strategy = chunking_strategy_used

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
            # Which strategy actually produced this chunk (#129), for eval
            # attribution -- carried through staging the same way the risk
            # signal is, then promoted into the persisted metadata JSONB by
            # store.py.
            "chunking_strategy": c.chunking_strategy,
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


# ---------------------------------------------------------------------------
# Format-aware strategies (#129) -- see the module docstring for the hint ->
# strategy mapping and why each one is shaped the way it is.
# ---------------------------------------------------------------------------


def _bounded_context(text: str, max_size: int = _MAX_INJECTED_CONTEXT_CHARS) -> str:
    """Cap injected context (table header, section heading) so it can never
    blow the chunk budget by itself -- the adversarial "200 columns" case.
    Never truncates the ORIGINAL occurrence of this text in the source (that
    one is a real, untouched slice); only truncates the COPY being
    re-injected into a later chunk.

    `max_size` (#129 follow-up, blocker 2) SCALES the cap down for a small
    configured chunk budget: the absolute default (`_MAX_INJECTED_CONTEXT_CHARS`,
    500) is only safe when `max_size` is comfortably bigger than that. A
    caller with a small `max_size` (a tuned `MAX_CHUNK_SIZE`, or a small
    embedding token budget via `_token_budget_char_cap`) that still got the
    absolute 500-char cap could see injected context alone consume the
    ENTIRE budget, collapsing `_slice_oversized_line`'s per-slice body
    budget toward zero (measured: a 747-char header against a 393-char
    `max_size` produced 3,883 one-line chunks from 3,908 source chars).
    Capping context to at most a THIRD of `max_size` (with a small absolute
    floor so a truncation marker is still legible) keeps context injection
    from ever dominating a chunk's budget, at any configured size.
    """
    cap = max(20, min(max_size, max_size // 3 if max_size > 60 else max_size))
    cap = min(cap, _MAX_INJECTED_CONTEXT_CHARS)
    if len(text) <= cap:
        return text
    dropped = len(text) - cap
    return text[:cap] + f"...[+{dropped} chars]"


def _line_entries(text: str) -> list[tuple[str, int, int]]:
    """(line_without_newline, start_char, end_char) for every line in `text`,
    with offsets exact against the source (same #25 contract as the
    sentence/paragraph span-tracking above) -- computed by walking
    `splitlines(keepends=True)` so the newline's own width is accounted for
    without any join-length guessing."""
    entries = []
    cursor = 0
    for raw in text.splitlines(keepends=True):
        stripped = raw.rstrip("\r\n")
        start = cursor
        end = start + len(stripped)
        entries.append((stripped, start, end))
        cursor += len(raw)
    return entries


def _slice_oversized_line(
    line: str,
    start: int,
    end: int,
    document_id: str,
    max_size: int,
    context: str,
    first_index: int,
) -> list[ChunkData]:
    """Bounded-length character slices of ONE line that alone exceeds
    `max_size` -- the adversarial "a single row/section line exceeds
    max_chunk_size" case (e.g. a 200-column row, or one pathological table
    cell). Every slice still carries `context` (the table/section header)
    so the fragment stays self-describing even split apart. Offsets map
    exactly to the real sliced span of `line` in the source -- nothing is
    dropped, the slices concatenate back to `line` byte-for-byte.

    Real forward progress per slice is guaranteed to be at least
    `max_size // 5` (#129 follow-up blocker 2) regardless of how large the
    injected context is. `_bounded_context` already scales its own cap with
    `max_size`, but two independently-scaled pieces (a tabular chunk's sheet
    heading + header row) can still combine to leave very little of the
    budget for real content when `max_size` itself is small -- an absolute
    cap alone let `budget` collapse to 1, turning one oversized row into one
    chunk PER CHARACTER (measured: a 747-char header against a 393-char
    `max_size` produced 3,883 one-line chunks from a 3,908-char sheet). This
    floor is the second, independent guarantee: context is shrunk FURTHER
    than its own scaled cap, if needed, before ever letting per-slice
    progress fall below it.
    """
    out: list[ChunkData] = []
    floor = max(1, max_size // 5)
    context_block = f"{_bounded_context(context, max_size)}\n\n" if context else ""
    if max_size - len(context_block) < floor:
        # Even the already-scaled context still leaves less than the floor
        # -- shrink it further (context is a nice-to-have; guaranteed
        # forward progress on the actual data is not negotiable) rather than
        # let `budget` collapse toward zero.
        context_cap = max(0, max_size - floor - 2)  # -2 for the "\n\n" separator
        trimmed = context[:context_cap] if context else ""
        context_block = f"{trimmed}\n\n" if trimmed else ""
    # Reserve room for the injected context so the FINAL content (context +
    # slice) still respects max_size, not just the slice alone -- but never
    # below `floor`.
    budget = max(floor, max_size - len(context_block))
    idx = first_index
    pos = 0
    while pos < len(line):
        piece_end = min(pos + budget, len(line))
        piece = line[pos:piece_end]
        out.append(
            ChunkData(
                document_id=document_id,
                content=(context_block + piece).strip(),
                chunk_index=idx,
                start_char=start + pos,
                end_char=start + piece_end,
            )
        )
        idx += 1
        pos = piece_end
    return out


# XLSX's own sheet-boundary marker (see extract.py's `_extract_xlsx_text`,
# which emits this exact prefix for both the initial heading and its
# periodic "(continued)" repeats). CSV's `text_passthrough` extraction has
# no such marker at all -- `_chunk_by_rows` below treats its absence as "no
# sheet boundaries, just a header row", which is exactly CSV's shape.
_SHEET_HEADING_PREFIX = "## Sheet:"

# extract.py re-emits the sheet heading periodically with this suffix as a
# cheap, extraction-time insurance marker (see _XLSX_HEADER_REPEAT_ROWS).
# That suffix is plumbing about the RAW extracted text, not something worth
# repeating into every chunk's INJECTED copy of the heading (#129 follow-up
# item 7) -- the line as it actually appears in the source keeps it, only
# the tracked copy used for later re-injection is normalized.
_SHEET_HEADING_CONTINUED_SUFFIX = " (continued)"


def _chunk_by_rows(text: str, document_id: str, max_size: int) -> list[ChunkData]:
    """Row-based chunking for the ``tabular`` hint (CSV, XLSX) (#129).

    Never splits a row across two chunks (unless the row alone exceeds
    `max_size` -- see `_slice_oversized_line`). Every chunk carries the
    table's header row, and XLSX's current "## Sheet: <name>" heading when
    present, UNLESS the chunk already contains it naturally (the first
    group of a sheet/document already includes its own heading and header
    row as real content -- injecting a second copy would be pure waste).

    This subsumes extract.py's `_XLSX_HEADER_REPEAT_ROWS` "cheap insurance"
    (periodic re-emission every 50 rows): that extraction-time repeat still
    fires and is harmless (it just becomes one more real row this chunker
    groups normally), but it is no longer load-bearing -- EVERY chunk gets
    header context now, not just the ones that happen to land near a
    50-row boundary.

    Offsets (`start_char`/`end_char`) cover only the REAL row span in the
    source -- injected header/heading context is prepended to `content` but
    is not part of the offset range (see `test_chunk_format_aware.py`'s
    relaxed invariant: the source span is a substring of `content`, not
    equal to it, whenever context was injected).

    Packing reserves room for injected context UP FRONT for any group that
    will need it (#129 follow-up item 5): without this, content could exceed
    `max_size` (measured 1,313 chars against a 787 cap) because context was
    added ON TOP of an already-full group instead of the group being packed
    smaller to make room for it.
    """
    entries = _line_entries(text)
    chunks: list[ChunkData] = []
    chunk_index = 0

    group: list[tuple[str, int, int]] = []
    group_size = 0
    group_has_context = False
    sheet_heading: str | None = None
    header_row: str | None = None

    def context_prefix() -> str:
        parts = [p for p in (sheet_heading, header_row) if p]
        return "\n".join(_bounded_context(p, max_size) for p in parts)

    def pack_cap() -> int:
        # The group currently open only needs the injection reserved for if
        # it will NOT already contain the context naturally at flush time.
        # The first group of a sheet (heading, then header_row, both real
        # content) needs no reservation -- their length is already counted
        # in group_size by the normal packing loop below.
        if group_has_context:
            return max_size
        ctx = context_prefix()
        return max(1, max_size - len(ctx) - 2) if ctx else max_size  # -2 for "\n\n"

    def flush() -> None:
        nonlocal group, group_size, group_has_context, chunk_index
        if not group:
            return
        body = "\n".join(t for t, _s, _e in group)
        start = group[0][1]
        end = group[-1][2]
        ctx = context_prefix()
        content = body if (not ctx or group_has_context) else f"{ctx}\n\n{body}"
        chunks.append(
            ChunkData(
                document_id=document_id,
                content=content.strip(),
                chunk_index=chunk_index,
                start_char=start,
                end_char=end,
            )
        )
        chunk_index += 1
        group, group_size, group_has_context = [], 0, False

    for line, start, end in entries:
        if not line.strip():
            continue  # tabular text has no meaningful blank lines; skip defensively

        if line.startswith(_SHEET_HEADING_PREFIX):
            # New sheet boundary -- flush whatever the previous sheet was
            # accumulating so a chunk never straddles two sheets, then start
            # the new sheet's group with its own heading (so it never needs
            # the heading injected back into itself).
            flush()
            sheet_heading = (
                line[: -len(_SHEET_HEADING_CONTINUED_SUFFIX)]
                if line.endswith(_SHEET_HEADING_CONTINUED_SUFFIX)
                else line
            )
            header_row = None
            group.append((line, start, end))
            group_size = len(line)
            group_has_context = True
            continue

        if len(line) > max_size:
            # Adversarial: a single row (e.g. 200 columns) alone exceeds the
            # budget.
            #
            # Do NOT adopt this oversized row as `header_row` (#129 follow-up
            # item 6's root cause): the row about to be sliced becoming its
            # OWN injected "header context" would duplicate its own giant
            # text into every one of its own slices. A later, normal-sized
            # row (if any) still becomes header_row for subsequent chunks --
            # `header_row` is deliberately left untouched here.
            #
            # Merge a PENDING context-only group (just the sheet heading, or
            # heading+header_row, with zero real data rows) into the slices
            # instead of flushing it as a useless standalone orphan chunk
            # (#129 follow-up item 6: measured a 56-char zero-data-row
            # orphan chunk emitted right before the oversized row's slices).
            context_only_lines = {t for t in (sheet_heading, header_row) if t}
            is_orphan = bool(group) and all(t in context_only_lines for t, _s, _e in group)
            if is_orphan:
                group, group_size, group_has_context = [], 0, False
            else:
                flush()
            slices = _slice_oversized_line(
                line, start, end, document_id, max_size, context_prefix(), chunk_index
            )
            chunks.extend(slices)
            chunk_index = slices[-1].chunk_index + 1
            continue

        first_header_sighting = header_row is None
        if first_header_sighting:
            header_row = line

        line_len = len(line) + 1  # +1 for the join separator
        if group and group_size + line_len > pack_cap():
            flush()
        group.append((line, start, end))
        group_size += line_len
        if first_header_sighting:
            group_has_context = True

    flush()
    return chunks


# Any line starting with this prefix is a section boundary in "structured"
# text -- matches PPTX's "## Slide N: Title" markers (extract.py's
# `_extract_pptx_text`). JSON's pretty-printed output has no such marker at
# all, which is the deliberate "hint and text shape disagree" case
# `_chunk_by_sections` degrades from -- see `_has_section_markers`.
_SECTION_HEADING_PREFIX = "## "


def _has_section_markers(text: str) -> bool:
    """Whether `text` has at least one "## " section boundary -- the signal
    `_chunk_by_sections` uses to decide between real section-aware chunking
    and its plain size-based fallback (#129 adversarial case: the hint says
    "structured" but the extractor produced no section markers at all)."""
    return any(line.startswith(_SECTION_HEADING_PREFIX) for line, _s, _e in _line_entries(text))


def _chunk_by_sections(
    text: str,
    document_id: str,
    max_size: int,
    overlap: int,
    _has_markers: bool | None = None,
) -> list[ChunkData]:
    """Section-based chunking for the ``structured`` hint (JSON, PPTX) (#129).

    Splits at "## " section boundaries (PPTX's per-slide headings); every
    section boundary forces a chunk boundary (a chunk never silently merges
    two different sections, so a retrieved fragment is never ambiguous about
    which section it's from), and a section too large for one chunk on its
    own is sliced further with the heading carried into every slice (same
    `_slice_oversized_line` insurance as `_chunk_by_rows`'s oversized row).

    Falls back to plain size-based chunking (`_chunk_by_size`) when `text`
    has NO "## " markers at all -- the hint and the actual extracted text
    shape can legitimately disagree (JSON's pretty-printed body has no
    section markers; a future extractor change could drop them too), and
    that must degrade gracefully rather than crash or emit one unbounded
    chunk covering the whole document.

    Packing reserves room for injected context up front for a continuation
    group that will need the heading re-injected at flush time (#129
    follow-up item 5, same fix as `_chunk_by_rows`'s `pack_cap`).

    `_has_markers`: precomputed `_has_section_markers(text)` result, so the
    dispatcher in `_chunk_text_inner` (which needs the same flag to label
    `chunking_strategy`) and this function don't each independently scan the
    whole text (#129 follow-up item 12 -- up to 5,000,000 chars, per the
    extraction cost guard). Recomputed internally when omitted (`None`, the
    default), so this function stays independently callable/testable.
    """
    has_markers = _has_section_markers(text) if _has_markers is None else _has_markers
    if not has_markers:
        return _chunk_by_size(text, document_id, max_size, overlap)

    entries = _line_entries(text)
    chunks: list[ChunkData] = []
    chunk_index = 0

    group: list[tuple[str, int, int]] = []
    group_size = 0
    group_has_heading = False
    section_heading: str | None = None

    def pack_cap() -> int:
        # Symmetric with _chunk_by_rows's pack_cap (#129 follow-up item 5):
        # a continuation group that will need the section heading injected
        # at flush time must reserve room for it up front, or content can
        # end up larger than max_size.
        if group_has_heading or not section_heading:
            return max_size
        return max(1, max_size - len(_bounded_context(section_heading, max_size)) - 2)

    def flush() -> None:
        nonlocal group, group_size, group_has_heading, chunk_index
        if not group:
            return
        body = "\n".join(t for t, _s, _e in group)
        start = group[0][1]
        end = group[-1][2]
        content = (
            body
            if (not section_heading or group_has_heading)
            else f"{_bounded_context(section_heading, max_size)}\n\n{body}"
        )
        chunks.append(
            ChunkData(
                document_id=document_id,
                content=content.strip(),
                chunk_index=chunk_index,
                start_char=start,
                end_char=end,
            )
        )
        chunk_index += 1
        group, group_size, group_has_heading = [], 0, False

    for line, start, end in entries:
        if not line.strip():
            continue

        if line.startswith(_SECTION_HEADING_PREFIX):
            # A new section always starts a new chunk group -- see the
            # docstring: sections are never silently merged.
            flush()
            section_heading = line
            group.append((line, start, end))
            group_size = len(line)
            group_has_heading = True
            continue

        if len(line) > max_size:
            # Adversarial: one oversized line within a section (a huge
            # paragraph or table cell).
            #
            # Merge a pending context-only group (just the section heading,
            # zero real body lines) into the slices instead of flushing it
            # as a useless standalone orphan chunk (#129 follow-up item 6,
            # same fix as _chunk_by_rows's oversized-row path).
            is_orphan = (
                bool(group)
                and group_has_heading
                and all(t == section_heading for t, _s, _e in group)
            )
            if is_orphan:
                group, group_size, group_has_heading = [], 0, False
            else:
                flush()
            slices = _slice_oversized_line(
                line, start, end, document_id, max_size, section_heading or "", chunk_index
            )
            chunks.extend(slices)
            chunk_index = slices[-1].chunk_index + 1
            continue

        line_len = len(line) + 1
        if group and group_size + line_len > pack_cap():
            flush()
        group.append((line, start, end))
        group_size += line_len

    flush()
    return chunks


# A leading "Key: value" block -- exactly the shape `_extract_eml_text`
# always emits for From/To/Cc/Date/Subject (extract.py). Deliberately keyed
# on TEXT SHAPE, not on "this document is specifically an .eml", so it also
# helps any other prose format that front-loads structured metadata this
# way, and costs nothing when it doesn't apply.
_HEADER_LINE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9 _-]{0,40}:\s+\S")
# Hard bounds so detection can never misfire on a long document that merely
# happens to have several early "Key: value"-shaped lines with no actual
# header/body separation -- a runaway match would silently swallow real
# body content into what it thinks is a header block.
_MAX_HEADER_BLOCK_LINES = 8
_MAX_HEADER_BLOCK_SCAN_CHARS = 2000
# Require at least TWO consecutive "Key: value" lines before treating them
# as a real header block (#129 follow-up item 8). A single such line is a
# common, ordinary false-positive shape in genuine prose ("Note: this
# document is confidential.", a lone "Content-Type: application/json" line
# before a JSON body) -- one line proves nothing about document structure.
# Two or more consecutive matches is a much stronger signal: it's what a
# REAL header block (.eml's 2-5 From/To/Cc/Date/Subject lines) always looks
# like, and it's what most single-sentence false positives don't have.
#
# Residual, ACCEPTED false-positive class (not eliminated by this
# threshold, cost documented rather than chased further): a genuine
# multi-line "Key: value"-shaped block that isn't actually a document
# header -- a short definition list ("Alpha: ...\nBeta: ...\nGamma: ..."),
# or a 2-line config file ("host: ...\nport: ..."). These are structurally
# indistinguishable from a real header block by this generic, format-
# agnostic heuristic; carrying them forward costs a modest, now-BOUNDED
# increase in chunk count (see the overlap clamp below -- it caps how much
# worse this can get, it doesn't try to detect intent). No content is ever
# corrupted by a false positive, only chunk count/cost shifts modestly.
_MIN_HEADER_BLOCK_LINES = 2


def _detect_header_block(text: str) -> tuple[str, int]:
    """Best-effort detection of a leading "Key: value" header block (#129 --
    the EML acceptance criterion: From/To/Subject/Date appear exactly once,
    at the top, so a mid-body chunk of a longer email carries none of them).

    Returns ``(header_block_text, offset_immediately_after_it)``. Returns
    ``("", 0)`` when fewer than `_MIN_HEADER_BLOCK_LINES` consecutive lines
    match the "Key: value" shape from the very start of the text -- the
    overwhelming majority of prose documents (a plain memo opening with an
    ordinary sentence, or a single incidental "Note: ..." line) -- so
    `_chunk_prose` below is a complete no-op for them, byte-for-byte
    identical to the pre-#129 sentence chunker.
    """
    window = text[:_MAX_HEADER_BLOCK_SCAN_CHARS]
    matched: list[str] = []
    cursor = 0
    for raw in window.splitlines(keepends=True):
        stripped = raw.rstrip("\r\n")
        if not stripped:
            break  # blank line ends the header block
        if not _HEADER_LINE_RE.match(stripped):
            break
        matched.append(stripped)
        cursor += len(raw)
        if len(matched) >= _MAX_HEADER_BLOCK_LINES:
            break
    if len(matched) < _MIN_HEADER_BLOCK_LINES:
        return "", 0
    return "\n".join(matched), cursor


# Per-line (not whole-block) truncation cap for a detected header block
# (#129 follow-up blocker 1). `_extract_eml_text` emits From/To/Cc/Date/
# Subject IN THAT ORDER -- truncating the whole joined block from the tail
# (the old behavior, via the generic `_bounded_context`) drops whichever
# field sorts LAST first, which is Subject: the field with the most
# retrieval value. What actually blows up a real header's size is a long
# recipient list (a big To:/Cc:); capping each line independently keeps
# every field/key present and only trims the value that's actually
# oversized.
_MAX_HEADER_LINE_CHARS = 200


def _bound_header_block(header_block: str) -> str:
    """Cap each line of `header_block` independently -- see the
    `_MAX_HEADER_LINE_CHARS` comment above for why this replaces a single
    whole-block truncation."""
    bounded = []
    for line in header_block.split("\n"):
        if len(line) > _MAX_HEADER_LINE_CHARS:
            dropped = len(line) - _MAX_HEADER_LINE_CHARS
            bounded.append(line[:_MAX_HEADER_LINE_CHARS] + f"...[+{dropped} chars]")
        else:
            bounded.append(line)
    return "\n".join(bounded)


def _chunk_prose(
    text: str,
    document_id: str,
    max_size: int,
    overlap: int,
    _header: tuple[str, int] | None = None,
) -> list[ChunkData]:
    """Sentence chunking for the ``prose`` hint, with header-block
    carry-forward (#129).

    When `_detect_header_block` finds nothing (the common case), this is
    IDENTICAL to `_chunk_by_sentences` -- no behavior change for txt,
    markdown, docx, epub, rtf, odt, pdf, html, or a plain-body .eml.

    When it finds a header block (an .eml's From/To/Cc/Date/Subject, or any
    other prose document shaped that way), the block is reserved OUT of the
    sentence chunker's own budget up front (`effective_max_size` below) and
    then re-injected into every chunk that doesn't already contain it
    naturally -- so the header stays within the overall max_chunk_size
    budget instead of pushing a full chunk over it.

    `overlap` is CLAMPED to at most half of `effective_max_size` (#129
    follow-up blocker 1): the caller's `overlap` is calibrated against
    `max_size`, not against the smaller `effective_max_size` left after
    reserving room for the header. Left unclamped, the sentence chunker's
    stride (`effective_max_size - overlap`) can collapse toward zero as the
    header grows -- measured on a 19KB email with a 400-sentence body at the
    SHIPPED defaults (max_size 787 / overlap 200): a 20-recipient To: list
    shrank `effective_max_size` to 268, leaving a stride of 68 (9% of the
    original 787 budget) and multiplying 32 chunks into 348. Bounding
    `overlap` to at most half of `effective_max_size` keeps stride at >= 50%
    of it regardless of how large the header is.

    `_header`: precomputed `_detect_header_block(text)` result, so the
    dispatcher in `_chunk_text_inner` (which needs the same detection to
    label `chunking_strategy`) and this function don't each independently
    scan the text (#129 follow-up item 12). Recomputed internally when
    omitted (`None`, the default), so this function stays independently
    callable/testable.
    """
    header_block, header_end = _detect_header_block(text) if _header is None else _header
    if not header_block:
        return _chunk_by_sentences(text, document_id, max_size, overlap)

    header_block = _bound_header_block(header_block)
    reserved = len(header_block) + 2  # +2 for the "\n\n" separator
    # If the header alone would consume the whole budget (a pathological
    # tiny max_size, or a very long multi-line header), fall back to plain
    # sentence chunking rather than starving the body down to near-nothing.
    if reserved >= max_size:
        return _chunk_by_sentences(text, document_id, max_size, overlap)

    effective_max_size = max_size - reserved
    effective_overlap = min(overlap, effective_max_size // 2)
    chunks = _chunk_by_sentences(text, document_id, effective_max_size, effective_overlap)
    for c in chunks:
        if c.start_char >= header_end:
            c.content = f"{header_block}\n\n{c.content}".strip()
    return chunks
