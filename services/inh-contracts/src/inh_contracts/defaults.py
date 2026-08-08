"""Shared configuration defaults -- single source of truth (#132).

Both services accept an S3/object-store region override via an env var, but
each used to hardcode its OWN fallback default in its pydantic ``Settings``
class:

- ``inh-public-api-svc`` defaulted ``aws_s3_region`` to ``"eu-central-1"``.
- ``inh-ingestion-svc`` defaulted ``s3_region`` to ``"nbg1"`` (a Hetzner
  Object Storage location code -- not even the same naming scheme as an
  AWS-style region string).

A deployment that sets the region env var for only one service (or relies on
defaults during local dev / a bare ``uv run`` without docker-compose) silently
leaves the other service on its own, different default. Uploads then land in
one region/bucket while reads target another -- #132.

``DEFAULT_S3_REGION`` is now the ONE default both services' Settings classes
import, so the code-level fallback (used whenever the region env var is
unset) can never disagree between the two services again. This mirrors the
existing anti-drift pattern for Weaviate naming (see ``inh_contracts.naming``,
#12): put the single value here, have both services import it, and pin a
contract test on each side (``test_settings_region_contract.py``) so a future
hardcoded literal on either side fails CI instead of drifting silently.

The value matches the default already baked into the deployed stack
(``docker-compose.yml``, ``docker-compose.release.yml``, ``infra/server.tf``
and ``.env.example`` all default ``AWS_REGION`` to ``us-east-1``), so a
service now agrees with the documented, deployed default even when started
directly (e.g. tests, local ``uv run``) without compose's env injection.
"""

DEFAULT_S3_REGION = "us-east-1"

# ---------------------------------------------------------------------------
# #176 -- two more cross-service config defaults drifting the same way the
# S3 region did before #132. Both found while re-reading each service's
# Settings class during the #132 pattern sweep; scripts/validate_env.py
# already runtime-checks the resolved values for both, but that only warns
# once an operator has actually set the two env vars to *different* values --
# it does not catch the two services silently defaulting to different values
# when an operator sets neither.
# ---------------------------------------------------------------------------

# ``inh-ingestion-svc`` defaulted ``storage_bucket`` (env ``STORAGE_BUCKET``)
# to ``""`` while ``inh-public-api-svc`` defaulted ``aws_s3_bucket`` (env
# ``AWS_S3_BUCKET``) to ``"inherent-documents"``. An operator who sets
# neither env var got ingestion writing to an empty bucket name while
# public-api read from ``"inherent-documents"`` -- the exact #132 shape.
# Ingestion's primary upload path mostly uses the per-message
# ``storage_bucket`` from the event payload rather than this Settings
# default (see ``services/storage.py``'s ``default_bucket`` fallback), so the
# empty-string default was rarely the effective value -- but the same
# single-source-of-truth fix applies for defense in depth and consistency
# with the #132 precedent. Both services now default to this shared bucket
# name; the real deployed bucket is still fully operator-configurable via
# ``STORAGE_BUCKET`` / ``AWS_S3_BUCKET``.
DEFAULT_S3_BUCKET = "inherent-documents"

# Both services also defaulted ``mongodb_db_name`` to ``"main"`` and both
# select the database explicitly via that field when opening the Mongo
# client (``client[settings.mongodb_db_name]``, see
# ``inh-public-api-svc/src/services/mongo_client.py`` and
# ``inh-ingestion-svc/src/temporal/activities/audit_activities.py``) -- so
# the URI's own path segment is inert either way. Still, ``mongodb_uri``
# defaulted to two different literals (``.../27017`` vs ``.../27017/main``),
# the same "hardcoded twice, drifted slightly" shape #132 exists to prevent.
# Single-sourced here with no path segment -- the path is not the source of
# truth for database selection, ``mongodb_db_name`` is, so the shared
# default should not imply otherwise.
DEFAULT_MONGODB_URI = "mongodb://localhost:27017"
