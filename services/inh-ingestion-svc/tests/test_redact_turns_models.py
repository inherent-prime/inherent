"""Unit tests for the #307 redaction dataclasses in src/temporal/models.py:
RedactTurnInput, RedactedTurn, RedactTurnsInput, RedactTurnsOutput.

Pure dataclass construction/defaults -- no PostgreSQL involved.
`cleanup_test_data` is overridden below with a no-op (same pattern as
tests/test_migrations.py) so the package-wide autouse DB fixture doesn't
silently skip these tests.
"""

from __future__ import annotations

import dataclasses

import pytest

from src.temporal.models import (
    RedactedTurn,
    RedactTurnInput,
    RedactTurnsInput,
    RedactTurnsOutput,
)


@pytest.fixture(autouse=True)
def cleanup_test_data():
    """No-op override of the package-level DB-dependent autouse fixture."""
    yield


class TestRedactTurnInput:
    def test_is_dataclass(self):
        assert dataclasses.is_dataclass(RedactTurnInput)

    def test_required_fields(self):
        obj = RedactTurnInput(turn_id="t1", text="hello world")
        assert obj.turn_id == "t1"
        assert obj.text == "hello world"

    def test_role_defaults_to_none(self):
        obj = RedactTurnInput(turn_id="t1", text="hello")
        assert obj.role is None

    def test_role_can_be_set(self):
        obj = RedactTurnInput(turn_id="t1", text="hello", role="assistant")
        assert obj.role == "assistant"


class TestRedactedTurn:
    def test_is_dataclass(self):
        assert dataclasses.is_dataclass(RedactedTurn)

    def test_required_fields(self):
        obj = RedactedTurn(turn_id="t1", text="hello")
        assert obj.turn_id == "t1"
        assert obj.text == "hello"

    def test_redaction_counts_defaults_to_empty_dict(self):
        obj = RedactedTurn(turn_id="t1", text="hello")
        assert obj.redaction_counts == {}

    def test_redaction_counts_default_is_not_shared_between_instances(self):
        """A `field(default_factory=dict)` mutable-default bug (a bare `= {}`
        default) would let two instances share the same dict object --
        mutating one would corrupt the other. Guards against that
        regression."""
        obj1 = RedactedTurn(turn_id="t1", text="hello")
        obj2 = RedactedTurn(turn_id="t2", text="world")

        obj1.redaction_counts["api_key"] = 1

        assert obj2.redaction_counts == {}

    def test_all_fields_can_be_set(self):
        obj = RedactedTurn(
            turn_id="t1",
            text="[redacted:api_key]",
            role="user",
            redaction_counts={"api_key": 1},
        )
        assert obj.turn_id == "t1"
        assert obj.text == "[redacted:api_key]"
        assert obj.role == "user"
        assert obj.redaction_counts == {"api_key": 1}


class TestRedactTurnsInput:
    def test_is_dataclass(self):
        assert dataclasses.is_dataclass(RedactTurnsInput)

    def test_required_field_is_turns(self):
        obj = RedactTurnsInput(turns=[])
        assert obj.turns == []

    def test_optional_context_fields_default_to_none(self):
        obj = RedactTurnsInput(turns=[])
        assert obj.workflow_run_id is None
        assert obj.workspace_id is None
        assert obj.document_id is None

    def test_context_fields_can_be_set(self):
        obj = RedactTurnsInput(
            turns=[RedactTurnInput(turn_id="t1", text="hi")],
            workflow_run_id="run-1",
            workspace_id="ws-1",
            document_id="conv-1",
        )
        assert obj.workflow_run_id == "run-1"
        assert obj.workspace_id == "ws-1"
        assert obj.document_id == "conv-1"
        assert len(obj.turns) == 1


class TestRedactTurnsOutput:
    def test_is_dataclass(self):
        assert dataclasses.is_dataclass(RedactTurnsOutput)

    def test_defaults_are_empty(self):
        obj = RedactTurnsOutput()
        assert obj.redacted_turns == []
        assert obj.dropped_turn_ids == []
        assert obj.redaction_counts == {}

    def test_default_containers_are_not_shared_between_instances(self):
        obj1 = RedactTurnsOutput()
        obj2 = RedactTurnsOutput()

        obj1.dropped_turn_ids.append("t1")
        obj1.redaction_counts["api_key"] = 1

        assert obj2.dropped_turn_ids == []
        assert obj2.redaction_counts == {}

    def test_all_fields_can_be_set(self):
        redacted = RedactedTurn(turn_id="t1", text="ok")
        obj = RedactTurnsOutput(
            redacted_turns=[redacted],
            dropped_turn_ids=["t2"],
            redaction_counts={"api_key": 1},
        )
        assert obj.redacted_turns == [redacted]
        assert obj.dropped_turn_ids == ["t2"]
        assert obj.redaction_counts == {"api_key": 1}
