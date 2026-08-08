"""Config defaults must be single-sourced, not respelled per call site (#202).

The ingestion half of the pair; see this service's sibling in
inh-public-api-svc/tests/unit/test_settings_config_dedup_contract.py for the
full rationale. Both services talk to the same TEI endpoint with the same
embedding width, and both previously re-hardcoded that URL and dimension in
``services/embedder.py`` as literals separate from their own ``Settings``
field defaults -- so each service could drift from settings.py independently,
and the two services could drift from each other.

Also guards a dead constant staying dead: ``temporal/worker.py`` used to
carry ``TASK_QUEUE_NAME = "document-ingestion"``, an unused duplicate of
``settings.temporal_task_queue``. A duplicate that nothing reads is the
cheapest kind to reintroduce and the most misleading to find later -- a
future reader has no way to tell which of the two the worker actually binds
to without tracing it.

Extended for #176 (same defect class, filed as a follow-up sweep from #132 /
#202; see the public-api twin's docstring for the full rationale -- this
service's Settings has REQUIRED fields, so its tests below assert against
declared field defaults rather than an instantiated Settings for the same
reason as ``test_task_queue_name_duplicate_stays_removed``):

- ``storage_bucket`` and ``mongodb_uri`` each hardcoded their own default
  independently of inh-public-api-svc's ``aws_s3_bucket`` / ``mongodb_uri``.
"""

from __future__ import annotations

import pytest
from inh_contracts.defaults import DEFAULT_MONGODB_URI, DEFAULT_S3_BUCKET

import src.temporal.worker as worker_module
from src.config.settings import Settings
from src.services.embedder import _DEFAULT_DIM, _DEFAULT_URL


# Override the package-level DB-dependent autouse fixture (tests/conftest.py)
# with a no-op. These assertions read module constants only -- without this
# override the whole module silently SKIPS wherever PostgreSQL is absent, so
# an anti-drift guard would report green while checking nothing. Same pattern
# as tests/test_temporal_trigger.py and tests/test_contracts.py.
@pytest.fixture(autouse=True)
def cleanup_test_data():
    """No-op override so this module's tests run without a live database."""
    yield


# Golden values, kept identical to the public-api service's copy on purpose:
# these two constants are the cross-service contract.
GOLDEN_EMBEDDING_URL = "http://text-embeddings-inference:80"
GOLDEN_EMBEDDING_DIM = 384
GOLDEN_S3_BUCKET = "inherent-documents"
GOLDEN_MONGODB_URI = "mongodb://localhost:27017"


def test_embedder_defaults_derive_from_settings() -> None:
    """embedder.py must not re-hardcode what settings.py already declares."""
    assert _DEFAULT_URL == Settings.model_fields["embedding_service_url"].default
    assert _DEFAULT_DIM == Settings.model_fields["embedding_dim"].default


def test_embedder_defaults_match_golden_values() -> None:
    """Pins the shared default itself, so a silent change is visible."""
    assert _DEFAULT_URL == GOLDEN_EMBEDDING_URL
    assert _DEFAULT_DIM == GOLDEN_EMBEDDING_DIM


def test_task_queue_name_duplicate_stays_removed() -> None:
    """The task queue name has exactly one home: settings.temporal_task_queue.

    Asserts against the field default rather than an instantiated Settings:
    this service's Settings has required fields with no defaults, so
    ``Settings()`` raises unless the full environment is present. The
    contract here is about the declared default anyway, not a runtime value.
    """
    assert not hasattr(worker_module, "TASK_QUEUE_NAME")
    assert Settings.model_fields["temporal_task_queue"].default


def test_s3_bucket_default_is_single_sourced() -> None:
    """#176: storage_bucket must derive from the shared cross-service default.

    Ingestion's primary upload path mostly reads the per-message
    ``storage_bucket`` from the event payload rather than this Settings
    default (see ``services/storage.py``'s ``default_bucket`` fallback), so
    this default was rarely the effective value in practice -- still fixed
    for defense in depth and consistency with the #132 precedent (same
    defect shape as the S3 region).
    """
    assert Settings.model_fields["storage_bucket"].default == DEFAULT_S3_BUCKET


def test_s3_bucket_default_matches_golden_value() -> None:
    """A deliberate bucket-name change must come here; an accidental one fails."""
    assert DEFAULT_S3_BUCKET == GOLDEN_S3_BUCKET


def test_mongodb_uri_default_is_single_sourced() -> None:
    """#176: mongodb_uri must derive from the shared cross-service default.

    The database actually opened is selected via ``mongodb_db_name``
    (``mongo_uri=settings.mongodb_uri, db_name=settings.mongodb_db_name`` in
    ``temporal/activities/audit_activities.py``), so the URI's own path
    segment is inert -- the shared default carries no path, matching that it
    is not the source of truth for db selection.
    """
    assert Settings.model_fields["mongodb_uri"].default == DEFAULT_MONGODB_URI


def test_mongodb_uri_default_matches_golden_value() -> None:
    """A deliberate URI change must come here; an accidental one fails."""
    assert DEFAULT_MONGODB_URI == GOLDEN_MONGODB_URI
