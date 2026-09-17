"""Document/chunk lineage (provenance + freshness) helper (#40).

``explain_lineage`` answers: *where did this document/chunk come from and is it
still fresh?* It is a thin read-only projection over already-ingested data — the
document row and its chunks — with no new business logic:

- provenance: ``source_uri`` and ``content_hash`` are read from the chunk's
  metadata (falling back to the document metadata, then to the document's stored
  ``storage_url`` for source_uri).
- freshness: ``ingested_at`` is parsed and ``is_stale`` is computed with the
  SAME logic the search path uses (:meth:`SearchService._compute_is_stale`), so
  lineage and search always agree on staleness — including the conversation
  exemption (a conversation grows by appending chunks, so an old
  ``ingested_at`` on its earlier chunks is expected, not stale).

The same builder backs BOTH the REST ``GET /v1/documents/{id}/lineage`` endpoint
and the MCP ``explain_lineage`` tool, so the two surfaces never drift.
"""

from __future__ import annotations

from pydantic import BaseModel

from src.models.document import Document, DocumentChunk
from src.services.search import SearchService


class LineageResponse(BaseModel):
    """Provenance + freshness for a document (optionally a specific chunk)."""

    document_id: str
    document_name: str
    workspace_id: str
    chunk_id: str | None = None
    source_uri: str | None = None
    content_hash: str | None = None
    ingested_at: str | None = None
    is_stale: bool = False
    status: str = "processed"


def build_lineage(
    document: Document,
    chunks: list[DocumentChunk],
    *,
    chunk_id: str | None = None,
) -> LineageResponse:
    """Project provenance + freshness for ``document`` (or a specific chunk).

    Raises ``KeyError`` when ``chunk_id`` is given but not found in ``chunks`` so
    callers can surface a clear "chunk not found" error.
    """
    selected: DocumentChunk | None = None
    if chunk_id:
        selected = next((c for c in chunks if c.id == chunk_id), None)
        if selected is None:
            raise KeyError(chunk_id)
    elif chunks:
        selected = chunks[0]

    def _meta_get(key: str) -> object:
        chunk_meta = (selected.metadata if selected else None) or {}
        if key in chunk_meta and chunk_meta[key] is not None:
            return chunk_meta[key]
        doc_meta = document.metadata or {}
        return doc_meta.get(key)

    ingested_at = SearchService._parse_ingested_at(_meta_get("ingested_at"))
    # The chunk's own content_type when it carries one, else the document
    # row's (`processed_documents.content_type`, surfaced as `mime_type`).
    # Only used to spot a conversation, which is exempt from the age-based
    # staleness rule (#306 follow-up) — see _compute_is_stale's docstring.
    chunk_content_type = _meta_get("content_type")
    is_stale = SearchService._compute_is_stale(
        ingested_at,
        content_type=(
            chunk_content_type if isinstance(chunk_content_type, str) else document.mime_type
        ),
    )
    source_uri = _meta_get("source_uri") or (document.metadata or {}).get("storage_url")
    content_hash = _meta_get("content_hash")

    return LineageResponse(
        document_id=document.id,
        document_name=document.name,
        workspace_id=document.workspace_id,
        chunk_id=selected.id if selected else None,
        source_uri=str(source_uri) if source_uri is not None else None,
        content_hash=str(content_hash) if content_hash is not None else None,
        ingested_at=ingested_at.isoformat() if ingested_at else None,
        is_stale=is_stale,
        status=document.status,
    )
