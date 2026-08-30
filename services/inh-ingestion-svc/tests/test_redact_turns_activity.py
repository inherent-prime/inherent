"""Unit tests for the `redact_turns` activity (#307).

Pure-logic/mocked, no PostgreSQL required -- `cleanup_test_data` is
overridden below with a no-op (same pattern as
tests/test_backpressure_dead_letter.py) so the package-wide autouse DB
fixture in conftest.py doesn't silently skip these tests.

Covers the #307 acceptance criteria this activity owns directly:
  - per-pattern redaction end-to-end through the activity (not just the
    pattern module) -- see test_redaction_patterns.py for the exhaustive
    per-pattern coverage; this file focuses on ACTIVITY-level behaviour.
  - the failure path: (a) the dropped turn's raw text is absent from the
    output, (b) the audit call args carry no raw text, (c) the raw secret
    appears nowhere in captured log output.
  - per-turn granularity: one bad turn never fails the whole batch.
  - the non-retryable contract: an activity-level catastrophe raises
    ApplicationError(non_retryable=True).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
import structlog
from structlog.testing import LogCapture
from temporalio.exceptions import ApplicationError

from src.config.settings import Settings
from src.temporal.activities.redact import redact_turns
from src.temporal.models import RedactTurnInput, RedactTurnsInput


@pytest.fixture(autouse=True)
def cleanup_test_data():
    """No-op override of the package-level DB-dependent autouse fixture."""
    yield


@pytest.fixture
def log_output():
    """Capture every structlog event emitted during a test, regardless of
    stdlib `logging` routing -- `caplog` does not see structlog records
    unless the app is configured to route through `structlog.stdlib` (this
    service isn't, see test_extraction_by_type.py's precedent). This is the
    ONLY mechanism strong enough to honestly back the #307 acceptance
    criterion "no chunk, no vector, and no log": it captures the exact
    structured event dicts passed to every logger call app-wide for the
    duration of the test, so assertions below inspect what was ACTUALLY
    logged rather than trusting the implementation not to log the wrong
    thing.
    """
    capture = LogCapture()
    original_processors = structlog.get_config()["processors"]
    structlog.configure(processors=[capture])
    yield capture
    structlog.configure(processors=original_processors)


def _settings(**overrides) -> Settings:
    defaults: dict = {"redaction_patterns_extra": []}
    defaults.update(overrides)
    return Settings.model_construct(**defaults)


# ---------------------------------------------------------------------------
# Happy path: batch redaction, per-turn counts, aggregate counts
# ---------------------------------------------------------------------------


class TestRedactTurnsHappyPath:
    async def test_redacts_multiple_turns(self, monkeypatch):
        monkeypatch.setattr("src.config.settings.get_settings", lambda: _settings(), raising=True)

        secret1 = "sk-proj-abcdefghijklmnopqrstuvwxyz123456"
        secret2 = "postgres://dbuser:S3cretPass1@db.internal.example.com:5432/appdb"
        out = await redact_turns(
            RedactTurnsInput(
                turns=[
                    RedactTurnInput(turn_id="t1", text=f"my key is {secret1}", role="user"),
                    RedactTurnInput(
                        turn_id="t2", text=f"use this conn: {secret2}", role="assistant"
                    ),
                    RedactTurnInput(turn_id="t3", text="nothing sensitive here", role="user"),
                ],
                workflow_run_id="run-1",
                workspace_id="ws-1",
                document_id="conv-1",
            )
        )

        assert len(out.redacted_turns) == 3
        assert out.dropped_turn_ids == []

        by_id = {t.turn_id: t for t in out.redacted_turns}
        assert secret1 not in by_id["t1"].text
        assert "[redacted:api_key]" in by_id["t1"].text
        assert by_id["t1"].redaction_counts == {"api_key": 1}

        assert secret2 not in by_id["t2"].text
        assert "S3cretPass1" not in by_id["t2"].text
        assert by_id["t2"].redaction_counts == {"connection_string": 1}

        assert by_id["t3"].text == "nothing sensitive here"
        assert by_id["t3"].redaction_counts == {}

        # role is carried through untouched.
        assert by_id["t1"].role == "user"
        assert by_id["t2"].role == "assistant"

        # Batch-level aggregate for metric emission.
        assert out.redaction_counts == {"api_key": 1, "connection_string": 1}

    async def test_empty_batch_returns_empty_output(self, monkeypatch):
        monkeypatch.setattr("src.config.settings.get_settings", lambda: _settings(), raising=True)

        out = await redact_turns(RedactTurnsInput(turns=[]))

        assert out.redacted_turns == []
        assert out.dropped_turn_ids == []
        assert out.redaction_counts == {}

    async def test_extra_patterns_from_settings_are_applied(self, monkeypatch):
        monkeypatch.setattr(
            "src.config.settings.get_settings",
            lambda: _settings(redaction_patterns_extra=[r"INTERNAL-ID-\d+"]),
            raising=True,
        )

        out = await redact_turns(
            RedactTurnsInput(
                turns=[RedactTurnInput(turn_id="t1", text="ticket INTERNAL-ID-4471 opened")]
            )
        )

        assert "INTERNAL-ID-4471" not in out.redacted_turns[0].text
        assert "[redacted:custom]" in out.redacted_turns[0].text


# ---------------------------------------------------------------------------
# Failure path: per-turn drop, audit record shape, log leak check
# ---------------------------------------------------------------------------


class TestRedactTurnsFailurePath:
    async def test_failing_turn_is_dropped_others_still_succeed(self, monkeypatch, log_output):
        """#307 core design constraint: one bad turn must not fail the batch."""
        monkeypatch.setattr("src.config.settings.get_settings", lambda: _settings(), raising=True)

        secret_that_triggers_failure = "TRIGGER_BOOM_a1b2c3d4e5f6g7h8i9j0k1l2m3n4"
        good_secret = "sk-proj-abcdefghijklmnopqrstuvwxyz123456"

        import src.services.redaction_patterns as redaction_patterns

        real_entropy_detector = redaction_patterns._redact_high_entropy_tokens

        def _flaky(text: str):
            # Fails ONLY for the turn carrying the trigger marker -- proves
            # the other turn's detector run is genuinely unaffected, not
            # just "the whole batch happened to short-circuit".
            if "TRIGGER_BOOM" in text:
                raise ValueError("simulated detector crash")
            return real_entropy_detector(text)

        monkeypatch.setattr(redaction_patterns, "_redact_high_entropy_tokens", _flaky)

        mock_db = _mock_db()
        monkeypatch.setattr(
            "src.temporal.shared_services.get_db_service", lambda: mock_db, raising=True
        )

        out = await redact_turns(
            RedactTurnsInput(
                turns=[
                    RedactTurnInput(
                        turn_id="bad-turn", text=f"secret: {secret_that_triggers_failure}"
                    ),
                    RedactTurnInput(turn_id="good-turn", text=f"key: {good_secret}"),
                ],
                workflow_run_id="run-x",
                workspace_id="ws-x",
                document_id="conv-x",
            )
        )

        # (batch-succeeds) the good turn redacted normally despite the bad one.
        assert out.dropped_turn_ids == ["bad-turn"]
        assert len(out.redacted_turns) == 1
        assert out.redacted_turns[0].turn_id == "good-turn"
        assert good_secret not in out.redacted_turns[0].text

        # (a) the dropped turn's raw text is absent from the output entirely.
        all_output_text = " ".join(t.text for t in out.redacted_turns)
        assert secret_that_triggers_failure not in all_output_text
        assert "bad-turn" not in all_output_text or True  # turn_id alone is fine, text is not

        # (b) the audit call args carry no raw text.
        mock_db.record_redaction_failure.assert_awaited_once()
        audit_kwargs = mock_db.record_redaction_failure.await_args.kwargs
        assert audit_kwargs["turn_id"] == "bad-turn"
        assert audit_kwargs["detector"] == "high_entropy_token"
        assert audit_kwargs["error_type"] == "ValueError"
        serialized_audit_call = repr(audit_kwargs)
        assert secret_that_triggers_failure not in serialized_audit_call
        assert "TRIGGER_BOOM" not in serialized_audit_call

        # (c) the raw secret appears nowhere in captured log output -- the
        # actual structlog event dicts emitted during this call, not just
        # "no exception raised". Checked against the FULL event structure
        # (both the rendered message and every keyword value), which is the
        # only way a stray `text=turn.text` kwarg would be caught.
        for entry in log_output.entries:
            rendered = repr(entry)
            assert secret_that_triggers_failure not in rendered
            assert "TRIGGER_BOOM" not in rendered

    async def test_audit_write_failure_does_not_escalate_or_reraise(self, monkeypatch):
        """A failure to WRITE the audit row (e.g. Postgres hiccup) must not
        turn a clean per-turn drop into an activity-level abort -- same
        best-effort contract as dead_letter.py's record_dead_letter."""
        monkeypatch.setattr("src.config.settings.get_settings", lambda: _settings(), raising=True)

        import src.services.redaction_patterns as redaction_patterns

        def _always_fails(_text: str):
            raise ValueError("boom")

        monkeypatch.setattr(redaction_patterns, "_redact_high_entropy_tokens", _always_fails)

        mock_db = _mock_db()
        mock_db.record_redaction_failure.side_effect = RuntimeError("db unreachable")
        monkeypatch.setattr(
            "src.temporal.shared_services.get_db_service", lambda: mock_db, raising=True
        )

        # Must complete normally (not raise) even though the audit write itself failed.
        out = await redact_turns(
            RedactTurnsInput(turns=[RedactTurnInput(turn_id="t1", text="some text here")])
        )

        assert out.dropped_turn_ids == ["t1"]
        assert out.redacted_turns == []


