"""Usage-based ranking boost math (inherent#394).

A chunk whose content keeps reappearing (near-duplicated) in newer documents
in the same workspace has been "reused" -- ``document_chunks.reuse_count`` /
the mirrored Weaviate ``reuse_count`` property counts how many times
(``inh-ingestion-svc``'s reuse-detection step, gated per-workspace by
``WORKSPACE_REUSE_BOOST``, increments it; see that module and
``services/inh-ingestion-svc/src/services/reuse_detection.py``).

This module is the single, shared formula both call sites of the boost use
so they can never drift: ``inh-public-api-svc``'s ``SearchService`` (the only
caller today, applied after fusion for semantic/hybrid/keyword alike) and
any future consumer.

Formula (issue #394's own wording)::

    score * min(cap, 1 + weight * log1p(reuse_count))

- ``log1p`` (``log(1 + reuse_count)``) rather than a linear term: the first
  few reuses matter a lot (a chunk reused once vs. never is a real signal),
  but the 50th reuse should barely move the needle over the 49th -- a
  logarithm gives exactly that diminishing-returns shape without a second
  tunable.
- ``weight`` (``WORKSPACE_REUSE_BOOST``'s per-workspace value, ``[0.0,
  1.0]``) scales how much a workspace trusts the reuse signal. ``weight ==
  0.0`` (or ``reuse_count == 0``) multiplies by exactly ``1.0`` -- score
  unchanged -- so a workspace with no override, or a chunk nobody has ever
  reused, is BYTE-FOR-BYTE identical to ranking before this feature existed.
- ``cap`` (default ``1.5``, i.e. +50% at most) bounds how far even an
  extremely-reused chunk can jump the ranking -- a bounded boost can reorder
  the page but can never let a barely-relevant chunk bury a highly relevant
  one purely on reuse count.
"""

from __future__ import annotations

import math

# Default hard cap on the boost multiplier (+50% at most) -- see module
# docstring. Kept as a named default (not just inlined 1.5) so a future
# caller reading `apply_reuse_boost(score, count, weight)` sees the cap
# value without reading this module's internals.
DEFAULT_BOOST_CAP = 1.5


def reuse_boost_multiplier(
    reuse_count: int, weight: float, cap: float = DEFAULT_BOOST_CAP
) -> float:
    """Return the bounded multiplier ``min(cap, 1 + weight * log1p(reuse_count))``.

    ``reuse_count <= 0`` or ``weight <= 0`` always returns exactly ``1.0``
    (no boost) -- never a value fractionally off from 1.0 due to floating
    point on ``log1p(0)``, so "disabled" really does mean "unchanged".
    """
    if reuse_count <= 0 or weight <= 0:
        return 1.0
    multiplier = 1.0 + weight * math.log1p(reuse_count)
    return min(cap, multiplier)


def apply_reuse_boost(
    score: float, reuse_count: int, weight: float, cap: float = DEFAULT_BOOST_CAP
) -> float:
    """Return ``score`` scaled by the bounded reuse-boost multiplier.

    See module docstring for the full formula and rationale. A ``weight`` of
    ``0.0`` (workspace has no ``WORKSPACE_REUSE_BOOST`` override) or a
    ``reuse_count`` of ``0`` (chunk never detected as reused) leaves
    ``score`` byte-for-byte unchanged.
    """
    return score * reuse_boost_multiplier(reuse_count, weight, cap)
