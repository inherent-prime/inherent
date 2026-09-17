"""Chunk content_hash and token_count formulas shared by public-api writes (#133).

Duplicates the live ingestion formulas (not a shared package) so REST/MCP
chunk CRUD produces the same evidence hash and token estimates as bulk
ingestion / ChunkEditWorkflow. Do not import inh-ingestion-svc from here.
"""

from __future__ import annotations

import hashlib
import math

# Same constant as inh-ingestion-svc/src/temporal/activities/chunk.py
_CHARS_PER_TOKEN = 4


def compute_chunk_content_hash(content: str) -> str:
    """SHA-256 hex digest of UTF-8 content (live #41 path — no whitespace normalize)."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def estimate_tokens(text: str) -> int:
    """Estimate model tokens without a tokenizer.

    Formula (matches ingestion ``estimate_tokens``):

        est_tokens = ceil(max(words * 1.3, chars / 4))

    Empty string → 0.
    """
    if not text:
        return 0
    words = len(text.split())
    chars = len(text)
    return int(math.ceil(max(words * 1.3, chars / _CHARS_PER_TOKEN)))
