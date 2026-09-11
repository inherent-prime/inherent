"""Pin ingestion hash / token / risk formulas against the shared frozen table.

A one-sided change in public-api or ingestion fails that side; updating the
table without updating this service fails here.
"""

from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path

import pytest

from src.services.quality import compute_content_risk
from src.temporal.activities.chunk import estimate_tokens


@pytest.fixture(autouse=True)
async def cleanup_test_data():
    """Override global autouse cleanup -- these tests are offline formula pins."""
    yield


@pytest.fixture()
def db_service():
    """Override -- no PostgreSQL needed to pin hash/token/risk formulas."""
    yield None


def _load_vectors():
    path = Path(__file__).resolve().parents[3] / "tests" / "chunk_formula_vectors.py"
    spec = importlib.util.spec_from_file_location("chunk_formula_vectors", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_hash_and_tokens_match_frozen_table():
    vectors = _load_vectors()
    for content, digest, tokens in vectors.HASH_TOKEN_CASES:
        assert hashlib.sha256(content.encode("utf-8")).hexdigest() == digest
        assert estimate_tokens(content) == tokens


def test_content_risk_matches_frozen_table():
    vectors = _load_vectors()
    for text, level, reasons in vectors.CONTENT_RISK_CASES:
        got_level, got_reasons = compute_content_risk(text)
        assert got_level == level, text
        assert got_reasons == reasons, text
