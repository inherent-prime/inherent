"""Unit tests for chunk content_hash + token_count formulas (#133).

Must match ingestion's live path (chunk_edit / store) so REST/MCP writes
produce the same evidence hash and token estimates as bulk ingestion.
"""

from __future__ import annotations

import hashlib
import math

from src.services.chunk_math import compute_chunk_content_hash, estimate_tokens


def _load_vectors():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[4] / "tests" / "chunk_formula_vectors.py"
    spec = importlib.util.spec_from_file_location("chunk_formula_vectors", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestFrozenVectorTable:
    def test_hash_and_tokens_match_frozen_table(self):
        vectors = _load_vectors()
        for content, digest, tokens in vectors.HASH_TOKEN_CASES:
            assert compute_chunk_content_hash(content) == digest
            assert estimate_tokens(content) == tokens


class TestComputeChunkContentHash:
    def test_sha256_of_utf8_bytes(self):
        content = "hello world"
        assert (
            compute_chunk_content_hash(content)
            == hashlib.sha256(content.encode("utf-8")).hexdigest()
        )

    def test_empty_string_is_valid_hash(self):
        assert compute_chunk_content_hash("") == hashlib.sha256(b"").hexdigest()

    def test_does_not_normalize_whitespace(self):
        # Live ingestion hashes raw content (no strip / CRLF rewrite).
        a = compute_chunk_content_hash("line\r\n")
        b = compute_chunk_content_hash("line\n")
        assert a != b


class TestEstimateTokens:
    def test_empty_is_zero(self):
        assert estimate_tokens("") == 0

    def test_matches_ingestion_formula(self):
        text = "one two three four five"
        words = len(text.split())
        chars = len(text)
        expected = int(math.ceil(max(words * 1.3, chars / 4)))
        assert estimate_tokens(text) == expected

    def test_chars_branch_dominates_dense_text(self):
        # Few spaces → chars/4 wins over words*1.3
        text = "x" * 100
        assert estimate_tokens(text) == int(math.ceil(100 / 4))
