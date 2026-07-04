"""Chunk char offsets must map to real source positions (#25).

_chunk_by_sentences / _chunk_by_paragraphs computed start_char/end_char by
accumulating joined-chunk lengths (a guess of the separator width), so with
overlap or non-single-space separators the offsets drifted from the true source
positions — breaking any feature that highlights/verifies a chunk's source span.
"""

from __future__ import annotations

from src.temporal.activities.chunk import _chunk_by_paragraphs, _chunk_by_sentences


def _normalize(s: str) -> str:
    return " ".join(s.split())


def test_sentence_offsets_map_to_source_with_wide_separators():
    # Double spaces between sentences: the join guess (single space) drifts.
    text = "First one here.  Second two here.  Third three here.  Fourth four ok."
    chunks = _chunk_by_sentences(text, "doc", max_size=18, overlap=0)
    assert len(chunks) >= 2
    for c in chunks:
        span = text[c.start_char : c.end_char]
        # The source span (whitespace-normalized) equals the chunk content.
        assert _normalize(span) == _normalize(c.content)


def test_sentence_offsets_with_overlap_stay_accurate():
    text = "Aaa bbb ccc. Ddd eee fff. Ggg hhh iii. Jjj kkk lll."
    chunks = _chunk_by_sentences(text, "doc", max_size=26, overlap=12)
    assert len(chunks) >= 2
    for c in chunks:
        assert _normalize(text[c.start_char : c.end_char]) == _normalize(c.content)


def test_paragraph_offsets_map_to_source():
    text = "Para one body text.\n\nPara two body text.\n\nPara three body text."
    chunks = _chunk_by_paragraphs(text, "doc", max_size=25)
    assert len(chunks) >= 2
    for c in chunks:
        assert _normalize(text[c.start_char : c.end_char]) == _normalize(c.content)
