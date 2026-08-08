"""The S3/object-store region default must match the public-api service's (#132).

Both services accept an S3-region env var, but each previously hardcoded its
OWN fallback default in its Settings class: inh-ingestion-svc defaulted to
"nbg1" (a Hetzner Object Storage location code) while inh-public-api-svc
defaulted to "eu-central-1" (an AWS-style region string) -- not even the same
naming scheme. A deployment that sets the region env var for only one service
silently leaves the other on its own default, so uploads land in one
region/bucket and reads target another.

This mirrors the existing anti-drift pattern for Weaviate naming (#12, see
src/services/weaviate.py and tests/test_naming_contract.py): the golden value
now lives in ONE place (inh_contracts.defaults.DEFAULT_S3_REGION) and both
services' Settings classes must default to it. The companion test in
inh-public-api-svc's test suite (test_settings_region_contract.py) is the
other half of the pair and catches the case where only one side drifts.
"""

from __future__ import annotations

import pytest
from inh_contracts.defaults import DEFAULT_S3_REGION

from src.config.settings import Settings


@pytest.fixture(autouse=True)
def cleanup_test_data():
    """No-op override of the package-level DB-dependent autouse fixture.

    Same rationale as test_naming_contract.py: this test is pure/offline and
    must not skip when PostgreSQL is unavailable.
    """
    yield


def test_s3_region_default_matches_shared_contract():
    """s3_region must fall back to the single shared default (#132)."""
    field = Settings.model_fields["s3_region"]
    assert field.default == DEFAULT_S3_REGION
