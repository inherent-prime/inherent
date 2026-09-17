"""embedding_defaults.py must not drift from the shared inh_contracts values (#311).

``src/services/embedding_defaults.py`` deliberately does NOT import
``inh_contracts.embedding`` -- ``weaviate_store_budget.py`` (which reads these
constants) is imported inside the Temporal *workflow sandbox*
(``document_ingestion.py``), and ``inh_contracts.embedding``'s package
``__init__`` transitively imports httpx/threading, which is exactly the class
of import this module's docstring says the sandbox-safe budget code avoids
("stdlib + embedding_defaults only"). So the two constant sets are kept as
two literal, independently-edited copies -- and THIS test is what keeps them
from silently drifting apart, the same anti-drift pattern
``test_settings_config_dedup_contract.py`` already uses for the URL/dim
defaults.
"""

from __future__ import annotations

from inh_contracts.embedding.defaults import (
    BATCH_RETRY_SLEEP_BUDGET_S as CONTRACTS_BATCH_RETRY_SLEEP_BUDGET_S,
)
from inh_contracts.embedding.defaults import (
    DEFAULT_BATCH_MAX_RETRIES as CONTRACTS_DEFAULT_BATCH_MAX_RETRIES,
)
from inh_contracts.embedding.defaults import DEFAULT_BATCH_SIZE as CONTRACTS_DEFAULT_BATCH_SIZE
from inh_contracts.embedding.defaults import (
    DEFAULT_MAX_CONCURRENCY as CONTRACTS_DEFAULT_MAX_CONCURRENCY,
)
from inh_contracts.embedding.defaults import DEFAULT_TIMEOUT_S as CONTRACTS_DEFAULT_TIMEOUT_S

from src.services.embedding_defaults import (
    BATCH_RETRY_SLEEP_BUDGET_S,
    DEFAULT_BATCH_MAX_RETRIES,
    DEFAULT_BATCH_SIZE,
    DEFAULT_MAX_CONCURRENCY,
    DEFAULT_TIMEOUT_S,
)


def test_batch_size_matches_shared_default() -> None:
    assert DEFAULT_BATCH_SIZE == CONTRACTS_DEFAULT_BATCH_SIZE


def test_max_concurrency_matches_shared_default() -> None:
    assert DEFAULT_MAX_CONCURRENCY == CONTRACTS_DEFAULT_MAX_CONCURRENCY


def test_timeout_matches_shared_default() -> None:
    assert DEFAULT_TIMEOUT_S == CONTRACTS_DEFAULT_TIMEOUT_S


def test_batch_max_retries_matches_shared_default() -> None:
    assert DEFAULT_BATCH_MAX_RETRIES == CONTRACTS_DEFAULT_BATCH_MAX_RETRIES


def test_batch_retry_sleep_budget_matches_shared_default() -> None:
    assert BATCH_RETRY_SLEEP_BUDGET_S == CONTRACTS_BATCH_RETRY_SLEEP_BUDGET_S
