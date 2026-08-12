"""Document-related models."""

from dataclasses import dataclass
from datetime import datetime
from typing import Final

from pydantic import BaseModel

# --- Document-context pagination bounds (#219) ------------------------------
# A 169-chunk PDF's combined text measured 117,086 chars (~29,300 tokens) --
# a sizeable fraction of an LLM context window in a single call. These bounds
# are shared by BOTH GET /v1/chunks/{document_id}/context (src/api/v1/chunks.py)
# and the MCP get_document_context tool (src/mcp_server/server.py) so neither
# surface can silently disagree about how much text one call returns.
DEFAULT_MAX_CHARS: Final[int] = 20_000
MIN_MAX_CHARS: Final[int] = 1
MAX_MAX_CHARS: Final[int] = 100_000


class Document(BaseModel):
    """Document metadata."""

    id: str
    name: str
    workspace_id: str
    source_type: str
    mime_type: str | None = None
    size_bytes: int = 0
    chunk_count: int = 0
    status: str = "processed"
    created_at: datetime
    updated_at: datetime
    metadata: dict | None = None


class DocumentChunk(BaseModel):
    """A chunk from a document."""

    id: str
    document_id: str
    content: str
    chunk_index: int
    token_count: int = 0
    metadata: dict | None = None


class ChunkContentRequest(BaseModel):
    """Body for Create / Update chunk (#133)."""

    content: str


class DocumentListResponse(BaseModel):
    """Response for listing documents."""

    documents: list[Document]
    total: int
    page: int
    page_size: int


class DocumentUploadResponse(BaseModel):
    """Response returned after a document is accepted for ingestion."""

    document_id: str
    name: str
    workspace_id: str
    storage_url: str
    mime_type: str
    size_bytes: int
    status: str = "pending"
    message: str = "Document uploaded successfully. Processing will begin shortly."


@dataclass(frozen=True)
class ContextWindow:
    """One bounded slice of a document's combined chunk text (#219).

    Returned by ``windowed_document_context`` below, which is the single
    shared computation both REST (``api/v1/chunks.py``) and MCP
    (``mcp_server.server._handle_get_context``) call — so the two surfaces
    can never disagree about the default bound or the slicing rule.
    """

    full_text: str
    chunks: list["DocumentChunk"]
    total_chars: int
    truncated: bool
    offset: int
    next_offset: int | None


def windowed_document_context(
    chunks: list["DocumentChunk"], *, offset: int, max_chars: int
) -> ContextWindow:
    """Slice a document's combined chunk text to a bounded [offset, offset+max_chars) window.

    Bounding rule (#219): the unbounded version of this endpoint joined every
    chunk's ``content`` with no limit -- a 169-chunk PDF returned 298 KB /
    117,086 chars in one response. Truncating only ``full_text`` while still
    returning every ``DocumentChunk`` object would NOT bound the payload (the
    chunk objects alone carried most of that 298 KB), so ``chunks`` here is
    windowed the SAME way: a chunk is included only if some part of its
    ``content`` falls inside the returned character range. A chunk straddling
    a page boundary is therefore returned whole on both the page that starts
    it and the page that finishes it -- deliberate, since chunk content is
    never split mid-chunk.

    An ``offset`` at or past the end of the text returns an empty,
    non-truncated slice (``next_offset=None``) -- the same "ran off the end"
    semantics as any offset-based pager, not an error. The returned
    ``ContextWindow.offset`` is the CLAMPED value actually used (into
    ``[0, total_chars]``), not the raw input, so a caller that requests an
    offset past the end sees what was really applied.

    A truncated slice gets a short human-readable marker appended to
    ``full_text`` (after the max_chars cut, so it adds a small constant
    overhead rather than counting against the budget) so a caller reading
    ``full_text`` alone -- without checking ``truncated`` -- still notices
    the document was cut off.
    """
    # Same join used by the original unbounded implementation, so
    # total_chars / offsets stay meaningful across calls for the same document.
    joined = "\n\n".join(chunk.content for chunk in chunks)
    total_chars = len(joined)

    offset = max(0, min(offset, total_chars))
    window_end = min(offset + max_chars, total_chars)
    truncated = window_end < total_chars
    next_offset = window_end if truncated else None

    # Select chunks overlapping [offset, window_end) by walking each chunk's
    # own [start, end) span in `joined` (mirrors how `joined` was built).
    windowed_chunks: list[DocumentChunk] = []
    pos = 0
    for chunk in chunks:
        start = pos
        end = start + len(chunk.content)
        if end > offset and start < window_end:
            windowed_chunks.append(chunk)
        pos = end + 2  # len("\n\n") separator between chunks

    sliced_text = joined[offset:window_end]
    if truncated:
        sliced_text += (
            f"\n\n[...truncated: showing chars {offset}-{window_end} of "
            f"{total_chars}; request offset={next_offset} for more...]"
        )

    return ContextWindow(
        full_text=sliced_text,
        chunks=windowed_chunks,
        total_chars=total_chars,
        truncated=truncated,
        offset=offset,
        next_offset=next_offset,
    )


class DocumentContextResponse(BaseModel):
    """Response for getting full document context.

    ``chunks`` and ``full_text`` are BOTH bounded to the same
    ``[offset, offset + max_chars)`` window over the document's combined
    text (#219) -- see ``windowed_document_context`` above for the exact
    slicing rule. ``chunks`` is therefore "the chunks contributing to this
    page", not "every chunk in the document"; use ``total_chars`` /
    ``truncated`` / ``next_offset`` to page through the rest.

    New fields (``truncated``, ``total_chars``, ``offset``, ``next_offset``)
    default to the "whole short document, nothing to page" values so any
    other caller constructing this model without them keeps working.
    """

    document: Document
    chunks: list[DocumentChunk]
    full_text: str
    truncated: bool = False
    total_chars: int = 0
    offset: int = 0
    next_offset: int | None = None
