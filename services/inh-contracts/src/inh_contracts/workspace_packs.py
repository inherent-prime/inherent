"""Operator-configured workspace -> vertical pack binding.

Workspace records are owned by a different service/repo (see
``vertical_discovery``'s and ``vertical_pack``'s module docstrings) -- there
is no column in this engine to read a workspace's bound pack from. Until
that binding exists, a hand-onboarded pilot is wired through one operator
setting instead: ``WORKSPACE_VERTICAL_PACKS``, a comma-separated list of
``workspace_id=pack_name`` pairs, read identically by both consuming
services (``inh-ingestion-svc`` and ``inh-public-api-svc``) via this shared
parser -- so the two can never disagree about what a raw env value means.

Unset/empty (the default) means "no bindings" -- feature off, matching every
other vertical-pack knob's off-by-default contract. Any other malformed
value is a hard error raised at settings-construction time (i.e. at service
startup), not a silent partial parse -- a pilot's hand-edited config is
exactly the case where a typo should fail loudly and immediately, not surface
later as "this workspace's tags never showed up."
"""

from __future__ import annotations


class WorkspaceVerticalPacksError(ValueError):
    """``WORKSPACE_VERTICAL_PACKS`` is malformed; the message names the entry and why."""


def parse_workspace_vertical_packs(raw: str | None) -> dict[str, str]:
    """Parse ``"ws_abc=support,ws_def=handbook"`` into ``{workspace_id: pack_name}``.

    Rules (all violations raise ``WorkspaceVerticalPacksError``):

    - Entries are comma-separated; surrounding whitespace on each entry is
      trimmed. A blank entry (e.g. a trailing comma) is skipped, not an error.
    - Each non-blank entry must contain exactly one ``=``, splitting into a
      non-empty ``workspace_id`` and a non-empty ``pack_name`` (both trimmed).
    - A ``workspace_id`` bound twice (even to the same pack name) is
      rejected outright -- a silent "last one wins" would hide a
      contradictory pilot config typo instead of catching it at startup.

    ``None`` or an all-whitespace/empty string returns ``{}`` -- the
    deliberate "unset" case, not an error.
    """
    if not raw or not raw.strip():
        return {}

    bindings: dict[str, str] = {}
    for raw_entry in raw.split(","):
        entry = raw_entry.strip()
        if not entry:
            continue  # a stray/trailing comma, not a real entry

        if "=" not in entry:
            raise WorkspaceVerticalPacksError(
                f"WORKSPACE_VERTICAL_PACKS entry {entry!r} is missing '=' "
                "(expected workspace_id=pack_name)"
            )
        workspace_id, _, pack_name = entry.partition("=")
        workspace_id = workspace_id.strip()
        pack_name = pack_name.strip()

        if not workspace_id or not pack_name:
            raise WorkspaceVerticalPacksError(
                f"WORKSPACE_VERTICAL_PACKS entry {entry!r} has an empty workspace_id or pack_name"
            )
        if "=" in pack_name:
            # partition() only splits on the FIRST '=', so a second one
            # landed inside what we're calling pack_name -- reject rather
            # than silently accept "support=extra" as a pack name.
            raise WorkspaceVerticalPacksError(
                f"WORKSPACE_VERTICAL_PACKS entry {entry!r} has more than one '=' "
                "(pack names cannot contain '=')"
            )
        if workspace_id in bindings:
            raise WorkspaceVerticalPacksError(
                f"WORKSPACE_VERTICAL_PACKS: workspace_id {workspace_id!r} is bound twice "
                f"({bindings[workspace_id]!r} and {pack_name!r})"
            )
        bindings[workspace_id] = pack_name

    return bindings
