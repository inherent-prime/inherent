"""Public-api content_risk scorer matches the frozen vector table (#44 / #133)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

from src.services.content_risk import compute_content_risk


def _load_vectors():
    path = Path(__file__).resolve().parents[4] / "tests" / "chunk_formula_vectors.py"
    spec = importlib.util.spec_from_file_location("chunk_formula_vectors", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_content_risk_matches_frozen_table():
    vectors = _load_vectors()
    for text, level, reasons in vectors.CONTENT_RISK_CASES:
        got_level, got_reasons = compute_content_risk(text)
        assert got_level == level, text
        assert got_reasons == reasons, text
