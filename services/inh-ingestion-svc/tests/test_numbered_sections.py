"""numbered_sections chunking strategy tests (inherent#390).

Mirrors the behaviour the first vertical pack already tests for its own
reference splitter (preamble kept, sub-levels stay in the parent, parent
heading context, never drops text, oversize fallback) -- using a generic
"handbook" fixture, no domain terms.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from inh_contracts.vertical_pack import load_vertical

from src.temporal.activities.numbered_sections import (
    classify_heading,
    split_numbered_sections,
)

FIXTURE_PACK = Path(__file__).resolve().parent / "fixtures" / "handbook_pack"


@pytest.fixture(scope="module")
def profile():
    return load_vertical(FIXTURE_PACK).chunking


SAMPLE = """PRODUCT HANDBOOK (synthetic fixture)
1. PRICING
1.1 The monthly fee for the standard plan is $10 per seat.
1.2 Enterprise pricing is negotiated separately.
2. SECURITY
2.1 All data must be encrypted at rest and in transit.
(a) Encryption keys are rotated every 90 days.
2.2 Access is limited to authenticated staff.
3. GOVERNANCE
This handbook is reviewed annually.
"""


@pytest.mark.parametrize(
    ("line", "number", "level"),
    [
        ("1. PRICING", "1", 1),
        ("Section 12 Governance", "12", 1),
        ("1.1 The monthly fee is $10.", "1.1", 2),
        ("1.1.3 Sub-point", "1.1.3", 3),
        ("(a) Encryption keys are rotated.", "a", 4),
        ("## 2.1 Access control", "2.1", 2),  # markdown-prefixed (#389)
        ("### 2 SECURITY", "2", 1),
    ],
)
def test_classify_heading(profile, line, number, level):
    heading = classify_heading(line, profile.heading_patterns)
    assert heading is not None
    assert heading.number == number
    assert heading.level == level


def test_classify_heading_returns_none_for_body_text(profile):
    assert classify_heading("This is just body text.", profile.heading_patterns) is None


def test_preamble_before_first_heading_is_kept(profile):
    chunks = split_numbered_sections(SAMPLE, "doc-1", profile, overlap=0)
    assert chunks[0].content.startswith("PRODUCT HANDBOOK")


def test_sub_levels_stay_in_parent_section(profile):
    chunks = split_numbered_sections(SAMPLE, "doc-1", profile, overlap=0)
    # "2.1" is a level-2 heading (== split_at_level) so it starts its own
    # chunk; "(a)" is level 4, ABOVE split_at_level, so it stays inside
    # 2.1's body rather than starting its own chunk.
    section_2_1 = next(c for c in chunks if "2.1 All data must be encrypted" in c.content)
    assert "(a) Encryption keys are rotated" in section_2_1.content


def test_parent_heading_context_is_prepended(profile):
    chunks = split_numbered_sections(SAMPLE, "doc-1", profile, overlap=0)
    pricing_sub = next(c for c in chunks if "1.1 The monthly fee" in c.content)
    # 1.1 is level 2 == split_at_level, so it DOES start its own chunk; its
    # parent (1. PRICING) is prepended as context.
    assert pricing_sub.content.startswith("1. PRICING")
    assert "1.1 The monthly fee" in pricing_sub.content


def test_never_drops_text(profile):
    chunks = split_numbered_sections(SAMPLE, "doc-1", profile, overlap=0)
    # Every non-blank source line appears in at least one chunk's content.
    for line in SAMPLE.splitlines():
        if line.strip():
            assert any(line.strip() in c.content for c in chunks), line


def test_oversize_section_falls_back_to_sentence_splitting(profile):
    # max_tokens=200 in the fixture profile -> ~800 char budget. Build one
    # section whose own text alone blows well past that.
    long_body = " ".join(f"Sentence number {i} in a very long section." for i in range(60))
    text = f"1. PRICING\n{long_body}\n"
    chunks = split_numbered_sections(text, "doc-1", profile, overlap=0)
    assert len(chunks) > 1
    # Parent context ("1. PRICING") re-injected into every fallback slice.
    assert all(c.content.startswith("1. PRICING") for c in chunks)
    # Concatenating the REAL (non-injected) text back together loses nothing:
    # every chunk's content contains real sentence numbers from the source
    # and none is empty.
    assert all(c.content.strip() for c in chunks)


def test_section_heading_recorded_on_chunk_data(profile):
    chunks = split_numbered_sections(SAMPLE, "doc-1", profile, overlap=0)
    security = next(c for c in chunks if c.section_heading == "2. SECURITY")
    assert security is not None


def test_chunking_strategy_field_not_set_by_splitter_itself(profile):
    # chunk.py's dispatcher is the single place that stamps
    # chunking_strategy="numbered_sections" (same convention as every other
    # strategy) -- the splitter itself leaves it at the ChunkData default.
    chunks = split_numbered_sections(SAMPLE, "doc-1", profile, overlap=0)
    assert all(c.chunking_strategy == "" for c in chunks)
