"""Shared identity derivation for conversation ingestion (#306).

Both `conversation_trigger.py` (which starts/signals the Temporal workflow)
and `ConversationMemoryWorkflow` itself (which persists a `processed_documents`
row) need the SAME deterministic identifiers for a given
`(workspace_id, external_id)` pair. Deriving them in one place means the two
can never drift apart -- a drift here would mean the workflow that receives a
signal isn't the workflow whose document row the trigger expects it to write.
"""

from __future__ import annotations


def conversation_workflow_id(workspace_id: str, external_id: str) -> str:
    """The Temporal workflow id for a conversation (#306 issue's own scheme).

    Idempotent by construction: `signal_with_start` against this id is what
    makes the first turn start the workflow and every later turn a signal to
    the SAME run (mod `continue_as_new`, which preserves the workflow id).
    """
    return f"conv-{workspace_id}-{external_id}"


def conversation_document_id(workspace_id: str, external_id: str) -> str:
    """The `processed_documents.document_id` for a conversation (#306).

    Deliberately the SAME string as `conversation_workflow_id` -- there is no
    reason to mint a second identifier when this one is already deterministic
    and unique per `(workspace_id, external_id)` (migration 020's
    `document_id` column is a plain global-uniqueness UNIQUE, not scoped to
    workspace, so baking `workspace_id` into the string itself -- not just
    `external_id` alone -- is what keeps two different workspaces' identically
    named conversations from colliding).
    """
    return conversation_workflow_id(workspace_id, external_id)
