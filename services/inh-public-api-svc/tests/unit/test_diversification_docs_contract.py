"""Docs describing ``enable_diversification``'s default must match the code.

Why this test exists: #146 flipped ``Settings.enable_diversification`` from
``False`` to ``True`` on 2026-08-06 and updated *some* of the prose, but
``docs/architecture/overview.md`` kept saying "default off" for six days --
long enough to be the published answer while CI was measuring the opposite
behaviour. The flag changes ranking for every multi-chunk-per-document query,
so "is it on?" is the single fact a reader (or an agent) needs from these
pages; a page that gets it backwards is worse than one that omits it.

This is the same anti-drift pattern as ``test_docs_sync.py`` (#117/#193): the
golden value lives in ONE place -- the ``Settings`` field default -- and the
prose surfaces are verified against it rather than maintained by hand.

Scope -- CURRENT_STATE_SURFACES only:

Only pages that describe how the system behaves *today* are checked. ADR 0004
is deliberately excluded: a decision record narrates history, so it correctly
contains both "default ``False`` at the time" (the 2026-07-23 decision) and
"default flipped to ``True``" (the 2026-08-06 amendment). Asserting a single
polarity over a document whose job is to hold both would force the ADR to
lie about its own past.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from src.config.settings import Settings

# tests/unit -> inh-public-api-svc -> services -> repo root (same REPO_ROOT
# convention as test_docs_sync.py).
REPO_ROOT = Path(__file__).resolve().parents[4]

# Pages that describe present-day behaviour and name the flag. Add a page here
# when it starts documenting the default; see the module docstring for why
# docs/adr/** is excluded.
CURRENT_STATE_SURFACES = (
    REPO_ROOT / "docs" / "architecture" / "overview.md",
    REPO_ROOT / "docs" / "advanced-indexes.md",
)

# Phrases that assert a polarity for the default. Matched case-insensitively
# against the sentence that mentions the flag, so an unrelated "default off"
# elsewhere on the page (e.g. about a different setting) is not misread.
DEFAULT_ON_PHRASES = ("default on", "on by default", "defaults to true", "default `true`")
DEFAULT_OFF_PHRASES = (
    "default off",
    "off by default",
    "defaults to false",
    "default `false`",
    "opt-in",
    "opt in",
)

FLAG = "enable_diversification"


def _sentences_mentioning_flag(text: str) -> list[str]:
    """Return every sentence in ``text`` that names the flag.

    Markdown prose wraps across lines, so split on sentence terminators rather
    than newlines -- otherwise "..., `enable_diversification`, default off) is
    a post-filter..." (a single sentence broken over three source lines) would
    be inspected as fragments and the polarity claim missed.
    """
    flat = re.sub(r"\s+", " ", text)
    sentences = re.split(r"(?<=[.!?]) ", flat)
    return [s for s in sentences if FLAG in s]


@pytest.mark.parametrize("doc_path", CURRENT_STATE_SURFACES, ids=lambda p: p.name)
def test_docs_state_the_flags_real_default(doc_path: Path) -> None:
    """Each current-state page must claim the polarity the code actually has."""
    assert doc_path.exists(), f"missing documentation surface {doc_path}"
    sentences = _sentences_mentioning_flag(doc_path.read_text())
    assert sentences, f"{doc_path} no longer mentions {FLAG} -- update CURRENT_STATE_SURFACES"

    default_on = Settings.model_fields[FLAG].default
    wrong_phrases = DEFAULT_OFF_PHRASES if default_on else DEFAULT_ON_PHRASES

    for sentence in sentences:
        lowered = sentence.lower()
        contradictions = [p for p in wrong_phrases if p in lowered]
        assert not contradictions, (
            f"{doc_path.relative_to(REPO_ROOT)} says {contradictions} about {FLAG}, "
            f"but Settings.{FLAG} defaults to {default_on}. "
            f"Offending sentence: {sentence.strip()!r}"
        )


def test_at_least_one_surface_states_the_default_explicitly() -> None:
    """Guard against 'fixing' drift by deleting the claim instead of correcting it.

    Silence passes the polarity check above trivially, so require that the
    default is stated somewhere a reader will find it.
    """
    default_on = Settings.model_fields[FLAG].default
    right_phrases = DEFAULT_ON_PHRASES if default_on else DEFAULT_OFF_PHRASES

    stated = [
        doc_path.name
        for doc_path in CURRENT_STATE_SURFACES
        for sentence in _sentences_mentioning_flag(doc_path.read_text())
        if any(p in sentence.lower() for p in right_phrases)
    ]
    assert stated, (
        f"no current-state doc states that {FLAG} defaults to {default_on}; "
        f"expected one of {right_phrases} in {[p.name for p in CURRENT_STATE_SURFACES]}"
    )
