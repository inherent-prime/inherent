"""Config defaults must be single-sourced, not respelled per call site (#202).

Three values in this service were each written down twice, so a deployment
that changed one and not the other would silently disagree with itself:

- the Postgres database name, spelled "knowledge_base" in BOTH the
  ``database_url`` default and ``cloud_sql_database``. Change the local
  default to point at a renamed database and the Cloud SQL path keeps
  naming the old one.
- the TEI endpoint URL and the embedding dimension, declared as
  ``Settings`` field defaults AND re-hardcoded as ``_DEFAULT_URL`` /
  ``_DEFAULT_DIM`` literals in ``services/embedder.py``. The embedder's
  fallback is what gets used when the env var is absent, so a corrected
  ``Settings`` default would not reach the code path that actually needs
  it. A wrong ``_DEFAULT_DIM`` in particular does not fail loudly -- it
  produces vectors of the wrong width against the configured collection.

This mirrors the anti-drift pattern already used for the S3 region (#132,
test_settings_region_contract.py) and Weaviate naming (#12): the golden
value lives in ONE place and every other site derives from it.

The two assertion styles below are deliberate and catch different faults:

- comparing the embedder's constant to the ``Settings`` field default
  catches someone re-hardcoding a literal in embedder.py that no longer
  matches settings.py -- the drift this change exists to prevent.
- comparing the ``Settings`` field default to a golden literal catches a
  silent change to the shared default itself. That may well be intended,
  but it must be a deliberate edit to this test, not a side effect.

The companion half for inh-ingestion-svc lives in that service's
tests/test_settings_config_dedup_contract.py -- both embedders read the
same TEI service, so each side needs its own guard.

Extended for #176 / #203 (same defect class, filed as a follow-up sweep from
#132 / #202):

- ``aws_s3_bucket`` and ``mongodb_uri`` each hardcoded their own default
  independently of inh-ingestion-svc's ``storage_bucket`` / ``mongodb_uri``
  -- the identical cross-service drift #132 fixed for the S3 region, now
  fixed the same way via ``inh_contracts.defaults``.
- ``health_check_timeout_seconds`` (#203) was a Settings field with ZERO
  call sites -- the health endpoints read hardcoded
  ``DATABASE_HEALTH_CHECK_TIMEOUT`` / ``WEAVIATE_HEALTH_CHECK_TIMEOUT``
  constants instead, so setting the env var silently did nothing. Fixed as
  TWO knobs (``database_health_check_timeout_seconds`` /
  ``weaviate_health_check_timeout_seconds``) rather than one: the two
  dependencies already have independently-tuned degradation thresholds in
  ``api/v1/health.py`` (100ms for Postgres, 500ms for Weaviate -- Weaviate
  vector search is expected to be slower), so a shared single timeout would
  have forced one dependency's probe to wait on the other's tolerance. The
  single generic knob is deleted, not kept alongside the two -- one knob
  that nothing reads plus two more that do would just be a second copy of
  the same defect.
"""

from __future__ import annotations

from inh_contracts.defaults import DEFAULT_MONGODB_URI, DEFAULT_S3_BUCKET

from src.config import constants
from src.config.constants import DEFAULT_DATABASE_NAME
from src.config.settings import Settings
from src.services.embedder import _DEFAULT_DIM, _DEFAULT_URL

# Golden values. Changing these is allowed; changing them by accident is not.
GOLDEN_DATABASE_NAME = "knowledge_base"
GOLDEN_EMBEDDING_URL = "http://text-embeddings-inference:80"
GOLDEN_EMBEDDING_DIM = 384
GOLDEN_S3_BUCKET = "inherent-documents"
GOLDEN_MONGODB_URI = "mongodb://localhost:27017"
GOLDEN_HEALTH_CHECK_TIMEOUT_SECONDS = 5.0


def test_database_name_is_single_sourced() -> None:
    """Both database-name call sites must derive from DEFAULT_DATABASE_NAME.

    Asserts against the declared field defaults, NOT an instantiated
    ``Settings()``. Anywhere the real environment is present -- CI's compose
    stack, any deployment -- ``DATABASE_URL`` is set and legitimately points
    somewhere else, so instantiating here would test the environment rather
    than the deduplication this guards.
    """
    # The Cloud SQL field IS the shared constant, not a copy of its text.
    assert Settings.model_fields["cloud_sql_database"].default == DEFAULT_DATABASE_NAME
    # The local URL embeds it rather than respelling it.
    assert Settings.model_fields["database_url"].default.endswith(f"/{DEFAULT_DATABASE_NAME}")


def test_database_name_matches_golden_value() -> None:
    """A deliberate rename must come here; an accidental one fails."""
    assert DEFAULT_DATABASE_NAME == GOLDEN_DATABASE_NAME


def test_embedder_defaults_derive_from_settings() -> None:
    """embedder.py must not re-hardcode what settings.py already declares."""
    assert _DEFAULT_URL == Settings.model_fields["embedding_service_url"].default
    assert _DEFAULT_DIM == Settings.model_fields["embedding_dim"].default


