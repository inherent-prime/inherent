"""Conversation-document contract: the synthetic identifiers a conversation's
``processed_documents`` row and Weaviate chunks carry (#306).

A conversation is not a file. It has no uploaded bytes, so
``ConversationMemoryWorkflow`` stamps synthetic-but-real values that satisfy
``processed_documents``' file-shaped NOT NULL/CHECK constraints (migration
001) while still being unambiguously identifiable as "not a file":

- ``content_type = CONVERSATION_CONTENT_TYPE`` -- written to
  ``processed_documents.content_type`` AND to every Weaviate chunk object's
  ``content_type`` property, so BOTH read paths (Postgres document row,
  Weaviate search result) can tell a conversation chunk from a file chunk
  without a second lookup.
- ``document_type = CONVERSATION_DOCUMENT_TYPE`` -- the Postgres-only column
  migration 020 adds, used for row-scoped queries (external-id lookup,
  conversation stats).

These live here rather than in either service because both sides need them:
``inh-ingestion-svc`` WRITES them (``conversation_memory.py``) and
``inh-public-api-svc`` READS them (freshness/staleness, conversation
queries). Same drift lesson as ``file_types.py`` -- a string literal
duplicated across two services is a bug waiting for one of them to be
edited alone.
"""

from typing import Final

# The synthetic MIME type a conversation document/chunk carries. Deliberately
# in the `application/x-` vendor-extension space: it is NOT a real file format
# and must never match a FILE_TYPE_REGISTRY entry (see file_types.py).
CONVERSATION_CONTENT_TYPE: Final[str] = "application/x-inherent-conversation"

# `processed_documents.document_type` values (migration 020's CHECK
# constraint allows exactly these two). 'file' is the default every
# pre-existing row backfilled to.
CONVERSATION_DOCUMENT_TYPE: Final[str] = "conversation"
FILE_DOCUMENT_TYPE: Final[str] = "file"


def is_conversation(
    *,
    content_type: str | None = None,
    document_type: str | None = None,
) -> bool:
    """Return True when either signal identifies a conversation document.

    Both arguments are optional and independently sufficient, because the two
    read paths carry different ones: a Weaviate search result has the chunk's
    ``content_type`` but no ``document_type`` (that column is Postgres-only),
    while a ``processed_documents`` row has both. Unknown/absent values
    (``None``) simply do not match -- this never guesses.
    """
    return content_type == CONVERSATION_CONTENT_TYPE or document_type == CONVERSATION_DOCUMENT_TYPE
