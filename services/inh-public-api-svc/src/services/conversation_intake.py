"""Conversation-turn intake pipeline (#306).

Extracted out of `POST /v1/conversations/{external_id}/turns`
(`src/api/v1/conversations.py`) the same way `document_intake.py` was
extracted from `POST /v1/documents` (#87) -- a single place both the REST
route and any future second caller (an MCP tool, say) can share so behavior
can never drift between them.

Unlike document intake, there is no S3 upload, no content-hash dedup, and no
durable `pending` row written here: public-api does not talk to Temporal
directly (the boundary the issue explicitly keeps as-is) and a conversation
has no "file" for this service to own the storage lifecycle of. This
function's only two jobs are: validate, and publish ONE MQ message PER TURN
(never one per batch -- see `ConversationTurnMessage`'s docstring,
`inh_contracts.events`) to `core.conversation.turn.v1`. The workflow-side
buffer in `ConversationMemoryWorkflow` (#306, inh-ingestion-svc) is what
implements the debounce; the MQ layer needs none.
"""

from __future__ import annotations

from datetime import datetime, timezone

from inh_contracts.events import ConversationTurnMessage

from src.config import settings
from src.core.exceptions import ServiceUnavailableError
from src.models.conversation import ConversationTurnBatchResponse, ConversationTurnIn
from src.services.mq import get_mq_service
from src.utils import get_logger

logger = get_logger(__name__)


async def intake_turns(
    *,
    workspace_id: str,
    user_id: str,
    external_id: str,
    turns: list[ConversationTurnIn],
) -> ConversationTurnBatchResponse:
    """Publish each turn in `turns` to the conversation-turn MQ topic.

    Turns are published in order, ONE message per turn. If publishing a
    turn fails partway through the batch, every turn published so far has
    already been durably enqueued (MQ XADD) -- retrying the WHOLE batch is
    safe because `turn_id` makes every turn idempotent
    (`ConversationMemoryWorkflow.add_turn`'s dedup), so an already-published
    turn redelivered by a client retry is simply a no-op on the consumer
    side rather than a duplicate.

    Raises:
        ServiceUnavailableError: the MQ publish failed for some turn in the
            batch (transient -- safe to retry the whole request).
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    mq = await get_mq_service()

    published = 0
    for turn in turns:
        message = ConversationTurnMessage(
            event_type="conversation.turn",
            workspace_id=workspace_id,
            user_id=user_id,
            external_id=external_id,
            turn_id=turn.turn_id,
            role=turn.role,
            text=turn.text,
            ts=turn.ts,
            client=turn.client,
            timestamp=now_iso,
        )
        try:
            await mq.publish(settings.mq_topic_conversation_turn, message.model_dump())
        except Exception as exc:
            logger.error(
                "MQ publish failed for a conversation turn — batch partially enqueued",
                error=str(exc),
                workspace_id=workspace_id,
                external_id=external_id,
                turn_id=turn.turn_id,
                published_so_far=published,
            )
            raise ServiceUnavailableError(
                service_name="mq",
                detail=(
                    f"Failed to queue turn '{turn.turn_id}' for processing "
                    f"({published}/{len(turns)} turns already enqueued). "
                    "Retrying the whole request is safe — turn_id makes each "
                    "turn idempotent."
                ),
            ) from exc
        published += 1

    logger.info(
        "Conversation turns accepted",
        workspace_id=workspace_id,
        external_id=external_id,
        accepted=published,
    )

    return ConversationTurnBatchResponse(
        external_id=external_id,
        workspace_id=workspace_id,
        accepted=published,
    )
