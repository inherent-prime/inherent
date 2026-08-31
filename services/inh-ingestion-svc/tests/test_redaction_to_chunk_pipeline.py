"""The flush pipeline ordering is a security property (#306 + #307).

`redact_turns` -> `chunk_conversation` -- `chunk_conversation` must read
turn TEXT ONLY from `redact_turns`'s output
(`RedactTurnsOutput.redacted_turns`), never from the workflow's raw
pre-redaction buffer. `redact_turns`'s `non_retryable=True` stops
REDACTION ITSELF being retried; it does nothing to stop THIS bug class --
a downstream step re-deriving turn text from somewhere other than
`redact_turns`'s output. See redact.py's module docstring ("THE SHARPEST
EDGE") and conversation_chunk.py's module docstring.

This test drives the REAL `redact_turns` activity followed by the REAL
`chunk_conversation` activity -- exactly the call sequence
`ConversationMemoryWorkflow._flush` makes -- and asserts the raw credential
never appears in ANY staged chunk's content, for both a redaction SUCCESS
and a redaction FAILURE (dropped turn) path.

Pure-logic/mocked (no PostgreSQL, no Temporal workflow environment needed):
overrides the package-level `cleanup_test_data` autouse fixture with a
no-op, same pattern as test_redact_turns_activity.py.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.config.settings import Settings
from src.temporal.activities.conversation_chunk import chunk_conversation
from src.temporal.activities.redact import redact_turns
from src.temporal.models import (
    ChunkConversationInput,
    ConversationTurnMeta,
    RedactTurnInput,
    RedactTurnsInput,
)


@pytest.fixture(autouse=True)
def cleanup_test_data():
    """No-op override of the package-level DB-dependent autouse fixture."""
    yield


def _settings(**overrides) -> Settings:
    defaults: dict = {
        "redaction_patterns_extra": [],
        "max_chunk_size": 1000,
        "chunk_overlap": 100,
    }
    defaults.update(overrides)
    return Settings.model_construct(**defaults)


def _mock_db(chunk_count: int = 0):
    db = MagicMock()
    db.get_document_chunk_count = AsyncMock(return_value=chunk_count)
    db.record_redaction_failure = AsyncMock(return_value=1)
    return db


def _mock_staging():
    staging = MagicMock()
    staging.write_chunks = MagicMock()
    return staging


class TestCredentialNeverReachesChunks:
    """The core #306/#307 acceptance criterion: a credential in a turn
    never reaches the chunk output."""

    async def test_credential_redacted_before_chunking_end_to_end(self, monkeypatch):
        monkeypatch.setattr("src.config.settings.get_settings", lambda: _settings(), raising=True)

        secret = "sk-proj-abcdefghijklmnopqrstuvwxyz123456"
        turns = [
            RedactTurnInput(turn_id="t1", text=f"my API key is {secret}", role="user"),
            RedactTurnInput(turn_id="t2", text="what should I do with it?", role="assistant"),
        ]

        # Step 1 (real activity): redact_turns.
        redact_output = await redact_turns(
            RedactTurnsInput(
                turns=turns,
                workflow_run_id="run-pipeline",
                workspace_id="ws-pipeline",
                document_id="conv-ws-pipeline-conv1",
            )
        )
        assert len(redact_output.redacted_turns) == 2
        assert secret not in redact_output.redacted_turns[0].text
        assert "[redacted:api_key]" in redact_output.redacted_turns[0].text

        # Step 2 (real activity): chunk_conversation, fed ONLY redact_output
        # -- never the raw `turns` above. This mirrors _flush exactly.
        mock_db = _mock_db()
        mock_staging = _mock_staging()
        monkeypatch.setattr(
            "src.temporal.shared_services.get_db_service", lambda: mock_db, raising=True
        )
        monkeypatch.setattr(
            "src.temporal.shared_services.get_staging_service",
            lambda: mock_staging,
            raising=True,
        )

        chunk_output = await chunk_conversation(
            ChunkConversationInput(
                workflow_run_id="run-pipeline",
                document_id="conv-ws-pipeline-conv1",
                workspace_id="ws-pipeline",
                redacted_turns=redact_output.redacted_turns,
                turn_meta=[
                    ConversationTurnMeta(turn_id="t1", ts="2026-08-31T00:00:00Z", client="cli"),
                    ConversationTurnMeta(turn_id="t2", ts="2026-08-31T00:00:05Z", client="cli"),
                ],
            )
        )
        assert chunk_output.chunk_count == 2

        # The staged chunks are what store_in_postgresql/store_in_weaviate
        # would persist -- assert the raw secret is in NONE of them.
        mock_staging.write_chunks.assert_called_once()
        _, staged_chunks = mock_staging.write_chunks.call_args[0]
        all_chunk_text = " ".join(c["content"] for c in staged_chunks)
        assert secret not in all_chunk_text
        assert "[redacted:api_key]" in all_chunk_text

        # Turn attribution survived the trip (role/turn_index/ts/client).
        by_turn_id = {c["turn_id"]: c for c in staged_chunks}
        assert by_turn_id["t1"]["role"] == "user"
        assert by_turn_id["t1"]["turn_index"] == 0
        assert by_turn_id["t1"]["turn_ts"] == "2026-08-31T00:00:00Z"
        assert by_turn_id["t1"]["client"] == "cli"
        assert by_turn_id["t2"]["role"] == "assistant"
        assert by_turn_id["t2"]["turn_index"] == 1

    async def test_turn_that_fails_redaction_is_dropped_and_never_chunked(self, monkeypatch):
        """A turn whose redaction pass raises must be ABSENT from
        chunk_conversation's input entirely (per #307's per-turn drop
        contract) -- and therefore absent from every staged chunk too."""
        monkeypatch.setattr("src.config.settings.get_settings", lambda: _settings(), raising=True)

        import src.services.redaction_patterns as redaction_patterns

        secret_that_triggers_failure = "TRIGGER_BOOM_a1b2c3d4e5f6g7h8i9j0k1l2m3n4"
        real_entropy_detector = redaction_patterns._redact_high_entropy_tokens

        def _flaky(text: str):
            if "TRIGGER_BOOM" in text:
                raise ValueError("simulated detector crash")
            return real_entropy_detector(text)

        monkeypatch.setattr(redaction_patterns, "_redact_high_entropy_tokens", _flaky)

        audit_db = _mock_db()
        monkeypatch.setattr(
            "src.temporal.shared_services.get_db_service", lambda: audit_db, raising=True
        )

        redact_output = await redact_turns(
            RedactTurnsInput(
                turns=[
                    RedactTurnInput(
                        turn_id="bad-turn", text=f"secret: {secret_that_triggers_failure}"
                    ),
                    RedactTurnInput(turn_id="good-turn", text="perfectly fine text"),
                ],
                workflow_run_id="run-pipeline-2",
                workspace_id="ws-pipeline",
                document_id="conv-ws-pipeline-conv2",
            )
        )
        assert redact_output.dropped_turn_ids == ["bad-turn"]
        assert [t.turn_id for t in redact_output.redacted_turns] == ["good-turn"]

        # chunk_conversation fed ONLY the surviving redacted_turns -- exactly
        # what _flush does (it never re-adds a dropped turn from its own
        # buffer).
        chunk_db = _mock_db()
        mock_staging = _mock_staging()
        monkeypatch.setattr(
            "src.temporal.shared_services.get_db_service", lambda: chunk_db, raising=True
        )
        monkeypatch.setattr(
            "src.temporal.shared_services.get_staging_service",
            lambda: mock_staging,
            raising=True,
        )

        chunk_output = await chunk_conversation(
            ChunkConversationInput(
                workflow_run_id="run-pipeline-2",
                document_id="conv-ws-pipeline-conv2",
                workspace_id="ws-pipeline",
                redacted_turns=redact_output.redacted_turns,
                turn_meta=[
                    ConversationTurnMeta(turn_id="good-turn", ts="2026-08-31T00:00:00Z"),
                ],
            )
        )
        assert chunk_output.chunk_count == 1

        _, staged_chunks = mock_staging.write_chunks.call_args[0]
        assert len(staged_chunks) == 1
        all_chunk_text = " ".join(c["content"] for c in staged_chunks)
        assert secret_that_triggers_failure not in all_chunk_text
        assert "bad-turn" not in {c["turn_id"] for c in staged_chunks}


class TestChunkConversationTurnAwareChunking:
    """chunk_conversation-specific behavior, independent of redact_turns."""

    async def test_chunk_index_continues_from_existing_chunk_count(self, monkeypatch):
        """append-mode chunk_index continuation (#306 ground truth item 2):
        chunk_index starts at the document's CURRENT chunk_count, not 0."""
        monkeypatch.setattr("src.config.settings.get_settings", lambda: _settings(), raising=True)

        from src.temporal.models import RedactedTurn

        mock_db = _mock_db(chunk_count=5)  # 5 chunks already stored
        mock_staging = _mock_staging()
        monkeypatch.setattr(
            "src.temporal.shared_services.get_db_service", lambda: mock_db, raising=True
        )
        monkeypatch.setattr(
            "src.temporal.shared_services.get_staging_service",
            lambda: mock_staging,
            raising=True,
        )

        await chunk_conversation(
            ChunkConversationInput(
                workflow_run_id="run-x",
                document_id="conv-ws-x-conv1",
                redacted_turns=[
                    RedactedTurn(turn_id="t1", text="hello there", role="user"),
                    RedactedTurn(turn_id="t2", text="general kenobi", role="assistant"),
                ],
                turn_meta=[
                    ConversationTurnMeta(turn_id="t1", ts="t1"),
                    ConversationTurnMeta(turn_id="t2", ts="t2"),
                ],
            )
        )

        _, staged_chunks = mock_staging.write_chunks.call_args[0]
        assert [c["chunk_index"] for c in staged_chunks] == [5, 6]

    async def test_each_chunk_belongs_to_exactly_one_turn(self, monkeypatch):
        """A single oversized turn is split WITHIN itself, never merged with
        a neighboring turn -- role stays a well-defined single value."""
        monkeypatch.setattr(
            "src.config.settings.get_settings",
            lambda: _settings(max_chunk_size=20, chunk_overlap=0),
            raising=True,
        )

        from src.temporal.models import RedactedTurn

        mock_db = _mock_db(chunk_count=0)
        mock_staging = _mock_staging()
        monkeypatch.setattr(
            "src.temporal.shared_services.get_db_service", lambda: mock_db, raising=True
        )
        monkeypatch.setattr(
            "src.temporal.shared_services.get_staging_service",
            lambda: mock_staging,
            raising=True,
        )

        long_turn_text = "word " * 20  # well over max_chunk_size=20
        await chunk_conversation(
            ChunkConversationInput(
                workflow_run_id="run-y",
                document_id="conv-ws-y-conv1",
                redacted_turns=[
                    RedactedTurn(turn_id="long-turn", text=long_turn_text, role="user"),
                    RedactedTurn(turn_id="short-turn", text="ok", role="assistant"),
                ],
                turn_meta=[
                    ConversationTurnMeta(turn_id="long-turn", ts="t1"),
                    ConversationTurnMeta(turn_id="short-turn", ts="t2"),
                ],
            )
        )

        _, staged_chunks = mock_staging.write_chunks.call_args[0]
        long_turn_chunks = [c for c in staged_chunks if c["turn_id"] == "long-turn"]
        short_turn_chunks = [c for c in staged_chunks if c["turn_id"] == "short-turn"]

        assert len(long_turn_chunks) > 1  # split into multiple pieces
        assert len(short_turn_chunks) == 1  # short turn stays whole
        # Every piece of the long turn still carries ONLY that turn's role.
        assert all(c["role"] == "user" for c in long_turn_chunks)
        assert all(c["turn_index"] == 0 for c in long_turn_chunks)
        assert short_turn_chunks[0]["role"] == "assistant"
        assert short_turn_chunks[0]["turn_index"] == 1

    async def test_empty_redacted_turns_produces_no_chunks(self, monkeypatch):
        monkeypatch.setattr("src.config.settings.get_settings", lambda: _settings(), raising=True)

        out = await chunk_conversation(
            ChunkConversationInput(
                workflow_run_id="run-empty",
                document_id="conv-ws-empty-conv1",
                redacted_turns=[],
                turn_meta=[],
            )
        )
        assert out.chunk_count == 0