def test_embedder_defaults_match_golden_values() -> None:
    """Pins the shared default itself, so a silent change is visible."""
    assert _DEFAULT_URL == GOLDEN_EMBEDDING_URL
    assert _DEFAULT_DIM == GOLDEN_EMBEDDING_DIM


def test_s3_bucket_default_is_single_sourced() -> None:
    """#176: aws_s3_bucket must derive from the shared cross-service default.

    Asserts against the declared field default, not an instantiated
    ``Settings()`` -- see module docstring on why (a real deployment/CI
    environment legitimately sets AWS_S3_BUCKET, which would make this test
    check the environment instead of the code-level default).
    """
    assert Settings.model_fields["aws_s3_bucket"].default == DEFAULT_S3_BUCKET


def test_s3_bucket_default_matches_golden_value() -> None:
    """A deliberate bucket-name change must come here; an accidental one fails."""
    assert DEFAULT_S3_BUCKET == GOLDEN_S3_BUCKET


def test_mongodb_uri_default_is_single_sourced() -> None:
    """#176: mongodb_uri must derive from the shared cross-service default.

    The database actually opened is selected via ``mongodb_db_name``
    (``client[settings.mongodb_db_name]``, see ``services/mongo_client.py``),
    so the URI's own path segment is inert -- the shared default carries no
    path, matching that it is not the source of truth for db selection.
    """
    assert Settings.model_fields["mongodb_uri"].default == DEFAULT_MONGODB_URI


def test_mongodb_uri_default_matches_golden_value() -> None:
    """A deliberate URI change must come here; an accidental one fails."""
    assert DEFAULT_MONGODB_URI == GOLDEN_MONGODB_URI


def test_single_health_check_timeout_knob_is_removed() -> None:
    """#203: the dead ``health_check_timeout_seconds`` knob must stay gone.

    It was declared but had zero call sites -- health.py read hardcoded
    constants instead, so setting the env var silently did nothing. Two
    per-dependency knobs replace it (see the tests below); re-adding this
    single knob alongside them would reintroduce a duplicate nothing reads.
    """
    assert "health_check_timeout_seconds" not in Settings.model_fields


def test_health_check_timeout_settings_have_independent_defaults() -> None:
    """#203: Postgres and Weaviate each get their own configurable timeout."""
    assert (
        Settings.model_fields["database_health_check_timeout_seconds"].default
        == GOLDEN_HEALTH_CHECK_TIMEOUT_SECONDS
    )
    assert (
        Settings.model_fields["weaviate_health_check_timeout_seconds"].default
        == GOLDEN_HEALTH_CHECK_TIMEOUT_SECONDS
    )


def test_health_check_timeout_constants_are_removed() -> None:
    """#203: the hardcoded constants health.py used instead of Settings are gone.

    Their continued existence is exactly the defect -- a duplicate default
    that the real code path reads instead of the operator-facing setting.
    """
    assert not hasattr(constants, "DATABASE_HEALTH_CHECK_TIMEOUT")
    assert not hasattr(constants, "WEAVIATE_HEALTH_CHECK_TIMEOUT")


def test_health_endpoints_read_settings_not_hardcoded_constants() -> None:
    """#203: health.py must call through Settings, the operator-facing surface.

    A source-level check (rather than only checking the constants are gone)
    catches a regression where someone reintroduces a *new* hardcoded
    literal timeout in health.py without going through constants.py at all.
    """
    import inspect

    import src.api.v1.health as health_module

    source = inspect.getsource(health_module)
    assert "settings.database_health_check_timeout_seconds" in source
    assert "settings.weaviate_health_check_timeout_seconds" in source


def test_weaviate_is_connected_requires_an_explicit_timeout_param() -> None:
    """#203 (deeper instance): SearchService.is_connected must take a timeout arg.

    ``asyncio.wait_for(search_service.is_connected(), timeout=...)`` alone
    was not enough -- ``is_connected`` hardcoded its OWN 5.0s timeout on the
    inner ``httpx`` request, so an operator raising
    ``WEAVIATE_HEALTH_CHECK_TIMEOUT_SECONDS`` above 5.0 would still see the
    check fail at 5.0s: the exact "setting is accepted but ignored" defect
    #203 exists to fix, just one call deeper than the constant it started
    from. See ``tests/unit/test_health_readiness.py`` for the behavioral test
    that the configured value actually reaches the call.

    The parameter must have NO default. A default (even one that happens to
    equal the current shipped 5.0) is itself a third copy of the health-check
    timeout: any future caller that forgets the kwarg silently falls back to
    a hardcoded literal instead of erroring, reintroducing this exact defect
    one layer down. Requiring it makes that mistake fail loudly (TypeError)
    instead of silently.
    """
    import inspect

    from src.services.search import SearchService

    sig = inspect.signature(SearchService.is_connected)
    assert "timeout" in sig.parameters
    assert sig.parameters["timeout"].default is inspect.Parameter.empty
