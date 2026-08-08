"""The S3/object-store region default must match the ingestion service's (#132).

Both services accept an S3-region env var, but each previously hardcoded its
OWN fallback default in its Settings class: public-api defaulted to
"eu-central-1" while ingestion defaulted to "nbg1" (a Hetzner Object Storage
location code -- not even the same naming scheme). A deployment that sets the
region env var for only one service silently leaves the other on its own
default, so uploads land in one region/bucket and reads target another.

This mirrors the existing anti-drift pattern for Weaviate naming (#12, see
services/inh-contracts/src/inh_contracts/naming.py and
tests/unit/test_naming_contract.py): the golden value now lives in ONE place
(inh_contracts.defaults.DEFAULT_S3_REGION) and both services' Settings
classes must default to it. If either service's Settings class hardcodes its
own literal again instead of importing the shared constant, this test still
passes only by coincidence -- the companion test in inh-ingestion-svc's test
suite (test_settings_region_contract.py) is the other half of the pair and
catches the case where only one side drifts.

Blocker-1 follow-up: aligning the DEFAULT was not enough -- ingestion reads
AWS_REGION while public-api read only AWS_S3_REGION, so an operator following
docs/deploy/production.md step 3 ("set AWS_REGION=<your-region>") configured
ingestion but silently left public-api on DEFAULT_S3_REGION. This is the same
single-env-var precedent already used for the upload topic (#15, see
test_settings_topic_contract.py): public-api now also accepts AWS_REGION via
AliasChoices, with AWS_S3_REGION still taking precedence when both are set.
"""

from __future__ import annotations

import pytest
from inh_contracts.defaults import DEFAULT_S3_REGION

from src.config.settings import Settings

# Part of the selectable contract-regression surface (M6 #30).
pytestmark = [pytest.mark.contract]


def test_s3_region_default_matches_shared_contract():
    """aws_s3_region must fall back to the single shared default (#132)."""
    field = Settings.model_fields["aws_s3_region"]
    assert field.default == DEFAULT_S3_REGION


def test_aws_region_alone_configures_public_api(monkeypatch: pytest.MonkeyPatch):
    """A lone AWS_REGION (ingestion's var) must configure public-api too (#132 blocker 1).

    Without this, an operator following docs/deploy/production.md step 3 --
    which sets only AWS_REGION -- silently leaves public-api on its default
    region while ingestion moves to the configured one.
    """
    monkeypatch.delenv("AWS_S3_REGION", raising=False)
    monkeypatch.setenv("AWS_REGION", "eu-central-1")
    s = Settings(_env_file=None)
    assert s.aws_s3_region == "eu-central-1"


def test_aws_s3_region_overrides_aws_region_when_both_set(monkeypatch: pytest.MonkeyPatch):
    """AWS_S3_REGION must win when an operator sets both (#132 blocker 1).

    Lets a deployment deliberately point public-api at a different region
    from ingestion (e.g. a regional read replica) by setting the
    public-api-specific var explicitly, rather than the fallback always
    winning silently.
    """
    monkeypatch.setenv("AWS_REGION", "eu-central-1")
    monkeypatch.setenv("AWS_S3_REGION", "ap-southeast-1")
    s = Settings(_env_file=None)
    assert s.aws_s3_region == "ap-southeast-1"
