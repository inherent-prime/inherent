"""Unit tests for chunk content_hash + token_count formulas (#133).

Must match ingestion's live path (chunk_edit / store) so REST/MCP writes
produce the same evidence hash and token estimates as bulk ingestion.
"""

from __future__ import annotations

import hashlib
import math

from src.services.chunk_math import compute_chunk_content_hash, estimate_tokens


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
