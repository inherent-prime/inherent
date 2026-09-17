"""Unit tests for src/services/conversation_intake.py (#306).

Mirrors test_document_intake.py's shape: pins the service-layer behavior the
REST route (tests/unit/test_conversations_endpoint.py) delegates to, so both
stay in sync the same way document_intake.py's tests do for
POST /v1/documents.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from src.core.exceptions import ServiceUnavailableError
from src.models.conversation import ConversationTurnIn
from src.services import conversation_intake

pytestmark = [pytest.mark.unit]


def _turn(**overrides) -> ConversationTurnIn:
    defaults: dict = {
        "turn_id": "t1",
        "role": "user",
        "text": "hello",
        "ts": "2026-08-31T00:00:00Z",
        "client": "agent-cli",
    }
    defaults.update(overrides)
    return ConversationTurnIn(**defaults)


@pytest.fixture
def mock_mq():
    mq = AsyncMock()
    mq.publish = AsyncMock(return_value="1234567890-0")
    return mq


class TestIntakeTurnsSuccess:
    async def test_publishes_one_message_per_turn(self, mock_mq):
        """Core #306 contract: ONE MQ message per turn, never one per
        batch -- see ConversationTurnMessage's docstring."""
        with patch.object(
            conversation_intake, "get_mq_service", new=AsyncMock(return_value=mock_mq)
        ):
            result = await conversation_intake.intake_turns(
                workspace_id="ws-1",
                user_id="user-1",
                external_id="conv-1",
                turns=[_turn(turn_id="t1"), _turn(turn_id="t2"), _turn(turn_id="t3")],
            )

        assert result.accepted == 3
        assert mock_mq.publish.await_count == 3

    async def test_publishes_to_the_conversation_turn_topic(self, mock_mq):
        with patch.object(
            conversation_intake, "get_mq_service", new=AsyncMock(return_value=mock_mq)
        ):
            await conversation_intake.intake_turns(
                workspace_id="ws-1", user_id="user-1", external_id="conv-1", turns=[_turn()]
            )

        from src.config import settings

        topic, _message = mock_mq.publish.await_args.args
        assert topic == settings.mq_topic_conversation_turn

    async def test_message_shape_matches_conversation_turn_contract(self, mock_mq):
        with patch.object(
            conversation_intake, "get_mq_service", new=AsyncMock(return_value=mock_mq)
        ):
            await conversation_intake.intake_turns(
                workspace_id="ws-1",
                user_id="user-9",
                external_id="conv-abc",
                turns=[_turn(turn_id="turn-42", role="assistant", text="the answer", client="sdk")],
            )

        _topic, message = mock_mq.publish.await_args.args
        assert message["event_type"] == "conversation.turn"
        assert message["workspace_id"] == "ws-1"
        assert message["user_id"] == "user-9"
        assert message["external_id"] == "conv-abc"
        assert message["turn_id"] == "turn-42"
        assert message["role"] == "assistant"
        assert message["text"] == "the answer"
        assert message["client"] == "sdk"
        assert "timestamp" in message
        assert "contract_version" in message

    async def test_response_echoes_workspace_and_external_id(self, mock_mq):
        with patch.object(
            conversation_intake, "get_mq_service", new=AsyncMock(return_value=mock_mq)
        ):
            result = await conversation_intake.intake_turns(
                workspace_id="ws-echo", user_id="user-1", external_id="conv-echo", turns=[_turn()]
            )

        assert result.workspace_id == "ws-echo"
        assert result.external_id == "conv-echo"


class TestIntakeTurnsPublishFailure:
    async def test_publish_failure_raises_service_unavailable(self, mock_mq):
        mock_mq.publish = AsyncMock(side_effect=RuntimeError("redis down"))

        with patch.object(
            conversation_intake, "get_mq_service", new=AsyncMock(return_value=mock_mq)
        ):
            with pytest.raises(ServiceUnavailableError):
                await conversation_intake.intake_turns(
                    workspace_id="ws-1", user_id="user-1", external_id="conv-1", turns=[_turn()]
                )

    async def test_partial_batch_failure_reports_turns_already_enqueued(self, mock_mq):
        """Turn 1 publishes fine, turn 2 fails -- the error must be
        actionable about how much of the batch is already safely enqueued
        (retrying the WHOLE request is safe: turn_id makes each turn
        idempotent on the consumer side)."""
        mock_mq.publish = AsyncMock(side_effect=["ok-1", RuntimeError("redis down")])

        with patch.object(
            conversation_intake, "get_mq_service", new=AsyncMock(return_value=mock_mq)
        ):
            with pytest.raises(ServiceUnavailableError) as exc_info:
                await conversation_intake.intake_turns(
                    workspace_id="ws-1",
                    user_id="user-1",
                    external_id="conv-1",
                    turns=[_turn(turn_id="t1"), _turn(turn_id="t2")],
                )

        assert "1/2" in str(exc_info.value.detail)
        assert mock_mq.publish.await_count == 2