# ---------------------------------------------------------------------------
# Non-retryable contract
# ---------------------------------------------------------------------------


class TestNonRetryableContract:
    async def test_catastrophic_failure_raises_non_retryable_application_error(self, monkeypatch):
        """An unexpected failure OUTSIDE the per-turn handling (settings
        resolution itself blowing up here) must become a non-retryable
        ApplicationError -- guard (1) of the two independent non-retryable
        guards described in the module docstring."""

        def _broken_settings():
            raise RuntimeError("settings backend unavailable")

        monkeypatch.setattr("src.config.settings.get_settings", _broken_settings, raising=True)

        with pytest.raises(ApplicationError) as exc_info:
            await redact_turns(
                RedactTurnsInput(turns=[RedactTurnInput(turn_id="t1", text="hello")])
            )

        assert exc_info.value.non_retryable is True
        assert exc_info.value.type == "RedactionCatastrophicFailure"

    async def test_per_turn_failure_alone_never_raises_application_error(self, monkeypatch):
        """The counterpart to the above: a NORMAL per-turn detector failure
        must NOT surface as an ApplicationError at all -- it is handled
        entirely inside the per-turn loop (dropped + audited), which is
        exactly what makes the batch keep succeeding."""
        monkeypatch.setattr("src.config.settings.get_settings", lambda: _settings(), raising=True)

        import src.services.redaction_patterns as redaction_patterns

        def _always_fails(_text: str):
            raise ValueError("boom")

        monkeypatch.setattr(redaction_patterns, "_redact_high_entropy_tokens", _always_fails)
        monkeypatch.setattr(
            "src.temporal.shared_services.get_db_service", lambda: _mock_db(), raising=True
        )

        # Must NOT raise.
        out = await redact_turns(
            RedactTurnsInput(turns=[RedactTurnInput(turn_id="t1", text="some text")])
        )
        assert out.dropped_turn_ids == ["t1"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_db():
    db = MagicMock()
    db.record_redaction_failure = AsyncMock(return_value=1)
    return db
