"""Bridges the `core.conversation.turn.v1` MQ topic to
`ConversationMemoryWorkflow` (#306).

Mirrors `trigger.py` (`TemporalWorkflowTrigger`), the document-upload MQ ->
Temporal bridge, but with a DELIBERATELY different conflict policy: the
first turn of a conversation must START the workflow, and every later turn
must SIGNAL the SAME running workflow -- never terminate and restart it.

USE_EXISTING, not TERMINATE_EXISTING
-----------------------------------------
`trigger.py` uses `WorkflowIDConflictPolicy.TERMINATE_EXISTING` because a
document re-upload really does want to supersede a still-running prior
ingestion of the SAME document_id (#110) -- the new content should win.

A conversation turn is the opposite: turn N+1 arriving while turn N's
workflow run is still open is the NORMAL case (that running workflow IS the
conversation's buffer), not a collision to resolve by killing it.
`TERMINATE_EXISTING` here would discard every turn buffered since the last
flush and restart the debounce window on every single turn -- silently
losing conversation history and defeating the size-or-idle debounce
entirely. `WorkflowIDConflictPolicy.USE_EXISTING` is what makes
`signal_with_start` (`start_signal=`/`start_signal_args=` below) add this
turn to the existing run when one is already open, and start a fresh run
only when none is.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog
from inh_contracts.events import ConversationTurnMessage
from pydantic import ValidationError as PydanticValidationError
from temporalio.client import Client
from temporalio.common import WorkflowIDConflictPolicy

from src.config.settings import Settings
from src.temporal.conversation_identity import conversation_workflow_id
from src.temporal.models import ConversationMemoryInput, ConversationTurnSignal
from src.temporal.workflows import ConversationMemoryWorkflow

if TYPE_CHECKING:
    from src.services.database import DatabaseService

logger = structlog.get_logger(__name__)


class ConversationTurnTrigger:
    """Delivers one MQ `ConversationTurnMessage` to `ConversationMemoryWorkflow`
    via `signal_with_start` (#306)."""

    def __init__(self, settings: Settings, db_service: DatabaseService | None = None):
        self.settings = settings
        # Held for parity with TemporalWorkflowTrigger and for a future
        # dead-letter path on a poison conversation-turn message -- not used
        # by the happy path below.
        self._db_service = db_service
        self._client: Client | None = None
        self._initialized = False

    async def initialize(self) -> None:
        if self._initialized:
            return
        logger.info(
            "Connecting to Temporal server for conversation-turn triggering",
            host=self.settings.temporal_host,
            namespace=self.settings.temporal_namespace,
        )
        self._client = await Client.connect(
            self.settings.temporal_host,
            namespace=self.settings.temporal_namespace,
        )
        self._initialized = True
        logger.info("Temporal client connected for conversation-turn triggering")

    async def trigger_turn_async(self, message: dict) -> str:
        """Validate `message` as a `ConversationTurnMessage` and deliver it
        to `ConversationMemoryWorkflow` via `signal_with_start`.

        A malformed (poison) message is logged and dropped (never raised) so
        the MQ consumer ACKs it and stops redelivering -- same "poison
        message never blocks the stream" contract as
        `TemporalWorkflowTrigger.trigger_workflow_async`. A transient
        failure (Temporal unavailable) DOES raise, so the message stays
        pending for redelivery.

        Returns:
            The conversation's workflow_id, or "" if the message was poison
            and dropped.
        """
        if not self._initialized:
            await self.initialize()

        try:
            turn_message = ConversationTurnMessage(**message)
        except PydanticValidationError as e:
            logger.error(
                "Poison conversation-turn message; dropping instead of redelivering",
                error=str(e),
                message=message,
                validation_errors=e.errors(),
            )
            return ""

        workflow_id = conversation_workflow_id(turn_message.workspace_id, turn_message.external_id)

        if self._client is None:
            raise RuntimeError("Temporal client not initialized")

        # signal_with_start: the FIRST turn starts the workflow (run() gets
        # ConversationMemoryInput); every later turn signals the SAME
        # running workflow (add_turn() gets this ConversationTurnSignal) --
        # USE_EXISTING is what makes that "add to the existing run, don't
        # collide" behavior happen. See module docstring.
        # Resolved HERE -- a normal Python call site, outside the workflow
        # sandbox -- and passed as plain data on ConversationMemoryInput.
        # These start-args are used only if this call actually STARTS a new
        # execution; Temporal ignores them for a signal to an already-
        # running workflow, so re-resolving on every turn is harmless and
        # keeps a fresh conversation's very first run current with whatever
        # the settings say right now. See ConversationMemoryInput's
        # docstring for why the WORKFLOW itself must never do this.
        await self._client.start_workflow(
            ConversationMemoryWorkflow.run,
            ConversationMemoryInput(
                workspace_id=turn_message.workspace_id,
                external_id=turn_message.external_id,
                user_id=turn_message.user_id,
                flush_char_threshold=self.settings.conversation_flush_char_threshold,
                flush_idle_seconds=self.settings.conversation_flush_idle_seconds,
                continue_as_new_turns=self.settings.conversation_continue_as_new_turns,
                idle_finalize_seconds=self.settings.conversation_idle_finalize_hours * 3600,
            ),
            id=workflow_id,
            task_queue=self.settings.temporal_task_queue,
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
            start_signal="add_turn",
            start_signal_args=[
                ConversationTurnSignal(
                    turn_id=turn_message.turn_id,
                    role=turn_message.role,
                    text=turn_message.text,
                    ts=turn_message.ts,
                    user_id=turn_message.user_id,
                    client=turn_message.client,
                )
            ],
        )

        logger.info(
            "Conversation turn delivered (signal_with_start)",
            workflow_id=workflow_id,
            workspace_id=turn_message.workspace_id,
            external_id=turn_message.external_id,
            turn_id=turn_message.turn_id,
        )

        return workflow_id

    def shutdown(self) -> None:
        self._client = None
        self._initialized = False
        logger.info("Conversation-turn trigger shut down")


_conversation_trigger: ConversationTurnTrigger | None = None


def get_conversation_trigger(
    settings: Settings, db_service: DatabaseService | None = None
) -> ConversationTurnTrigger:
    """Get or create the global conversation-turn trigger (mirrors
    `trigger.get_workflow_trigger`)."""
    global _conversation_trigger
    if _conversation_trigger is None:
        _conversation_trigger = ConversationTurnTrigger(settings, db_service=db_service)
    elif db_service is not None and _conversation_trigger._db_service is None:
        _conversation_trigger._db_service = db_service
    return _conversation_trigger
