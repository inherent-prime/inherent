"""`numbered_sections` chunking strategy (inherent#390).

Splits a document at section-boundary headings a vertical pack's chunking
profile describes (ordered heading regexes with a level, a `split_at_level`
cutoff, and whether to keep parent-heading context) -- the same rules the
pack itself already tests in isolation with its reference splitter. This module is
core's generic runtime counterpart: no domain terms, driven entirely by
whatever `ChunkingProfile` the bound pack supplies.

Markdown heading prefixes (#389): #389's structure-preserving extraction now
emits markdown `#` headings, so a heading line coming out of extraction can
look like ``## 2.1 Access control`` rather than bare ``2.1 Access control``.
`classify_heading` strips a leading run of ``#`` characters (and the
whitespace after them) before matching a pack's heading regexes, so a pack's
patterns don't each need to separately account for an optional markdown
prefix.

Oversize sections: a section whose body alone exceeds the pack's
`max_tokens` budget is never emitted as one unbounded chunk -- it falls back
to sentence splitting (`chunk.py`'s `_chunk_by_sentences`), with the same
parent-heading context re-injected into every resulting slice.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache

from inh_contracts.vertical_discovery import discover_all_packs
from inh_contracts.vertical_pack import ChunkingProfile, HeadingPattern, Vertical

from src.temporal.models import ChunkData

# A markdown heading prefix ("#", "##", ...) that #389's extraction may have
# added ahead of a numbered heading. Stripped before pattern matching so a
# pack's own heading_patterns never need to know about markdown at all.
_MARKDOWN_HEADING_PREFIX = re.compile(r"^#+\s*")

# ~4 chars/token, matching chunk.py's own _CHARS_PER_TOKEN convention -- used
# to translate a pack's max_tokens into a character budget for the oversize
# fallback below.
_CHARS_PER_TOKEN = 4


@dataclass(frozen=True)
class _Heading:
    number: str
    level: int


@dataclass
class _Section:
    number: str | None  # None for preamble text before the first heading
    level: int
    heading: str  # the ORIGINAL line (markdown prefix intact), or "" for preamble
    start: int
    end: int = 0  # filled in by flush() when the section closes
    body_lines: list[str] = field(default_factory=list)
    parent_headings: list[str] = field(default_factory=list)


def classify_heading(line: str, patterns: list[HeadingPattern]) -> _Heading | None:
    """Return the first pattern that matches `line`, or None for body text.

    Strips a leading markdown heading prefix (#389) before matching, so
    ``## 2.1 Access control`` matches the same pattern as
    ``2.1 Access control``.
    """
    stripped = _MARKDOWN_HEADING_PREFIX.sub("", line.strip())
    for pattern in patterns:
        match = re.match(pattern.regex, stripped)
        if match:
            return _Heading(number=match.group(1), level=pattern.level)
    return None


def _split_sections(text: str, profile: ChunkingProfile) -> list[_Section]:
    """Split `text` into sections at or above `split_at_level`.

    Deeper headings (e.g. "(a)", "(i)") stay inside their parent section's
    body rather than starting a new one -- mirrors the pack's own reference
    splitter. Offsets are tracked so nothing is ever silently dropped.
    """
    sections: list[_Section] = []
    current = _Section(number=None, level=0, heading="", start=0, end=0)
    open_headings: dict[int, str] = {}  # level -> heading line, for parent context
    cursor = 0

    def flush(end: int) -> None:
        current.end = end
        if current.number is not None or current.body_lines:
            sections.append(current)

    for raw_line in text.splitlines(keepends=True):
        line = raw_line.rstrip("\r\n")
        line_start = cursor
        cursor += len(raw_line)

        heading = classify_heading(line, profile.heading_patterns)
        if heading is None or heading.level > profile.split_at_level:
            current.body_lines.append(line)
            continue

        flush(line_start)
        # A new heading closes itself and every deeper open heading.
        open_headings = {lvl: h for lvl, h in open_headings.items() if lvl < heading.level}
        parents = [open_headings[lvl] for lvl in sorted(open_headings)]
        current = _Section(
            number=heading.number,
            level=heading.level,
            heading=line,
            start=line_start,
            body_lines=[],
            parent_headings=list(parents) if profile.keep_parent_heading else [],
        )
        open_headings[heading.level] = line

    flush(cursor)
    return sections


def _section_text(section: _Section) -> str:
    """The section's own heading + body, WITHOUT parent context -- used both
    to measure real size and as the tail of the final chunk content."""
    lines = ([section.heading] if section.heading else []) + section.body_lines
    return "\n".join(lines).strip()


def _context_prefix(section: _Section) -> str:
    """Parent-heading breadcrumb prepended to a section's chunk (see the
    module docstring's "prepend parent heading" decision) -- e.g.::

        1 Pricing
        1.1 Standard plan
        (chunk body...)

    so a retrieved chunk is self-describing about which section it's under
    without the caller needing to separately resolve ancestor headings.
    """
    if not section.parent_headings:
        return ""
    return "\n".join(h.strip() for h in section.parent_headings) + "\n"


@lru_cache(maxsize=32)
def _discover_packs_cached(packs_dir: str) -> dict[str, Vertical]:
    """Cached directory + entry-point scan -- TODO(follow-up): invalidate/
    refresh this if packs can be hot-swapped on a running worker without a
    restart; today a worker picks up a changed/added pack only on its next
    process start, which matches how every other piece of static config in
    this service is already handled (env vars read once at startup via
    `get_settings`)."""
    return discover_all_packs(packs_dir).packs


def resolve_pack(packs_dir: str | None, pack_name: str | None) -> Vertical | None:
    """Look up a bound pack by name under `packs_dir`.

    Returns None (never raises) when pack discovery is off (`packs_dir` is
    None/unset), no pack name is bound, or the named pack isn't found/fails
    to load -- any of these degrade to "no pack", i.e. the pre-#390 chunking
    dispatch, rather than failing the whole chunk activity over a vertical
    pack problem.
    """
    if not packs_dir or not pack_name:
        return None
    packs: dict[str, Vertical] = _discover_packs_cached(packs_dir)
    return packs.get(pack_name)


def split_numbered_sections(
    text: str,
    document_id: str,
    profile: ChunkingProfile,
    overlap: int,
) -> list[ChunkData]:
    """Split `text` into `ChunkData` using a pack's numbered_sections profile.

    Every section becomes one chunk, its content prefixed with its parent
    headings (when `profile.keep_parent_heading`) so the section number and
    heading trail is visible directly in the chunk text -- the "retrieval
    side can show the section number/heading" requirement is met by making
    it part of what's actually returned, rather than a side channel the
    caller must know to look up separately. `section_heading` on the
    returned `ChunkData` additionally carries just this section's own
    heading line, for structured display/filtering without re-parsing text.

    A section whose OWN text (excluding injected parent context) exceeds the
    token budget falls back to sentence splitting, with the same parent
    context re-injected into each resulting slice -- never one unbounded
    chunk.
    """
    sections = _split_sections(text, profile)
    char_budget = profile.max_tokens * _CHARS_PER_TOKEN

    chunks: list[ChunkData] = []
    chunk_index = 0
    for section in sections:
        own_text = _section_text(section)
        if not own_text:
            continue
        prefix = _context_prefix(section)

        if len(own_text) + len(prefix) <= char_budget:
            chunks.append(
                ChunkData(
                    document_id=document_id,
                    content=(prefix + own_text).strip(),
                    chunk_index=chunk_index,
                    start_char=section.start,
                    end_char=section.end,
                    section_heading=section.heading.strip() if section.heading else "",
                )
            )
            chunk_index += 1
            continue

        # Oversize fallback: sentence-split the section's BODY ONLY (never
        # its heading line -- a numbered heading like "1. PRICING" would
        # itself get mis-split by the sentence regex on its own "1."), then
        # re-inject (parent context + this section's own heading) into every
        # resulting slice, so the fallback stays just as self-describing as
        # the normal case and a heading is never itself chopped in half.
        from src.temporal.activities.chunk import _chunk_by_sentences

        heading_line = section.heading.strip() if section.heading else ""
        full_prefix = prefix + (f"{heading_line}\n" if heading_line else "")
        body_text = "\n".join(section.body_lines).strip()
        body_offset = section.start + (len(section.heading) + 1 if section.heading else 0)

        sub_budget = max(1, char_budget - len(full_prefix))
        sub_chunks = _chunk_by_sentences(body_text, document_id, sub_budget, overlap)
        for sub in sub_chunks:
            chunks.append(
                ChunkData(
                    document_id=document_id,
                    content=(full_prefix + sub.content).strip(),
                    chunk_index=chunk_index,
                    start_char=body_offset + sub.start_char,
                    end_char=body_offset + sub.end_char,
                    section_heading=heading_line,
                )
            )
            chunk_index += 1

    return chunks
