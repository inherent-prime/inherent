"""Context-window fetch must not over-fetch far-apart matches (#21).

The old _compute_ranges collapsed every match in a document into one
(min-k, max+k) span, so matches at chunk 2 and 500 pulled ~500 rows even though
only k neighbours around each match are used. It should emit one narrow range
per match, merging only overlapping/adjacent ones.
"""

from __future__ import annotations

from types import SimpleNamespace

from src.services.context_window import _compute_ranges


def _match(doc_id: str, idx: int):
    # _compute_ranges only reads .document_id and .metadata["chunk_index"].
    return SimpleNamespace(document_id=doc_id, metadata={"chunk_index": idx})


def test_far_apart_matches_produce_two_narrow_ranges():
    ranges = _compute_ranges([_match("doc", 2), _match("doc", 500)], k=2)
    assert len(ranges) == 2
    assert ("doc", 0, 4) in ranges
    assert ("doc", 498, 502) in ranges


def test_overlapping_matches_merge():
    # (3,7) and (4,8) overlap -> single (3,8).
    assert _compute_ranges([_match("doc", 5), _match("doc", 6)], k=2) == [("doc", 3, 8)]


def test_adjacent_matches_merge():
    # (0,4) and (3,7) overlap -> (0,7).
    assert _compute_ranges([_match("doc", 2), _match("doc", 5)], k=2) == [("doc", 0, 7)]


def test_lo_clamped_at_zero_and_empty_for_nonpositive_k():
    assert _compute_ranges([_match("doc", 1)], k=3) == [("doc", 0, 4)]
    assert _compute_ranges([_match("doc", 1)], k=0) == []
