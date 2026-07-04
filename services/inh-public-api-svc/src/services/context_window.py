"""Context window builder (PM-S019)."""

from __future__ import annotations

from typing import Protocol

from src.models.document import DocumentChunk
from src.models.search import ContextChunk, SearchResult
from src.utils import get_logger

logger = get_logger(__name__)


class _ContextCapableDatabase(Protocol):
    async def get_context_chunks(
        self, workspace_id: str, user_id: str, ranges: list[tuple[str, int, int]]
    ) -> list[DocumentChunk]: ...


def _match_chunk_index(match: SearchResult) -> int | None:
    """Pull chunk_index out of metadata (where SearchService currently puts it)."""
    if match.metadata and "chunk_index" in match.metadata:
        try:
            return int(match.metadata["chunk_index"])
        except (TypeError, ValueError):
            return None
    return None


def _compute_ranges(matches: list[SearchResult], k: int) -> list[tuple[str, int, int]]:
    """Compute one (lo, hi) fetch range per match, merging only overlapping or
    adjacent ranges within a document.

    Emitting a per-match ``[idx-k, idx+k]`` window (rather than one
    ``[min-k, max+k]`` span per document) means two far-apart matches fetch just
    their own neighbourhoods instead of the entire document between them (#21).
    Lo is clamped to 0; hi is uncapped because the DB returns only real rows.
    """
    if k <= 0:
        return []
    per_doc: dict[str, list[tuple[int, int]]] = {}
    for m in matches:
        idx = _match_chunk_index(m)
        if idx is None:
            continue
        per_doc.setdefault(m.document_id, []).append((max(0, idx - k), idx + k))

    ranges: list[tuple[str, int, int]] = []
    for doc_id, spans in per_doc.items():
        spans.sort()
        cur_lo, cur_hi = spans[0]
        for lo, hi in spans[1:]:
            if lo <= cur_hi + 1:  # overlapping or adjacent -> merge
                cur_hi = max(cur_hi, hi)
            else:
                ranges.append((doc_id, cur_lo, cur_hi))
                cur_lo, cur_hi = lo, hi
        ranges.append((doc_id, cur_lo, cur_hi))
    return ranges


def _assign_windows(
    matches: list[SearchResult],
    rows: list[DocumentChunk],
    k: int,
) -> None:
    """Populate context_before / context_after on each match using fetched rows."""
    by_doc: dict[str, list[DocumentChunk]] = {}
    for row in rows:
        by_doc.setdefault(row.document_id, []).append(row)
    for doc_rows in by_doc.values():
        doc_rows.sort(key=lambda r: r.chunk_index)

    for m in matches:
        idx = _match_chunk_index(m)
        if idx is None:
            continue
        doc_rows = by_doc.get(m.document_id, [])
        before = [r for r in doc_rows if idx - k <= r.chunk_index < idx]
        after = [r for r in doc_rows if idx < r.chunk_index <= idx + k]
        m.context_before = [
            ContextChunk(
                chunk_id=r.id,
                chunk_index=r.chunk_index,
                content=r.content,
                token_count=r.token_count or 0,
            )
            for r in before
        ]
        m.context_after = [
            ContextChunk(
                chunk_id=r.id,
                chunk_index=r.chunk_index,
                content=r.content,
                token_count=r.token_count or 0,
            )
            for r in after
        ]


class ContextWindowBuilder:
    """Expands search results with neighbouring chunks via one batched PG fetch."""

    def __init__(self, database: _ContextCapableDatabase) -> None:
        self._db = database

    async def expand(
        self,
        matches: list[SearchResult],
        workspace_id: str,
        user_id: str,
        k: int,
    ) -> None:
        """Expand each match with neighbouring chunks.

        Cross-tenant safety (#41): ``user_id`` is threaded through to
        ``get_context_chunks`` so neighbour chunks are scoped to the requesting
        user, not just the (possibly multi-user) workspace.
        """
        if not matches:
            return
        if k == 0:
            for m in matches:
                m.context_before = []
                m.context_after = []
            return
        ranges = _compute_ranges(matches, k)
        if not ranges:
            return
        try:
            rows = await self._db.get_context_chunks(workspace_id, user_id, ranges)
        except Exception as exc:
            logger.error(
                "context_window_fetch_failed",
                workspace_id=workspace_id,
                user_id=user_id,
                error=str(exc),
            )
            return
        _assign_windows(matches, rows, k)
