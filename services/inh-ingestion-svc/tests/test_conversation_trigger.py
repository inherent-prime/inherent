"""Unit tests for ConversationTurnTrigger (#306).

Mirrors test_temporal_trigger.py's mocked-client pattern (no real Temporal
server needed): a mocked `Client.start_workflow` proves the CALL SHAPE --
workflow id, id_conflict_policy, start_signal/start_signal_args -- without
needing a live Temporal server.

Overrides the package-level DB-dependent `cleanup_test_data` autouse fixture
with a no-op (same as test_temporal_trigger.py) -- these tests are pure/
mocked, no PostgreSQL interaction.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from temporalio.common import WorkflowIDConflictPolicy

from src.temporal.conversation_trigger import ConversationTurnTrigger, get_conversation_trigger
from src.temporal.models import ConversationMemoryInput, ConversationTurnSignal


@pytest.fixture(autouse=True)
def cleanup_test_data():
    """No-op override so this module's tests run without a live database."""
    yield


def _make_settings():
    settings = MagicMock()
    settings.temporal_host = "localhost:7233"
    settings.temporal_namespace = "default"
    settings.temporal_task_queue = "ingestion"
    return settings


def _turn_message(**overrides) -> dict:
    base = {
        "event_type": "conversation.turn",
        "workspace_id": "ws-1",
        "user_id": "user-1",
        "external_id": "conv-abc",
        "turn_id": "turn-1",
        "role": "user",
        "text": "hello",
        "ts": "2026-08-31T10:00:00Z",
        "client": "agent-cli",
        "timestamp": "2026-08-31T10:00:01Z",
    }
    base.update(overrides)
    return base


def _ready_trigger() -> ConversationTurnTrigger:
    trigger = ConversationTurnTrigger(_make_settings())
    trigger._initialized = True
    trigger._client = MagicMock()
    trigger._client.start_workflow = AsyncMock(return_value=MagicMock())
    return trigger


class TestInitialState:
    def test_client_is_none_initially(self):
        trigger = ConversationTurnTrigger(_make_settings())
        assert trigger._client is None

    def test_initialized_is_false_initially(self):
        trigger = ConversationTurnTrigger(_make_settings())
        assert trigger._initialized is False


class TestTriggerTurnAsync:
    """The core #306 contract: signal_with_start with USE_EXISTING, never
    TERMINATE_EXISTING -- a later turn must never kill a running
    conversation (see conversation_trigger.py's module docstring)."""

    @pytest.mark.asyncio
    async def test_uses_signal_with_start_and_use_existing_conflict_policy(self):
        trigger = _ready_trigger()

        await trigger.trigger_turn_async(_turn_message())

        _, kwargs = trigger._client.start_workflow.call_args
        assert kwargs["id_conflict_policy"] == WorkflowIDConflictPolicy.USE_EXISTING
        assert kwargs["start_signal"] == "add_turn"
        assert len(kwargs["start_signal_args"]) == 1
        assert isinstance(kwargs["start_signal_args"][0], ConversationTurnSignal)

    @pytest.mark.asyncio
    async def test_workflow_id_is_deterministic_from_workspace_and_external_id(self):
        trigger = _ready_trigger()

        await trigger.trigger_turn_async(_turn_message(workspace_id="ws-x", external_id="conv-y"))

        _, kwargs = trigger._client.start_workflow.call_args
        assert kwargs["id"] == "conv-ws-x-conv-y"

    @pytest.mark.asyncio
    async def test_two_turns_for_same_conversation_use_the_same_workflow_id(self):
        """The property that makes USE_EXISTING correct: turn 2 must target
        the SAME workflow id as turn 1."""
        trigger = _ready_trigger()

        await trigger.trigger_turn_async(_turn_message(turn_id="t1"))
        await trigger.trigger_turn_async(_turn_message(turn_id="t2"))

        first_id = trigger._client.start_workflow.call_args_list[0].kwargs["id"]
        second_id = trigger._client.start_workflow.call_args_list[1].kwargs["id"]
        assert first_id == second_id

    @pytest.mark.asyncio
    async def test_run_args_carry_conversation_identity(self):
        trigger = _ready_trigger()

        await trigger.trigger_turn_async(
            _turn_message(workspace_id="ws-1", external_id="conv-abc", user_id="user-9")
        )

        args, _ = trigger._client.start_workflow.call_args
        run_input = args[1]
        assert isinstance(run_input, ConversationMemoryInput)
        assert run_input.workspace_id == "ws-1"
        assert run_input.external_id == "conv-abc"
        assert run_input.user_id == "user-9"

    @pytest.mark.asyncio
    async def test_signal_args_carry_turn_fields(self):
        trigger = _ready_trigger()

        await trigger.trigger_turn_async(
            _turn_message(
                turn_id="turn-42", role="assistant", text="the answer is 42", client="sdk-py"
            )
        )

        _, kwargs = trigger._client.start_workflow.call_args
        signal = kwargs["start_signal_args"][0]
        assert signal.turn_id == "turn-42"
        assert signal.role == "assistant"
        assert signal.text == "the answer is 42"
        assert signal.client == "sdk-py"

    @pytest.mark.asyncio
    async def test_poison_message_is_dropped_not_raised(self):
        trigger = _ready_trigger()

        result = await trigger.trigger_turn_async({"not": "a valid conversation turn message"})

        assert result == ""
        trigger._client.start_workflow.assert_not_called()

    @pytest.mark.asyncio
    async def test_transient_temporal_failure_propagates(self):
        """A malformed message is dropped (poison); a genuinely transient
        Temporal-unavailable failure must propagate so the MQ consumer
        leaves the message pending for redelivery."""
        trigger = _ready_trigger()
        trigger._client.start_workflow = AsyncMock(side_effect=RuntimeError("temporal down"))

        with pytest.raises(RuntimeError, match="temporal down"):
            await trigger.trigger_turn_async(_turn_message())


class TestGetConversationTrigger:
    def test_returns_same_instance_on_repeated_calls(self):
        import src.temporal.conversation_trigger as mod

        mod._conversation_trigger = None
        settings = _make_settings()
        t1 = get_conversation_trigger(settings)
        t2 = get_conversation_trigger(settings)
        assert t1 is t2
        mod._conversation_trigger = None

    def test_backfills_db_service_without_downgrading_to_none(self):
        import src.temporal.conversation_trigger as mod

        mod._conversation_trigger = None
        settings = _make_settings()
        db = MagicMock()
        t1 = get_conversation_trigger(settings)
        assert t1._db_service is None
        t2 = get_conversation_trigger(settings, db_service=db)
        assert t1 is t2
        assert t2._db_service is db
        mod._conversation_trigger = None
