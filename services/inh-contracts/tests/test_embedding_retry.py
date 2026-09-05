"""embed_batch_with_retry (#311 item 5): transient-only retry, bounded sleep.

PR #314 review finding 2: earlier versions of this module's docstring (and
this test file's) called ``retry_budget_s`` a "wall-clock bound" -- it isn't.
It bounds cumulative SLEEP between attempts only; each attempt can still
independently burn a full per-request timeout before this loop ever sees it
fail. See ``max_wall_clock_s`` (tested below) for the formula that actually
describes one call's worst-case wall clock.
"""

from __future__ import annotations

import httpx
import pytest

from inh_contracts.embedding.retry import (
    embed_batch_with_retry,
    is_transient_embed_error,
    max_wall_clock_s,
)


class _FakeProvider:
    """Minimal stand-in -- only embed_batch is exercised by the retry helper."""

    def __init__(self, side_effects: list) -> None:
        self._side_effects = list(side_effects)
        self.calls = 0

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        effect = self._side_effects.pop(0)
        if isinstance(effect, Exception):
            raise effect
        return effect


def _status_error(code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "http://provider.local/embed")
    response = httpx.Response(code, request=request)
    return httpx.HTTPStatusError(f"{code} error", request=request, response=response)


# --- transient error classification -----------------------------------------------------


def test_timeout_is_transient() -> None:
    assert is_transient_embed_error(httpx.TimeoutException("slow"))


def test_network_error_is_transient() -> None:
    assert is_transient_embed_error(httpx.NetworkError("dropped"))


def test_429_is_transient() -> None:
    assert is_transient_embed_error(_status_error(429))


def test_5xx_is_transient() -> None:
    assert is_transient_embed_error(_status_error(503))


@pytest.mark.parametrize("code", [400, 401, 403, 404, 422])
def test_4xx_except_429_is_not_transient(code: int) -> None:
    assert not is_transient_embed_error(_status_error(code))


def test_unrelated_exception_is_not_transient() -> None:
    assert not is_transient_embed_error(ValueError("not an http error"))


# --- retry behavior -----------------------------------------------------------------------


def test_succeeds_after_transient_failures_within_budget() -> None:
    provider = _FakeProvider([_status_error(503), _status_error(503), [[1.0]]])
    sleeps: list[float] = []

    result = embed_batch_with_retry(
        provider, ["x"], max_retries=5, retry_budget_s=10.0, sleep=sleeps.append
    )

    assert result == [[1.0]]
    assert provider.calls == 3
    assert len(sleeps) == 2


def test_non_transient_error_fails_fast_no_sleep() -> None:
    provider = _FakeProvider([_status_error(400)])
    sleeps: list[float] = []

    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        embed_batch_with_retry(
            provider, ["x"], max_retries=5, retry_budget_s=10.0, sleep=sleeps.append
        )

    assert exc_info.value.response.status_code == 400
    assert provider.calls == 1
    assert sleeps == []


def test_exhausts_max_retries_then_raises_last_error() -> None:
    provider = _FakeProvider([_status_error(503)] * 3)
    sleeps: list[float] = []

    with pytest.raises(httpx.HTTPStatusError):
        embed_batch_with_retry(
            provider, ["x"], max_retries=3, retry_budget_s=100.0, sleep=sleeps.append
        )

    assert provider.calls == 3
    # 3 attempts -> at most 2 sleeps between them.
    assert len(sleeps) == 2


def test_total_sleep_never_exceeds_retry_budget() -> None:
    """The SLEEP bound is enforced, not just documented (#311 item 5).

    A huge max_retries with a tiny budget must still terminate with bounded
    total sleep time -- the loop stops retrying once the budget is spent,
    even though attempts remain. This is NOT a wall-clock bound (PR #314
    review finding 2): the attempts themselves, not exercised by this fake
    provider (which fails instantly), are unbounded by this mechanism -- see
    ``test_max_wall_clock_s_*`` below for the formula that covers them too.
    """
    provider = _FakeProvider([_status_error(503)] * 1000)
    sleeps: list[float] = []

    with pytest.raises(httpx.HTTPStatusError):
        embed_batch_with_retry(
            provider, ["x"], max_retries=1000, retry_budget_s=1.0, sleep=sleeps.append
        )

    assert sum(sleeps) <= 1.0
    # Far fewer than 1000 attempts were made before the budget ran out.
    assert provider.calls < 20


def test_retry_budget_of_zero_disables_retrying_entirely() -> None:
    provider = _FakeProvider([_status_error(503), [[1.0]]])
    sleeps: list[float] = []

    with pytest.raises(httpx.HTTPStatusError):
        embed_batch_with_retry(
            provider, ["x"], max_retries=5, retry_budget_s=0.0, sleep=sleeps.append
        )

    assert provider.calls == 1
    assert sleeps == []


# --- max_wall_clock_s (PR #314 review finding 2) -----------------------------------------------


def test_max_wall_clock_s_matches_ingestion_batch_formula() -> None:
    """Same shape weaviate_store_budget.py has used since #228: attempts *
    timeout + sleep budget. With the batch/ingestion defaults this is the
    100s/batch figure that formula's own module docstring cites."""
    assert max_wall_clock_s(attempts=3, timeout_s=30.0, retry_budget_s=10.0) == 100.0


def test_max_wall_clock_s_query_defaults_fit_under_the_15s_consumer_ceiling() -> None:
    """#311's own incident cites a 15s consumer-side ceiling on interactive
    chat search. The query-path defaults (see inh_contracts.embedding.
    defaults) must produce a worst case comfortably under that -- this is
    the number the PR body's retry claim now has to be honest about."""
    from inh_contracts.embedding.defaults import (
        DEFAULT_QUERY_MAX_RETRIES,
        DEFAULT_QUERY_TIMEOUT_S,
        QUERY_RETRY_SLEEP_BUDGET_S,
    )

    worst_case = max_wall_clock_s(
        attempts=DEFAULT_QUERY_MAX_RETRIES,
        timeout_s=DEFAULT_QUERY_TIMEOUT_S,
        retry_budget_s=QUERY_RETRY_SLEEP_BUDGET_S,
    )
    assert worst_case == 12.0
    assert worst_case < 15.0


def test_max_wall_clock_s_treats_zero_attempts_as_one() -> None:
    """A retry loop always makes at least one attempt -- max(1, attempts)."""
    assert max_wall_clock_s(attempts=0, timeout_s=5.0, retry_budget_s=2.0) == 7.0
