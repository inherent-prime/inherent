"""Workspace -> vertical pack binding (inherent#390 item 2/5).

DESIGN FORK (reported, not guessed, and still true): workspace RECORDS
themselves are owned by a different service/repo (the `workspace_metadata`
table lives in `prime`'s `db/migrations`, not in this engine) -- there is no
column here to read a workspace's bound pack from directly.

Until that binding exists, a hand-onboarded pilot is wired through one
operator setting instead: `WORKSPACE_VERTICAL_PACKS` (parsed by
`inh_contracts.workspace_packs.parse_workspace_vertical_packs`, shared
byte-for-byte with inh-ingestion-svc's identical setting -- see
`src/config/settings.py`). `resolve_workspace_pack` looks a workspace up in
that mapping, then loads the named pack from `VERTICAL_PACKS_DIR` (+ any
`inherent.verticals` entry points). Both settings default to
empty/unset, so every workspace is unaffected until an operator explicitly
maps it.

`resolve_workspace_pack` stays the single, documented extension point: once
a real `workspace_id -> vertical_pack` binding exists on the workspace
record itself, swap this function's body (or its `_workspace_pack_name`
lookup) for that lookup -- every caller already goes through this one
function, so nothing else needs to change.
"""

from __future__ import annotations

from functools import lru_cache

from inh_contracts.vertical_discovery import discover_all_packs
from inh_contracts.vertical_pack import Vertical

from src.config import settings


@lru_cache(maxsize=32)
def _discover_packs_cached(packs_dir: str) -> dict[str, Vertical]:
    """Cached directory + entry-point scan, keyed by `packs_dir` so a test
    that points at a different fixture directory doesn't share the first
    call's cached result. See inh-ingestion-svc's identical cache (same
    TODO on hot-swap invalidation: a pack changed/added on disk is picked
    up on this worker's next process start, matching how every other static
    config knob here is already handled)."""
    return discover_all_packs(packs_dir).packs


def resolve_workspace_pack(workspace_id: str) -> Vertical | None:
    """Return the vertical pack bound to `workspace_id`, or None if none.

    None whenever: `workspace_id` isn't in the `WORKSPACE_VERTICAL_PACKS`
    mapping (the overwhelming majority of workspaces, today all of them
    until an operator maps one), `VERTICAL_PACKS_DIR` is unset, or the
    mapped pack name isn't actually found/fails to load under it -- every
    one of these degrades to "no pack" rather than raising, since a broken
    pack must not break search for every OTHER workspace.
    """
    pack_name = settings.workspace_vertical_packs.get(workspace_id)
    if not pack_name or not settings.vertical_packs_dir:
        return None
    return _discover_packs_cached(settings.vertical_packs_dir).get(pack_name)
