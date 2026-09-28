"""Claim-level citation + verification models (#39).

A :class:`Citation` is a self-contained, auditable reference to the exact chunk
that supports a claim. It is populated on each ``SearchResult`` from that
result's own fields (stable ``chunk_id`` + character spans + score + provenance
+ freshness) so a caller can cite evidence without a second lookup.

A :class:`SupportVerdict` is the offline verification of how well a piece of
evidence supports a claim (see :mod:`src.services.verify`).
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from src.models.search import ScoreSource

SupportLevel = Literal["strong", "weak", "none"]


class Citation(BaseModel):
    """A citable reference to the chunk that supports a claim.

    All fields are sourced from the originating search result, so the citation
    is stable (same chunk_id + spans across requests) and auditable (provenance
    + freshness travel with it). Optional fields are simply ``None`` when the
    underlying chunk lacks them, keeping this backward-compatible.
    """

    chunk_id: str
    document_id: str
    document_name: str
    content: str
    start_char: int | None = None
    end_char: int | None = None
    score: float
    score_source: ScoreSource | None = None
    source_uri: str | None = None
    # Source link (inherent#391) — the original file's link in its source
    # system (e.g. a Drive webViewLink), distinct from source_uri above.
    source_url: str | None = None
    ingested_at: datetime | None = None
    is_stale: bool = False


class SupportVerdict(BaseModel):
    """Whether (and how strongly) evidence supports a claim.

    Produced by the offline lexical verifier in :mod:`src.services.verify`.

    - ``support_level``: ``"strong"`` | ``"weak"`` | ``"none"``.
    - ``score``: overlap score in ``[0, 1]`` (higher = better supported).
    - ``reason``: short human-readable explanation of the verdict.
    """

    support_level: SupportLevel
    score: float
    reason: str
